"""
MSTR Brain API Server v2.3
- MSTR Preis + Optionskette: Webull inoffizieller Wrapper (kostenlos, kein Broker-Account)
- Bitcoin: CoinGecko (live)
- Fear & Greed: alternative.me (live)
- Telegram: Roll, Warnung, Gewinnmitnahme, Morgen-Briefing 08:00 UTC
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests as req
from datetime import datetime, timedelta
import math, time, os, threading, schedule, json

app = Flask(__name__)
CORS(app)

TG_BOT_TOKEN  = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID    = os.environ.get('TG_CHAT_ID', '')
WB_EMAIL      = os.environ.get('WB_EMAIL', '')      # Email ODER Telefon
WB_PHONE      = os.environ.get('WB_PHONE', '')      # z.B. +4915112345678
WB_PASSWORD   = os.environ.get('WB_PASSWORD', '')

# ─── Cache 5 Minuten ───
_cache = {}
def cached(key, fn, ttl=300):
    now = time.time()
    if key in _cache and now - _cache[key]['ts'] < ttl:
        return _cache[key]['data']
    data = fn()
    _cache[key] = {'data': data, 'ts': now}
    return data

# ─── Telegram ───
def tg_send(msg):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        req.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=8)
        return True
    except Exception as e:
        print(f"Telegram Fehler: {e}")
        return False

_alarm_sent = {}
def can_alarm(t):
    now = time.time()
    if now - _alarm_sent.get(t, 0) > 1800:
        _alarm_sent[t] = now
        return True
    return False

# ─── Black-Scholes Delta ───
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_delta(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

def find_best_expiry_days(expirations, target_dte=42):
    """expirations = list of 'YYYY-MM-DD' strings"""
    today = datetime.today()
    best, best_diff = None, 9999
    for exp_str in expirations:
        try:
            dte = (datetime.strptime(exp_str, "%Y-%m-%d") - today).days
            if dte < 14:
                continue
            diff = abs(dte - target_dte)
            if diff < best_diff:
                best_diff, best = diff, exp_str
        except:
            continue
    return best

# ══════════════════════════════════════════
# WEBULL SESSION
# ══════════════════════════════════════════
_wb_session = {"token": None, "expires": 0, "account_id": None}

WEBULL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "did": "8a7f3d2e1c4b5a6f",  # Device ID
    "tz": "Europe/Berlin",
    "app": "global",
    "ver": "3.40.8",
    "lver": "2.40.8",
    "platform": "web",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
}

def wb_login():
    # Webull blockiert Logins von Cloud-Server-IPs (503)
    # Quote + Optionen funktionieren als public endpoints ohne Login
    return None


def wb_get_token():
    """Token holen, ggf. neu einloggen"""
    if _wb_session["token"] and time.time() < _wb_session["expires"]:
        return _wb_session["token"]
    return wb_login()

def wb_get_ticker_id(symbol="MSTR"):
    """Webull interne Ticker-ID für ein Symbol"""
    try:
        url = f"https://quotes-gw.webullfintech.com/api/search/pc/tickers?keyword={symbol}&pageIndex=1&pageSize=5"
        r = req.get(url, headers=WEBULL_HEADERS, timeout=10)
        data = r.json().get("data", [])
        for item in data:
            if item.get("tickerSymbol") == symbol and item.get("exchangeCode") in ("NASDAQ", "NYSE", "NSQ", "NMS", "NGS"):
                return str(item["tickerId"])
        return None
    except Exception as e:
        print(f"Ticker ID Fehler: {e}")
        return None

def wb_get_quote(ticker_id):
    """MSTR Kurs von Webull — probiert mehrere Endpoints"""
    urls = [
        f"https://quotes-gw.webullfintech.com/api/stock/tickerQuote?tickerId={ticker_id}&includeSecu=1",
        f"https://quotes-gw.webullfintech.com/api/quote/tickerSnapshot?tickerIds={ticker_id}",
        f"https://quotes-gw.webullfintech.com/api/stock/tickerRealTime/queryByTicker?tickerIds={ticker_id}",
    ]
    for url in urls:
        try:
            r = req.get(url, headers=WEBULL_HEADERS, timeout=10)
            d = r.json()
            if isinstance(d, list) and len(d) > 0:
                d = d[0]
            price = 0
            for field in ["close","pPrice","lastPrice","last","price","currentPrice","latestPrice","open"]:
                val = d.get(field)
                if val:
                    try:
                        price = float(val)
                        if price > 0:
                            break
                    except:
                        pass
            if price > 0:
                pre = 0
                for field in ["preClose","prevClose","previousClose","open"]:
                    val = d.get(field)
                    if val:
                        try:
                            pre = float(val)
                            break
                        except:
                            pass
                print(f"Quote OK: ${price} (url={url.split('?')[0].split('/')[-1]})")
                return {"price": round(price, 2),
                        "change_pct": round((price-pre)/pre*100, 2) if pre else 0}
            print(f"Quote 0 from {url.split('/')[-1]}: keys={list(d.keys())[:8]}")
        except Exception as e:
            print(f"Quote err: {e}")
    return None

def wb_get_options(ticker_id, spot):
    """Optionskette von Webull — ~42 DTE, Calls, Delta 0.03-0.22"""
    try:
        # Schritt 1: Verfügbare Expiry-Daten
        url = f"https://quotes-gw.webullfintech.com/api/quote/option/expireList?tickerId={ticker_id}&count=20"
        r   = req.get(url, headers=WEBULL_HEADERS, timeout=10)
        expire_list = r.json()

        if not expire_list:
            return None

        # Bestes Expiry ~42 DTE
        exp_dates = [e.get("date", "") for e in expire_list if e.get("date")]
        best_exp  = find_best_expiry_days(exp_dates, 42)
        if not best_exp:
            best_exp = exp_dates[0] if exp_dates else None
        if not best_exp:
            return None

        dte = (datetime.strptime(best_exp, "%Y-%m-%d") - datetime.today()).days
        T   = dte / 365.0

        # Schritt 2: Options Chain für dieses Expiry
        chain_url = (f"https://quotes-gw.webullfintech.com/api/quote/option/list"
                     f"?tickerId={ticker_id}&expireDate={best_exp}&direction=call&count=50")
        cr  = req.get(chain_url, headers=WEBULL_HEADERS, timeout=12)
        chain_data = cr.json()

        if not chain_data:
            return None

        # chain_data ist eine Liste von Options
        options_list = chain_data if isinstance(chain_data, list) else chain_data.get("data", [])

        rows = []
        for opt in options_list:
            try:
                strike = float(opt.get("strikePrice", 0))
                bid    = float(opt.get("bidPrice", 0) or 0)
                ask    = float(opt.get("askPrice", 0) or 0)
                mid    = round((bid + ask) / 2, 2) if bid and ask else None
                iv_raw = float(opt.get("impVol", 0) or 0)
                iv     = iv_raw if iv_raw > 0 else 0.88

                # Delta aus Webull oder BS berechnen
                delta_raw = opt.get("delta")
                if delta_raw is not None:
                    delta = round(abs(float(delta_raw)), 3)
                else:
                    delta = round(bs_delta(spot, strike, T, iv), 3)

                if delta < 0.03 or delta > 0.22:
                    continue

                iv_pct = round(iv * 100, 1) if iv < 5 else round(iv, 1)

                rows.append({
                    "strike":        strike,
                    "delta":         delta,
                    "bid":           round(bid, 2) if bid else None,
                    "ask":           round(ask, 2) if ask else None,
                    "mid":           mid,
                    "iv":            iv_pct,
                    "dte":           dte,
                    "otm_pct":       round((strike - spot) / spot * 100, 1),
                    "volume":        int(opt.get("volume", 0) or 0),
                    "open_interest": int(opt.get("openInterest", 0) or 0),
                })
            except Exception as e:
                print(f"  Option parse Fehler: {e}")
                continue

        rows.sort(key=lambda x: x["delta"], reverse=True)
        return {
            "expiry":  best_exp,
            "dte":     dte,
            "spot":    round(spot, 2),
            "source":  "webull",
            "options": rows
        }
    except Exception as e:
        print(f"Webull Options Fehler: {e}")
        return None

# ══════════════════════════════════════════
# TICKER ID CACHE
# ══════════════════════════════════════════
_ticker_id = None
def get_ticker_id():
    global _ticker_id
    if not _ticker_id:
        # Versuche zuerst dynamisch zu holen
        result = wb_get_ticker_id("MSTR")
        # Fallback: hardcoded (aus /debug verifiziert am 17.03.2026)
        _ticker_id = result or "913323987"
    return _ticker_id

# ══════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════
@app.route('/')
def index():
    tid = get_ticker_id()
    return jsonify({
        "status":    "MSTR Brain API v2.3 ✅",
        "webull":    f"Ticker ID: {tid}" if tid else "nicht verbunden",
        "telegram":  "aktiv" if TG_BOT_TOKEN else "nicht konfiguriert",
        "endpoints": ["/all", "/mstr", "/btc", "/fg", "/options", "/alarm", "/debug"]
    })

@app.route('/debug')
def debug():
    results = {}
    try:
        url = "https://quotes-gw.webullfintech.com/api/search/pc/tickers?keyword=MSTR&pageIndex=1&pageSize=5"
        r = req.get(url, headers=WEBULL_HEADERS, timeout=10)
        results['ticker_status'] = r.status_code
        # Only show MSTR result
        data = r.json().get('data', [])
        mstr = next((x for x in data if x.get('symbol') == 'MSTR'), None)
        results['ticker_id'] = mstr.get('tickerId') if mstr else None
    except Exception as e:
        results['ticker_error'] = str(e)
    # Test quote endpoint directly
    try:
        tid = "913323987"
        url = f"https://quotes-gw.webullfintech.com/api/stock/tickerQuote?tickerId={tid}&includeSecu=1"
        r = req.get(url, headers=WEBULL_HEADERS, timeout=10)
        results['quote_status'] = r.status_code
        results['quote_raw'] = r.json()
    except Exception as e:
        results['quote_error'] = str(e)
    # Test quote endpoint
    try:
        tid = "913323987"
        url = f"https://quotes-gw.webullfintech.com/api/stock/tickerQuote?tickerId={tid}&includeSecu=1"
        r = req.get(url, headers=WEBULL_HEADERS, timeout=10)
        results['quote_status'] = r.status_code
        results['quote_raw'] = r.json()
    except Exception as e:
        results['quote_error'] = str(e)
    results['wb_phone_set'] = bool(WB_PHONE)
    results['wb_email_set'] = bool(WB_EMAIL)
    results['wb_password_set'] = bool(WB_PASSWORD)
    if WB_PHONE or WB_EMAIL:
        try:
            account = WB_PHONE if WB_PHONE else WB_EMAIL
            account_type = "1" if WB_PHONE else "2"
            r = req.post("https://userapi.webull.com/api/passport/login/v5/account",
                json={"account": account, "accountType": account_type,
                      "pwd": WB_PASSWORD, "deviceId": "8a7f3d2e1c4b5a6f",
                      "deviceName": "MSTR Brain Server", "grade": 1, "regionId": 91},
                headers=WEBULL_HEADERS, timeout=15)
            results['login_status'] = r.status_code
            results['login_raw'] = r.json()
        except Exception as e:
            results['login_error'] = str(e)
    return jsonify(results)

@app.route('/mstr')
def get_mstr():
    def fetch():
        tid = get_ticker_id()
        if not tid:
            return {"error": "Webull Ticker ID nicht gefunden"}
        q = wb_get_quote(tid)
        if not q:
            return {"error": "Webull Quote fehlgeschlagen"}
        q["delayed"] = True
        q["as_of"]   = datetime.utcnow().isoformat() + "Z"
        return q
    try:
        return jsonify(cached('mstr', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/btc')
def get_btc():
    def fetch():
        r = req.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_7d_change": "true"}, timeout=8)
        d = r.json()["bitcoin"]
        return {"price": round(d["usd"]),
                "change_24h": round(d.get("usd_24h_change", 0), 2),
                "change_7d":  round(d.get("usd_7d_change", 0), 2),
                "as_of": datetime.utcnow().isoformat() + "Z"}
    try:
        return jsonify(cached('btc', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fg')
def get_fg():
    def fetch():
        data = req.get("https://api.alternative.me/fng/?limit=2", timeout=8).json()["data"]
        return {"value": int(data[0]["value"]), "label": data[0]["value_classification"],
                "yesterday": int(data[1]["value"]) if len(data) > 1 else None,
                "as_of": datetime.utcnow().isoformat() + "Z"}
    try:
        return jsonify(cached('fg', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/options')
def get_options():
    def fetch():
        tid = get_ticker_id()
        if not tid:
            return {"error": "Webull Ticker ID nicht gefunden"}
        q = wb_get_quote(tid)
        spot = q["price"] if q else 150.0
        result = wb_get_options(tid, spot)
        if not result:
            return {"error": "Webull Options fehlgeschlagen"}
        result["as_of"] = datetime.utcnow().isoformat() + "Z"
        return result
    try:
        return jsonify(cached('options', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/all')
def get_all():
    def fetch_all():
        out  = {}
        spot = 150.0
        tid  = get_ticker_id()

        # MSTR
        try:
            if tid:
                q = wb_get_quote(tid)
                if q:
                    spot = q["price"]
                    out['mstr'] = {"price": q["price"], "change_pct": q["change_pct"], "delayed": True}
                else:
                    out['mstr'] = {"error": "Quote fehlgeschlagen"}
            else:
                out['mstr'] = {"error": "Ticker ID nicht gefunden"}
        except Exception as e:
            out['mstr'] = {"error": str(e)}

        # BTC
        try:
            r   = req.get("https://api.coingecko.com/api/v3/simple/price",
                  params={"ids": "bitcoin", "vs_currencies": "usd",
                          "include_24hr_change": "true", "include_7d_change": "true"}, timeout=8)
            btc = r.json()["bitcoin"]
            out['btc'] = {"price": round(btc["usd"]),
                          "change_24h": round(btc.get("usd_24h_change", 0), 2),
                          "change_7d":  round(btc.get("usd_7d_change", 0), 2)}
        except Exception as e:
            out['btc'] = {"error": str(e)}

        # Fear & Greed
        try:
            fg = req.get("https://api.alternative.me/fng/?limit=2", timeout=8).json()["data"]
            out['fg'] = {"value": int(fg[0]["value"]), "label": fg[0]["value_classification"],
                         "yesterday": int(fg[1]["value"]) if len(fg) > 1 else None}
        except Exception as e:
            out['fg'] = {"error": str(e)}

        # Optionen
        try:
            if tid:
                opts = wb_get_options(tid, spot)
                out['options'] = opts or {"error": "Keine Optionsdaten"}
            else:
                out['options'] = {"error": "Ticker ID nicht gefunden"}
        except Exception as e:
            out['options'] = {"error": str(e)}

        out['as_of'] = datetime.utcnow().isoformat() + "Z"
        return out

    try:
        return jsonify(cached('all', fetch_all))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════
# TELEGRAM ALARM
# ══════════════════════════════════════════
@app.route('/alarm', methods=['POST'])
def send_alarm():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return jsonify({"error": "Telegram nicht konfiguriert"}), 400

    d      = request.json or {}
    t      = d.get('type', 'unknown')
    mstr   = d.get('mstr', '?')
    strike = d.get('strike', '?')
    buf    = d.get('buffer', '?')
    dte    = d.get('dte', '?')
    delta  = d.get('delta', '?')
    ns     = d.get('new_strike', '?')
    prem   = d.get('premium', '?')
    cur    = d.get('current_val', '?')
    fg     = d.get('fg', '?')
    btc    = d.get('btc', 0)
    ts     = datetime.utcnow().strftime('%d.%m. %H:%M UTC')
    btc_fmt = f"${int(btc):,}".replace(',', '.') if isinstance(btc, (int, float)) and btc else f"${btc}"

    if not can_alarm(t):
        return jsonify({"status": "cooldown"}), 200

    if t == 'roll':
        msg = (f"⚡ <b>MSTR BRAIN — JETZT ROLLEN!</b>\n🕐 {ts}\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"📈 MSTR:   <b>${mstr}</b>\n"
               f"🎯 Strike: <b>${strike}</b>\n"
               f"⚠️ Puffer: <b>{buf}%</b> — unter 6%!\n"
               f"📅 DTE:    <b>{dte} Tage</b>\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"<b>Jetzt handeln:</b>\n"
               f"① Buy-to-Close: Call ${strike}\n"
               f"② Sell-to-Open: <b>${ns}</b> · Δ{delta} · 42 Tage\n"
               f"③ NUR für Kredit! Kein Debit!\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"🌍 F&G: {fg} · BTC: {btc_fmt}")
    elif t == 'warn':
        msg = (f"⚠️ <b>MSTR BRAIN — Strike nähert sich</b>\n🕐 {ts}\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"📈 MSTR:   <b>${mstr}</b>\n"
               f"🎯 Strike: <b>${strike}</b>\n"
               f"📊 Puffer: <b>{buf}%</b> — unter 12%\n"
               f"📅 DTE:    <b>{dte} Tage</b>\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"👀 Beobachten · Roll auf ${ns} vorbereiten · Δ{delta}\n"
               f"🌍 F&G: {fg} · BTC: {btc_fmt}")
    elif t == 'profit':
        try:
            gewinn = str(round((float(prem) - float(cur)) * 200))
        except:
            gewinn = '?'
        msg = (f"💰 <b>MSTR BRAIN — Gewinnmitnahme!</b>\n🕐 {ts}\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"📈 MSTR:     <b>${mstr}</b>\n"
               f"🎯 Strike:   <b>${strike}</b>\n"
               f"💵 Kassiert: <b>${prem}/Aktie</b>\n"
               f"📉 Aktuell:  <b>${cur}/Aktie</b>\n"
               f"✅ 75%+ Zeitwert verloren!\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"① Buy-to-Close: ~${cur}/Aktie\n"
               f"② Gewinn: ~<b>${gewinn}</b> (2 Kontrakte)\n"
               f"③ Neuer Zyklus: ${ns} · Δ{delta} · 42 Tage")
    else:
        msg = f"🔔 MSTR Brain: {t}\nMSTR: ${mstr} · Strike: ${strike}"

    ok = tg_send(msg)
    return jsonify({"status": "sent" if ok else "error", "type": t})

# ══════════════════════════════════════════
# MORGEN-BRIEFING 08:00 UTC
# ══════════════════════════════════════════
def send_morning_briefing():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        tid  = get_ticker_id()
        mstr, mchg = 0, 0
        if tid:
            q = wb_get_quote(tid)
            if q:
                mstr = q["price"]
                mchg = q["change_pct"]

        r    = req.get("https://api.coingecko.com/api/v3/simple/price",
               params={"ids": "bitcoin", "vs_currencies": "usd",
                       "include_24hr_change": "true"}, timeout=8)
        btcd = r.json()["bitcoin"]
        btc  = round(btcd["usd"])
        bchg = round(btcd.get("usd_24h_change", 0), 2)

        fgd  = req.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()["data"][0]
        fg, fgl = int(fgd["value"]), fgd["value_classification"]

        ts    = datetime.utcnow().strftime('%d.%m.%Y')
        btc_f = f"${btc:,}".replace(',', '.')
        msg   = (f"☀️ <b>MSTR Brain — Morgen-Briefing {ts}</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━\n"
                 f"📈 MSTR: <b>${mstr}</b>  {'+' if mchg >= 0 else ''}{mchg}%\n"
                 f"₿  BTC:  <b>{btc_f}</b>  {'+' if bchg >= 0 else ''}{bchg}%\n"
                 f"😱 F&G:  <b>{fg}</b> — {fgl}\n"
                 f"━━━━━━━━━━━━━━━━━━━━━\n"
                 f"App öffnen für Optionskette & Empfehlung 📱")
        tg_send(msg)
        print(f"Morgen-Briefing gesendet {ts}")
    except Exception as e:
        print(f"Morgen-Briefing Fehler: {e}")

def run_scheduler():
    schedule.every().day.at("08:00").do(send_morning_briefing)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == '__main__':
    # Ticker ID beim Start voraufladen
    threading.Thread(target=get_ticker_id, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

"""
MSTR Brain API Server v2.1
- Polygon.io: Bulk Snapshot (ein einziger API-Call für alle Optionen)
- Kein Rate-Limit Problem mehr
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import requests as req
from datetime import datetime, timedelta
import math, time, os, threading, schedule

app = Flask(__name__)
CORS(app)

POLYGON_KEY  = os.environ.get('POLYGON_KEY', '')
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID', '')

_cache = {}
def cached(key, fn, ttl=300):
    now = time.time()
    if key in _cache and now - _cache[key]['ts'] < ttl:
        return _cache[key]['data']
    data = fn()
    _cache[key] = {'data': data, 'ts': now}
    return data

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

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_delta(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

def find_best_expiry(expirations, target_dte=42):
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
# POLYGON — BULK SNAPSHOT (1 API-Call!)
# ══════════════════════════════════════════
def fetch_options_polygon(spot):
    if not POLYGON_KEY:
        return None
    today = datetime.today()
    exp_from = (today + timedelta(days=25)).strftime('%Y-%m-%d')
    exp_to   = (today + timedelta(days=65)).strftime('%Y-%m-%d')

    try:
        # Schritt 1: Welche Expiries gibt es? (1 Call)
        r = req.get("https://api.polygon.io/v3/reference/options/contracts", params={
            "underlying_ticker": "MSTR",
            "contract_type":     "call",
            "expiration_date.gte": exp_from,
            "expiration_date.lte": exp_to,
            "limit": 250,
            "apiKey": POLYGON_KEY
        }, timeout=12)
        contracts = r.json().get('results', [])
        if not contracts:
            print("Polygon: keine Contracts gefunden")
            return None

        # Bestes Expiry wählen
        expiries  = sorted(set(c['expiration_date'] for c in contracts))
        best_exp  = find_best_expiry(expiries, 42)
        if not best_exp:
            return None

        filtered = [c for c in contracts if c['expiration_date'] == best_exp]
        dte = (datetime.strptime(best_exp, "%Y-%m-%d") - today).days
        T   = dte / 365.0
        print(f"Polygon: {len(filtered)} Contracts für {best_exp} ({dte} DTE)")

        # Schritt 2: BULK Snapshot — alle Tickers auf einmal (1 Call!)
        tickers_str = ",".join(c['ticker'] for c in filtered[:250])
        snap_r = req.get(
            "https://api.polygon.io/v3/snapshot/options/MSTR",
            params={
                "expiration_date": best_exp,
                "contract_type":   "call",
                "limit":           250,
                "apiKey":          POLYGON_KEY
            },
            timeout=15
        )
        snaps = snap_r.json().get('results', [])
        print(f"Polygon: {len(snaps)} Snapshots erhalten")

        rows = []
        for snap in snaps:
            try:
                details = snap.get('details', {})
                greeks  = snap.get('greeks', {})
                day     = snap.get('day', {})
                lq      = snap.get('last_quote', {})

                strike  = float(details.get('strike_price', 0))
                bid     = lq.get('bid') or day.get('open') or 0
                ask     = lq.get('ask') or day.get('close') or 0
                mid     = round((float(bid) + float(ask)) / 2, 2) if bid and ask else None
                iv_raw  = snap.get('implied_volatility', 0) or 0
                delta   = greeks.get('delta')

                if delta is None:
                    iv_use = iv_raw if iv_raw > 0.05 else 0.88
                    delta  = bs_delta(spot, strike, T, iv_use)
                delta = round(float(delta), 3)

                if delta < 0.03 or delta > 0.22:
                    continue

                iv_pct = round(iv_raw * 100, 1) if iv_raw < 5 else round(float(iv_raw), 1)

                rows.append({
                    "strike":        strike,
                    "delta":         delta,
                    "bid":           round(float(bid), 2) if bid else None,
                    "ask":           round(float(ask), 2) if ask else None,
                    "mid":           mid,
                    "iv":            iv_pct,
                    "dte":           dte,
                    "otm_pct":       round((strike - spot) / spot * 100, 1),
                    "volume":        int(day.get('volume', 0) or 0),
                    "open_interest": int(snap.get('open_interest', 0) or 0),
                })
            except Exception as e:
                print(f"  Snap parse Fehler: {e}")
                continue

        rows.sort(key=lambda x: x['delta'], reverse=True)
        print(f"Polygon: {len(rows)} Optionen nach Delta-Filter (0.03–0.22)")
        return {
            "expiry":  best_exp,
            "dte":     dte,
            "spot":    round(spot, 2),
            "source":  "polygon.io",
            "options": rows
        }
    except Exception as e:
        print(f"Polygon Fehler: {e}")
        return None

# ══════════════════════════════════════════
# FALLBACK: yfinance
# ══════════════════════════════════════════
def fetch_options_yfinance(spot):
    try:
        ticker  = yf.Ticker("MSTR")
        exp_str = find_best_expiry(list(ticker.options), 42)
        if not exp_str:
            return None
        calls   = ticker.option_chain(exp_str).calls
        dte     = (datetime.strptime(exp_str, "%Y-%m-%d") - datetime.today()).days
        T       = dte / 365.0
        rows    = []
        for _, row in calls.iterrows():
            strike = float(row['strike'])
            bid    = float(row['bid']) if row['bid'] > 0 else None
            ask    = float(row['ask']) if row['ask'] > 0 else None
            mid    = round((bid + ask) / 2, 2) if bid and ask else None
            iv     = float(row['impliedVolatility']) if row['impliedVolatility'] > 0 else 0.88
            delta  = round(bs_delta(spot, strike, T, iv), 3)
            if delta < 0.03 or delta > 0.22:
                continue
            rows.append({"strike": strike, "delta": delta, "bid": bid, "ask": ask, "mid": mid,
                         "iv": round(iv*100,1), "dte": dte,
                         "otm_pct": round((strike-spot)/spot*100,1),
                         "volume": int(row.get('volume',0) or 0),
                         "open_interest": int(row.get('openInterest',0) or 0)})
        rows.sort(key=lambda x: x['delta'], reverse=True)
        return {"expiry": exp_str, "dte": dte, "spot": round(spot,2),
                "source": "yfinance (15min delay)", "options": rows}
    except Exception as e:
        print(f"yfinance Fehler: {e}")
        return None

# ══════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════
@app.route('/')
def index():
    return jsonify({
        "status":   "MSTR Brain API v2.1 ✅",
        "polygon":  "aktiv" if POLYGON_KEY else "kein Key — yfinance Fallback",
        "telegram": "aktiv" if TG_BOT_TOKEN else "nicht konfiguriert",
        "endpoints": ["/all", "/mstr", "/btc", "/fg", "/options", "/alarm"]
    })

@app.route('/mstr')
def get_mstr():
    def fetch():
        info = yf.Ticker("MSTR").fast_info
        p, pr = info.last_price, info.previous_close
        return {"price": round(p,2), "change_pct": round((p-pr)/pr*100,2) if pr else 0,
                "delayed": True, "as_of": datetime.utcnow().isoformat()+"Z"}
    try:
        return jsonify(cached('mstr', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/btc')
def get_btc():
    def fetch():
        r = req.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids":"bitcoin","vs_currencies":"usd",
                    "include_24hr_change":"true","include_7d_change":"true"}, timeout=8)
        d = r.json()["bitcoin"]
        return {"price": round(d["usd"]), "change_24h": round(d.get("usd_24h_change",0),2),
                "change_7d": round(d.get("usd_7d_change",0),2),
                "as_of": datetime.utcnow().isoformat()+"Z"}
    try:
        return jsonify(cached('btc', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fg')
def get_fg():
    def fetch():
        data = req.get("https://api.alternative.me/fng/?limit=2", timeout=8).json()["data"]
        return {"value": int(data[0]["value"]), "label": data[0]["value_classification"],
                "yesterday": int(data[1]["value"]) if len(data)>1 else None,
                "as_of": datetime.utcnow().isoformat()+"Z"}
    try:
        return jsonify(cached('fg', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/options')
def get_options():
    def fetch():
        spot   = yf.Ticker("MSTR").fast_info.last_price
        result = fetch_options_polygon(spot) if POLYGON_KEY else None
        if not result:
            result = fetch_options_yfinance(spot)
        if not result:
            return {"error": "Keine Optionsdaten verfügbar"}
        result["as_of"] = datetime.utcnow().isoformat()+"Z"
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

        # MSTR
        try:
            info = yf.Ticker("MSTR").fast_info
            p, pr = info.last_price, info.previous_close
            spot  = p
            out['mstr'] = {"price": round(p,2),
                           "change_pct": round((p-pr)/pr*100,2) if pr else 0,
                           "delayed": True}
        except Exception as e:
            out['mstr'] = {"error": str(e)}

        # BTC
        try:
            r   = req.get("https://api.coingecko.com/api/v3/simple/price",
                  params={"ids":"bitcoin","vs_currencies":"usd",
                          "include_24hr_change":"true","include_7d_change":"true"}, timeout=8)
            btc = r.json()["bitcoin"]
            out['btc'] = {"price": round(btc["usd"]),
                          "change_24h": round(btc.get("usd_24h_change",0),2),
                          "change_7d":  round(btc.get("usd_7d_change",0),2)}
        except Exception as e:
            out['btc'] = {"error": str(e)}

        # Fear & Greed
        try:
            fg = req.get("https://api.alternative.me/fng/?limit=2", timeout=8).json()["data"]
            out['fg'] = {"value": int(fg[0]["value"]), "label": fg[0]["value_classification"],
                         "yesterday": int(fg[1]["value"]) if len(fg)>1 else None}
        except Exception as e:
            out['fg'] = {"error": str(e)}

        # Optionen
        try:
            opts = fetch_options_polygon(spot) if POLYGON_KEY else None
            if not opts:
                opts = fetch_options_yfinance(spot)
            out['options'] = opts or {"error": "Keine Daten"}
        except Exception as e:
            out['options'] = {"error": str(e)}

        out['as_of'] = datetime.utcnow().isoformat()+"Z"
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
    btc_fmt = f"${int(btc):,}".replace(',', '.') if isinstance(btc, (int,float)) and btc else f"${btc}"

    if not can_alarm(t):
        return jsonify({"status": "cooldown"}), 200

    if t == 'roll':
        msg = (f"⚡ <b>MSTR BRAIN — JETZT ROLLEN!</b>\n🕐 {ts}\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"📈 MSTR:   <b>${mstr}</b>\n🎯 Strike: <b>${strike}</b>\n"
               f"⚠️ Puffer: <b>{buf}%</b> — unter 6%!\n📅 DTE: <b>{dte} Tage</b>\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n<b>Jetzt handeln:</b>\n"
               f"① Buy-to-Close: Call ${strike}\n"
               f"② Sell-to-Open: <b>${ns}</b> · Δ{delta} · 42 Tage\n"
               f"③ NUR für Kredit! Kein Debit!\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n🌍 F&G: {fg} · BTC: {btc_fmt}")
    elif t == 'warn':
        msg = (f"⚠️ <b>MSTR BRAIN — Strike nähert sich</b>\n🕐 {ts}\n"
               f"━━━━━━━━━━━━━━━━━━━━━\n"
               f"📈 MSTR: <b>${mstr}</b>\n🎯 Strike: <b>${strike}</b>\n"
               f"📊 Puffer: <b>{buf}%</b> — unter 12%\n📅 DTE: <b>{dte} Tage</b>\n"
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
               f"📈 MSTR: <b>${mstr}</b>\n🎯 Strike: <b>${strike}</b>\n"
               f"💵 Kassiert: <b>${prem}/Aktie</b>\n📉 Aktuell: <b>${cur}/Aktie</b>\n"
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
# MORGEN-BRIEFING (08:00 UTC)
# ══════════════════════════════════════════
def send_morning_briefing():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        info  = yf.Ticker("MSTR").fast_info
        mstr  = round(info.last_price, 2)
        prev  = info.previous_close
        mchg  = round((mstr-prev)/prev*100, 2) if prev else 0
        r     = req.get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids":"bitcoin","vs_currencies":"usd","include_24hr_change":"true"}, timeout=8)
        btcd  = r.json()["bitcoin"]
        btc   = round(btcd["usd"])
        bchg  = round(btcd.get("usd_24h_change",0), 2)
        fgd   = req.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()["data"][0]
        fg, fgl = int(fgd["value"]), fgd["value_classification"]
        ts    = datetime.utcnow().strftime('%d.%m.%Y')
        btc_f = f"${btc:,}".replace(',','.')
        msg   = (f"☀️ <b>MSTR Brain — Morgen-Briefing {ts}</b>\n"
                 f"━━━━━━━━━━━━━━━━━━━━━\n"
                 f"📈 MSTR: <b>${mstr}</b>  {'+' if mchg>=0 else ''}{mchg}%\n"
                 f"₿  BTC:  <b>{btc_f}</b>  {'+' if bchg>=0 else ''}{bchg}%\n"
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
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

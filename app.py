"""
MSTR Brain API Server v3.0
- MSTR Preis:   CoinGecko MSTRX token (tokenisierter MSTR)
- Bitcoin:      CoinGecko (live)
- Fear & Greed: alternative.me (live)
- Optionskette: Black-Scholes Schätzung (kein externer Service nötig)
- Telegram:     Roll, Warnung, Gewinnmitnahme, Morgen-Briefing 08:00 UTC
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests as req
from datetime import datetime, timedelta
import math, time, os, threading, schedule

app = Flask(__name__)
CORS(app)

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

# ─── Black-Scholes ───
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_delta(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

def bs_price(S, K, T, sigma, r=0.05):
    if T <= 0 or sigma <= 0:
        return max(0, S - K)
    d1 = (math.log(S / K) + r * T + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def strike_from_delta(S, delta, T, sigma):
    lo, hi = S * 0.5, S * 2.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if bs_delta(S, mid, T, sigma) > delta:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 0)

# ══════════════════════════════════════════
# MSTR PREIS — CoinGecko MSTRX
# ══════════════════════════════════════════
def fetch_mstr_price():
    """MSTR via tokenisierten MSTRX Token auf CoinGecko"""
    # Versuch 1: MSTRX (MicroStrategy xStock)
    try:
        r = req.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "microstrategy-xstock",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            timeout=8)
        d = r.json().get("microstrategy-xstock", {})
        price = float(d.get("usd", 0))
        if price > 10:
            return {"price": round(price, 2),
                    "change_pct": round(float(d.get("usd_24h_change", 0)), 2),
                    "source": "mstrx"}
    except Exception as e:
        print(f"MSTRX Fehler: {e}")

    # Versuch 2: BMSTR (Backed MicroStrategy)
    try:
        r = req.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "backed-microstrategy",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            timeout=8)
        d = r.json().get("backed-microstrategy", {})
        price = float(d.get("usd", 0))
        if price > 10:
            return {"price": round(price, 2),
                    "change_pct": round(float(d.get("usd_24h_change", 0)), 2),
                    "source": "bmstr"}
    except Exception as e:
        print(f"BMSTR Fehler: {e}")

    return None

# ══════════════════════════════════════════
# OPTIONSKETTE — Black-Scholes
# ══════════════════════════════════════════
def build_options_chain(spot, iv=0.88, target_dte=42):
    """Generiert Optionskette via Black-Scholes für Delta 0.03–0.22"""
    today    = datetime.today()
    exp_date = today + timedelta(days=target_dte)
    # Runde auf nächsten Freitag
    days_to_fri = (4 - exp_date.weekday()) % 7
    exp_date = exp_date + timedelta(days=days_to_fri)
    exp_str  = exp_date.strftime("%Y-%m-%d")
    dte      = (exp_date - today).days
    T        = dte / 365.0

    rows = []
    # Generiere Strikes von OTM bis weit OTM
    for otm_pct in [i * 2.5 for i in range(2, 30)]:
        K = round(spot * (1 + otm_pct / 100) / 5) * 5  # Runde auf $5
        delta = round(bs_delta(spot, K, T, iv), 3)
        if delta < 0.03 or delta > 0.22:
            continue
        price_bs = bs_price(spot, K, T, iv)
        bid  = round(price_bs * 0.95, 2)
        ask  = round(price_bs * 1.05, 2)
        mid  = round(price_bs, 2)
        rows.append({
            "strike":        K,
            "delta":         delta,
            "bid":           bid,
            "ask":           ask,
            "mid":           mid,
            "iv":            round(iv * 100, 1),
            "dte":           dte,
            "otm_pct":       round(otm_pct, 1),
            "volume":        0,
            "open_interest": 0,
        })

    # Dedupliziere nach Strike, behalte bestes Delta
    seen = {}
    for row in rows:
        k = row["strike"]
        if k not in seen or abs(row["delta"] - 0.08) < abs(seen[k]["delta"] - 0.08):
            seen[k] = row
    rows = sorted(seen.values(), key=lambda x: x["delta"], reverse=True)

    return {
        "expiry":  exp_str,
        "dte":     dte,
        "spot":    round(spot, 2),
        "source":  "black-scholes (kein Live-Feed verfügbar)",
        "iv_used": round(iv * 100, 1),
        "options": rows
    }

# ══════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════
@app.route('/')
def index():
    mstr = fetch_mstr_price()
    return jsonify({
        "status":   "MSTR Brain API v3.0 ✅",
        "mstr":     f"${mstr['price']} via {mstr['source']}" if mstr else "kein Preis",
        "telegram": "aktiv" if TG_BOT_TOKEN else "nicht konfiguriert",
        "endpoints": ["/all", "/mstr", "/btc", "/fg", "/options", "/alarm"]
    })

@app.route('/mstr')
def get_mstr():
    def fetch():
        q = fetch_mstr_price()
        if not q:
            return {"error": "MSTR Preis nicht verfügbar (Markt geschlossen?)"}
        return {**q, "as_of": datetime.utcnow().isoformat() + "Z"}
    try:
        return jsonify(cached('mstr', fetch, ttl=60))
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
        q    = fetch_mstr_price()
        spot = q["price"] if q else 150.0
        iv   = float(req.get("https://api.alternative.me/fng/?limit=1",
               timeout=5).json()["data"][0]["value"]) / 100 * 1.5 + 0.5
        # IV grob aus F&G schätzen: Fear=hoch, Greed=tief
        iv   = max(0.60, min(1.40, iv))
        result = build_options_chain(spot, iv=iv)
        result["as_of"] = datetime.utcnow().isoformat() + "Z"
        return result
    try:
        return jsonify(cached('options', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/all')
def get_all():
    def fetch_all():
        out = {}

        # MSTR
        try:
            q = fetch_mstr_price()
            if q:
                out['mstr'] = {**q}
                spot = q["price"]
            else:
                out['mstr'] = {"error": "nicht verfügbar"}
                spot = 150.0
        except Exception as e:
            out['mstr'] = {"error": str(e)}
            spot = 150.0

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
        fg_val = 43
        try:
            fg = req.get("https://api.alternative.me/fng/?limit=2", timeout=8).json()["data"]
            fg_val = int(fg[0]["value"])
            out['fg'] = {"value": fg_val, "label": fg[0]["value_classification"],
                         "yesterday": int(fg[1]["value"]) if len(fg) > 1 else None}
        except Exception as e:
            out['fg'] = {"error": str(e)}

        # Optionen via BS
        try:
            iv = max(0.60, min(1.40, fg_val / 100 * 1.5 + 0.5))
            out['options'] = build_options_chain(spot, iv=iv)
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
    d = request.json or {}
    t = d.get('type','unknown'); mstr=d.get('mstr','?'); strike=d.get('strike','?')
    buf=d.get('buffer','?'); dte=d.get('dte','?'); delta=d.get('delta','?')
    ns=d.get('new_strike','?'); prem=d.get('premium','?'); cur=d.get('current_val','?')
    fg=d.get('fg','?'); btc=d.get('btc',0)
    ts = datetime.utcnow().strftime('%d.%m. %H:%M UTC')
    btc_fmt = f"${int(btc):,}".replace(',','.') if isinstance(btc,(int,float)) and btc else f"${btc}"
    if not can_alarm(t): return jsonify({"status":"cooldown"}), 200
    if t=='roll':
        msg=(f"⚡ <b>MSTR BRAIN — JETZT ROLLEN!</b>\n🕐 {ts}\n━━━━━━━━━━━━━━━━━━━━━\n"
             f"📈 MSTR: <b>${mstr}</b>\n🎯 Strike: <b>${strike}</b>\n"
             f"⚠️ Puffer: <b>{buf}%</b> — unter 6%!\n📅 DTE: <b>{dte} Tage</b>\n"
             f"━━━━━━━━━━━━━━━━━━━━━\n① Buy-to-Close: Call ${strike}\n"
             f"② Sell-to-Open: <b>${ns}</b> · Δ{delta} · 42 Tage\n③ NUR für Kredit!\n"
             f"━━━━━━━━━━━━━━━━━━━━━\n🌍 F&G: {fg} · BTC: {btc_fmt}")
    elif t=='warn':
        msg=(f"⚠️ <b>MSTR BRAIN — Strike nähert sich</b>\n🕐 {ts}\n━━━━━━━━━━━━━━━━━━━━━\n"
             f"📈 MSTR: <b>${mstr}</b>\n🎯 Strike: <b>${strike}</b>\n"
             f"📊 Puffer: <b>{buf}%</b> — unter 12%\n📅 DTE: <b>{dte} Tage</b>\n"
             f"━━━━━━━━━━━━━━━━━━━━━\n👀 Roll auf ${ns} vorbereiten · Δ{delta}\n"
             f"🌍 F&G: {fg} · BTC: {btc_fmt}")
    elif t=='profit':
        try: gewinn=str(round((float(prem)-float(cur))*200))
        except: gewinn='?'
        msg=(f"💰 <b>MSTR BRAIN — Gewinnmitnahme!</b>\n🕐 {ts}\n━━━━━━━━━━━━━━━━━━━━━\n"
             f"📈 MSTR: <b>${mstr}</b>\n🎯 Strike: <b>${strike}</b>\n"
             f"💵 Kassiert: <b>${prem}/Aktie</b>\n📉 Aktuell: <b>${cur}/Aktie</b>\n"
             f"✅ 75%+ Zeitwert verloren!\n━━━━━━━━━━━━━━━━━━━━━\n"
             f"① BTC ~${cur}/Aktie\n② Gewinn: ~<b>${gewinn}</b>\n"
             f"③ Neuer Zyklus: ${ns} · Δ{delta} · 42 Tage")
    else:
        msg=f"🔔 MSTR Brain: {t}\nMSTR: ${mstr} · Strike: ${strike}"
    ok = tg_send(msg)
    return jsonify({"status": "sent" if ok else "error", "type": t})

# ══════════════════════════════════════════
# MORGEN-BRIEFING 08:00 UTC
# ══════════════════════════════════════════
def send_morning_briefing():
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    try:
        q    = fetch_mstr_price()
        mstr = q["price"] if q else 0
        mchg = q.get("change_pct", 0) if q else 0
        r    = req.get("https://api.coingecko.com/api/v3/simple/price",
               params={"ids":"bitcoin","vs_currencies":"usd","include_24hr_change":"true"}, timeout=8)
        btcd = r.json()["bitcoin"]; btc=round(btcd["usd"]); bchg=round(btcd.get("usd_24h_change",0),2)
        fgd  = req.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()["data"][0]
        fg,fgl = int(fgd["value"]),fgd["value_classification"]
        ts = datetime.utcnow().strftime('%d.%m.%Y')
        msg=(f"☀️ <b>MSTR Brain — Morgen-Briefing {ts}</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
             f"📈 MSTR: <b>${mstr}</b>  {'+' if mchg>=0 else ''}{mchg}%\n"
             f"₿  BTC:  <b>${btc:,}</b>  {'+' if bchg>=0 else ''}{bchg}%\n"
             f"😱 F&G:  <b>{fg}</b> — {fgl}\n━━━━━━━━━━━━━━━━━━━━━\nApp öffnen 📱")
        tg_send(msg)
        print(f"Morgen-Briefing gesendet {ts}")
    except Exception as e: print(f"Morgen-Briefing Fehler: {e}")

def run_scheduler():
    schedule.every().day.at("08:00").do(send_morning_briefing)
    while True: schedule.run_pending(); time.sleep(30)

if __name__ == '__main__':
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

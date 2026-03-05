"""
MSTR Brain API Server
Läuft auf Render.com (kostenlos) und liefert:
- MSTR Aktienpreis (15min delayed via yfinance)
- Bitcoin Preis (live via CoinGecko)
- MSTR Optionskette (~42 DTE, Calls, Delta 0.05-0.12)
- Fear & Greed Index
"""

from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf
import requests
from datetime import datetime, timedelta
import math
import time

app = Flask(__name__)
CORS(app)  # Erlaubt Zugriff vom Browser (deine HTML-App)

# ─── Cache (vermeidet zu viele API-Calls) ───
_cache = {}
CACHE_TTL = 300  # 5 Minuten

def cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache[key]['ts'] < CACHE_TTL:
        return _cache[key]['data']
    data = fn()
    _cache[key] = {'data': data, 'ts': now}
    return data

# ─── Hilfsfunktionen ───

def norm_cdf(x):
    """Kumulative Normalverteilung (für Delta-Berechnung)"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_delta(S, K, T, sigma):
    """Black-Scholes Call Delta"""
    if T <= 0 or sigma <= 0:
        return 0
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

def bs_price(S, K, T, sigma, r=0.05):
    """Black-Scholes Call Preis"""
    if T <= 0 or sigma <= 0:
        return max(0, S - K)
    d1 = (math.log(S / K) + r * T + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def find_expiry_near_dte(expirations, target_dte=42):
    """Wähle Expiry am nächsten zu target_dte"""
    today = datetime.today()
    best = None
    best_diff = 9999
    for exp_str in expirations:
        exp = datetime.strptime(exp_str, "%Y-%m-%d")
        dte = (exp - today).days
        if dte < 14:
            continue  # zu nah
        diff = abs(dte - target_dte)
        if diff < best_diff:
            best_diff = diff
            best = exp_str
    return best

# ─── ENDPOINTS ───

@app.route('/')
def index():
    return jsonify({
        "status": "MSTR Brain API läuft ✅",
        "endpoints": ["/mstr", "/btc", "/options", "/fg", "/all"]
    })

@app.route('/mstr')
def get_mstr():
    def fetch():
        ticker = yf.Ticker("MSTR")
        info = ticker.fast_info
        price = info.last_price
        prev_close = info.previous_close
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
        return {
            "price": round(price, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(change_pct, 2),
            "currency": "USD",
            "delayed": True,
            "as_of": datetime.utcnow().isoformat() + "Z"
        }
    try:
        return jsonify(cached('mstr', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/btc')
def get_btc():
    def fetch():
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_7d_change": "true"},
            timeout=8
        )
        data = r.json()["bitcoin"]
        return {
            "price": round(data["usd"]),
            "change_24h": round(data.get("usd_24h_change", 0), 2),
            "change_7d": round(data.get("usd_7d_change", 0), 2),
            "as_of": datetime.utcnow().isoformat() + "Z"
        }
    try:
        return jsonify(cached('btc', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fg')
def get_fg():
    def fetch():
        r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=8)
        data = r.json()["data"]
        return {
            "value": int(data[0]["value"]),
            "label": data[0]["value_classification"],
            "yesterday": int(data[1]["value"]) if len(data) > 1 else None,
            "as_of": datetime.utcnow().isoformat() + "Z"
        }
    try:
        return jsonify(cached('fg', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/options')
def get_options():
    def fetch():
        ticker = yf.Ticker("MSTR")
        expirations = ticker.options
        if not expirations:
            return {"error": "Keine Optionen gefunden"}

        exp_str = find_expiry_near_dte(expirations, target_dte=42)
        if not exp_str:
            exp_str = expirations[0]

        chain = ticker.option_chain(exp_str)
        calls = chain.calls

        # Aktueller MSTR-Preis
        spot = ticker.fast_info.last_price

        exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
        dte = (exp_date - datetime.today()).days
        T = dte / 365.0

        # IV aus ATM-Option schätzen (für Delta-Berechnung)
        atm_iv = None
        atm_calls = calls[(calls['strike'] >= spot * 0.95) & (calls['strike'] <= spot * 1.05)]
        if not atm_calls.empty:
            atm_iv = float(atm_calls['impliedVolatility'].mean())

        result_rows = []
        for _, row in calls.iterrows():
            strike = float(row['strike'])
            bid = float(row['bid']) if row['bid'] > 0 else None
            ask = float(row['ask']) if row['ask'] > 0 else None
            mid = round((bid + ask) / 2, 2) if bid and ask else None
            iv = float(row['impliedVolatility']) if row['impliedVolatility'] > 0 else (atm_iv or 0.88)
            delta = round(bs_delta(spot, strike, T, iv), 3)
            otm_pct = round((strike - spot) / spot * 100, 1)

            # Nur OTM Calls mit Delta 0.03–0.20
            if delta < 0.03 or delta > 0.20:
                continue

            result_rows.append({
                "strike": strike,
                "delta": delta,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "iv": round(iv * 100, 1),
                "dte": dte,
                "otm_pct": otm_pct,
                "volume": int(row.get('volume', 0) or 0),
                "open_interest": int(row.get('openInterest', 0) or 0),
            })

        # Sortiere nach Delta absteigend (0.12 → 0.05)
        result_rows.sort(key=lambda x: x['delta'], reverse=True)

        return {
            "expiry": exp_str,
            "dte": dte,
            "spot": round(spot, 2),
            "options": result_rows,
            "delayed": True,
            "as_of": datetime.utcnow().isoformat() + "Z"
        }

    try:
        return jsonify(cached('options', fetch))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/all')
def get_all():
    """Alle Daten in einem Call — reduziert Latenz für die App"""
    def fetch_all():
        results = {}

        # MSTR
        try:
            ticker = yf.Ticker("MSTR")
            info = ticker.fast_info
            price = info.last_price
            prev = info.previous_close
            results['mstr'] = {
                "price": round(price, 2),
                "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                "delayed": True
            }
        except Exception as e:
            results['mstr'] = {"error": str(e)}

        # BTC
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd",
                        "include_24hr_change": "true", "include_7d_change": "true"},
                timeout=8
            )
            btc = r.json()["bitcoin"]
            results['btc'] = {
                "price": round(btc["usd"]),
                "change_24h": round(btc.get("usd_24h_change", 0), 2),
                "change_7d": round(btc.get("usd_7d_change", 0), 2),
            }
        except Exception as e:
            results['btc'] = {"error": str(e)}

        # Fear & Greed
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=8)
            fg = r.json()["data"]
            results['fg'] = {
                "value": int(fg[0]["value"]),
                "label": fg[0]["value_classification"],
                "yesterday": int(fg[1]["value"]) if len(fg) > 1 else None,
            }
        except Exception as e:
            results['fg'] = {"error": str(e)}

        # Options
        try:
            ticker = yf.Ticker("MSTR")
            expirations = ticker.options
            exp_str = find_expiry_near_dte(expirations, 42)
            chain = ticker.option_chain(exp_str)
            calls = chain.calls
            spot = results['mstr'].get('price', ticker.fast_info.last_price)
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
            dte = (exp_date - datetime.today()).days
            T = dte / 365.0

            rows = []
            for _, row in calls.iterrows():
                strike = float(row['strike'])
                bid = float(row['bid']) if row['bid'] > 0 else None
                ask = float(row['ask']) if row['ask'] > 0 else None
                mid = round((bid + ask) / 2, 2) if bid and ask else None
                iv = float(row['impliedVolatility']) if row['impliedVolatility'] > 0 else 0.88
                delta = round(bs_delta(spot, strike, T, iv), 3)
                if delta < 0.03 or delta > 0.20:
                    continue
                rows.append({
                    "strike": strike, "delta": delta,
                    "bid": bid, "ask": ask, "mid": mid,
                    "iv": round(iv * 100, 1), "dte": dte,
                    "otm_pct": round((strike - spot) / spot * 100, 1),
                    "volume": int(row.get('volume', 0) or 0),
                    "open_interest": int(row.get('openInterest', 0) or 0),
                })
            rows.sort(key=lambda x: x['delta'], reverse=True)
            results['options'] = {"expiry": exp_str, "dte": dte, "spot": spot, "options": rows}
        except Exception as e:
            results['options'] = {"error": str(e)}

        results['as_of'] = datetime.utcnow().isoformat() + "Z"
        return results

    try:
        return jsonify(cached('all', fetch_all))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# MSTR Brain API v2.3 — Webull Setup

## Umgebungsvariablen in Render

| Variable | Wert |
|----------|------|
| `WB_EMAIL` | Deine Webull Email |
| `WB_PASSWORD` | Dein Webull Passwort |
| `TG_BOT_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | Telegram Chat ID |

## Webull Account erstellen (2 Min)
1. app.webull.com → Sign up
2. Nur Email + Passwort, keine weiteren Daten nötig
3. Email bestätigen
4. Email + Passwort als WB_EMAIL / WB_PASSWORD in Render eintragen

## Deploy
Start Command: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 60

## Test
https://dein-service.onrender.com/options
→ sollte "source": "webull" mit echten Bid/Ask zeigen

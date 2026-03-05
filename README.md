# MSTR Brain API — Render.com Deployment

## Einmalige Einrichtung (ca. 5 Minuten)

### Schritt 1 — GitHub Account
Falls noch nicht vorhanden: https://github.com → kostenlos registrieren

### Schritt 2 — Neues Repository erstellen
1. github.com → "New repository"
2. Name: `mstr-brain-api`
3. Public ✓ → "Create repository"
4. Die 3 Dateien hochladen (app.py, requirements.txt, render.yaml):
   - "Add file" → "Upload files" → alle 3 reinziehen → Commit

### Schritt 3 — Render Account
1. https://render.com → "Get Started for Free"
2. Mit GitHub-Account anmelden (einfacher)

### Schritt 4 — Service deployen
1. Render Dashboard → "New +" → "Web Service"
2. "Connect a repository" → mstr-brain-api wählen
3. Einstellungen werden aus render.yaml automatisch geladen
4. "Create Web Service" klicken
5. Warten ~2 Minuten bis Build fertig

### Schritt 5 — URL kopieren
Nach dem Deploy siehst du eine URL wie:
`https://mstr-brain-api.onrender.com`

Diese URL in die MSTR Brain HTML-App eintragen (Einstellungen → API URL).

## Endpoints

| URL | Inhalt |
|-----|--------|
| `/all` | Alles auf einmal (MSTR + BTC + F&G + Optionen) |
| `/mstr` | Nur MSTR Preis |
| `/btc` | Nur Bitcoin Preis |
| `/fg` | Nur Fear & Greed |
| `/options` | Nur Optionskette |

## Wichtige Hinweise

- **Kostenloser Render-Plan**: Server schläft nach 15 Min Inaktivität ein
  → Erster Aufruf am Morgen kann 30-60 Sekunden dauern (Kaltstart)
  → Danach normal schnell
- **Datenverzögerung**: MSTR-Preis und Optionen ~15 Minuten verzögert (yfinance)
- **BTC und F&G**: Live (keine Verzögerung)
- **Cache**: 5 Minuten — API wird nicht bei jedem App-Aufruf neu abgefragt

## Lokaler Test (optional)
```bash
pip install -r requirements.txt
python app.py
# → http://localhost:10000/all
```

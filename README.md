# Signal Board

Dashboard fuer CIB/ISD-Handelssignale (Close Is Beyond / Inside Day). Live-Board:
`public/signal-board.html`, live auf Cloudflare Pages unter
https://signal-board-vfw.pages.dev/ (Auto-Deploy bei jedem Push auf `main`).

## Architektur

**Frueher (bis 2026-09-24) lief das Board auf Netlify** (statisches Hosting +
`netlify/functions/yahoo.js` als serverseitiger Yahoo-Proxy, live aus dem
Browser aufgerufen -- Nachteil: jeder Board-Aufruf verbrauchte Netlify-
Function-Credits). **Netlify wurde komplett entfernt** -- Site, alle Deploys
und alle Netlify-spezifischen Dateien im Repo (`netlify.toml`,
`netlify/functions/`, das Cleanup-Skript, das seinen Zweck erfuellt hat).
Cloudflare Pages ist jetzt die einzige Hosting-Plattform.

### Cloudflare Pages, JSON direkt im Repo
Ziel ist, den Live-Fetch aus dem Browser durch eine vorab generierte JSON-Datei
zu ersetzen -- **ohne separaten Object-Storage-Dienst**, damit keine
Zahlungsdaten/Zusatz-Account (Cloudflare R2 verlangt eine hinterlegte
Zahlungsmethode selbst fuer den kostenlosen Tarif) und keine zusaetzlichen
API-Keys noetig sind:

```
GitHub Actions (Cron, taeglich)
  -> python/generate_signals.py
     holt Yahoo-Daten, klassifiziert (CIB/ISD/...), bucketed 16:59 NY
     schreibt public/data/signals.json
  -> Workflow committet + pusht die Datei (GITHUB_TOKEN, keine Secrets noetig)
Cloudflare Pages (auto-deploy bei jedem Push)
  -> liefert public/data/signals.json als ganz normale statische Datei aus,
     genau wie signal-board.html
Board (signal-board.html)
  -> laedt /data/signals.json (same-origin) statt live von Yahoo
```

Vorteil: kein Live-Fetch mehr pro Seitenaufruf, Board bleibt ein statisches
HTML/JS-File, kein zusaetzlicher Cloud-Dienst.

**Projektstruktur:**

```
signal-board/
|-- cloudflare/
|   |-- _headers          # Referenzkopie -- aktiv ist public/_headers
|   `-- _redirects        # Referenzkopie -- aktiv ist public/_redirects
|-- python/
|   |-- generate_signals.py   # Pipeline: Abruf, Buendelung, Klassifikation, JSON-Schreiben
|   `-- requirements.txt      # yfinance-cache, pandas
|-- .github/workflows/
|   `-- daily-signals.yml     # taeglicher Cron-Lauf, committet signals.json
|-- public/
|   |-- signal-board.html     # das Board (Layout/CSS unveraendert, JS auf JSON-Fetch umgestellt)
|   |-- data/signals.json     # wird von generate_signals.py erzeugt / von der Action committet
|   |-- fallback.json         # statische Notfalldaten, falls signals.json nicht ladbar ist
|   |-- manifest.json         # PWA-Manifest ("zum Homescreen hinzufuegen")
|   |-- service-worker.js     # Offline-Cache (stale-while-revalidate)
|   |-- icon-192.png          # PWA-Icon (Platzhalter, dunkler Hintergrund + Teal-Punkt)
|   |-- icon-512.png          # PWA-Icon (Platzhalter, dunkler Hintergrund + Teal-Punkt)
|   `-- _headers, _redirects  # aktive Cloudflare-Pages-Konfiguration
`-- README.md
```

## Migrations-Status

| Schritt | Wer | Was | Status |
|---|---|---|---|
| 1 | Claude Code | Wrangler, Cloudflare-Pages-Projekt, Struktur | ✅ erledigt |
| 2 | Claude Code | Architektur ohne R2 umgebaut (JSON direkt im Repo statt Object-Storage) | ✅ erledigt |
| 3 | Claude Code | Cloudflare Pages mit GitHub-Repo verbunden, live deployt | ✅ erledigt |
| 4 | Claude Code | Python-Pipeline (Instrumente, Buendelung, Klassifikation) + HTML auf JSON-Fetch umgestellt, PWA-Dateien | ✅ erledigt, lokal getestet |
| 5 | Du | `.github/workflows/daily-signals.yml` manuell ueber die GitHub-Weboberflaeche anlegen | ✅ erledigt (als `main.yml`) |
| 6 | Du | Netlify komplett loeschen (Site + alle Deploys + Repo-Reste) | ✅ erledigt |
| 7 | Du | Ersten automatischen naechtlichen Lauf (03:00 UTC) abwarten und bestaetigen | offen |

**Hinweis:** Der ursprünglich geplante R2-Schritt entfällt -- Cloudflare
verlangt für R2 eine hinterlegte Zahlungsmethode auch im kostenlosen Tarif.
Da die Signaldaten nur wenige KB gross sind, committet die GitHub Action sie
stattdessen direkt ins Repo; Cloudflare Pages liefert sie automatisch mit aus.
Keine Zahlungsdaten, keine zusätzlichen API-Keys nötig.

## Bekannte Probleme

- **Workflow-Dateien koennen von dieser Session nicht per `git push`/API
  angelegt werden** (fehlender `workflow`-OAuth-Scope, von GitHub hart
  durchgesetzt -- betrifft jeden Versuch, ob `git push` oder GitHub-Contents-
  API). Muss einmalig manuell ueber die GitHub-Weboberflaeche angelegt werden.
  Bereits erledigt (liegt dort als `main.yml`, Inhalt identisch zur lokalen
  `daily-signals.yml`).

## Zeitplan der taeglichen Pipeline

Der Workflow laeuft um **`0 3 * * *` (03:00 UTC)**. Herleitung:

- Referenz-Schlusskurs ist immer **16:59 NY**, Handelstag-Rollover ist **18:00 NY**.
- Die Signale sollen spaetestens **23:00 NY** verfuegbar sein.
- `03:00 UTC` entspricht **23:00 NY im Sommer** (EDT, UTC-4) bzw. **22:00 NY
  im Winter** (EST, UTC-5) -- beide Zeiten liegen sicher NACH dem 18:00-NY-
  Rollover und spaetestens bei der Zielzeit.
- Auf deutsche Zeit umgerechnet ist das **ganzjaehrig 05:00 DE**: Deutschland
  und New York stellen beide auf Sommerzeit um (nur zu leicht unterschiedlichen
  Terminen im Fruehjahr/Herbst), der Versatz zwischen ihnen bleibt praktisch
  konstant bei 6 Stunden (Sommer) bzw. 6 Stunden (Winter) -- anders als beim
  vorherigen `22:00 UTC`-Ansatz, der im Winter VOR dem Rollover gelegen haette.

**Rollover-Check als Sicherheitsnetz:** `generate_signals.py` prueft vor jedem
Lauf die aktuelle NY-Zeit (`check_rollover()`). Liegt sie vor 18:00 NY, bricht
das Skript mit einer klaren Fehlermeldung ab, statt versehentlich einen noch
laufenden Handelstag als "gestern" zu behandeln. Fuer manuelle Testlaeufe vor
18:00 NY: `python generate_signals.py --force` (ueberspringt den Check). Das
ist ein grober globaler Vorab-Check -- die praezise Pruefung pro Instrument
(`bucket_complete()`) bleibt zusaetzlich bestehen.

## Cloudflare Setup

### Wrangler CLI

```bash
npm i -g wrangler
wrangler login
```

`wrangler login` oeffnet den Browser fuer den Cloudflare-OAuth-Login -- die
Anmeldung selbst bestaetigst du im Browser.

Cloudflare Pages Projekt wurde bereits angelegt:

```bash
wrangler pages project list
```

zeigt `signal-board` (Domain `signal-board-vfw.pages.dev`).

### Cloudflare Pages mit dem Repo verbinden (manuell, im Dashboard)

Kein R2, keine API-Token, keine GitHub Secrets noetig -- nur eine einmalige
Verknuepfung, damit Cloudflare Pages bei jedem Push automatisch neu deployt:

1. Cloudflare Dashboard -> Workers & Pages -> Projekt **`signal-board`** oeffnen
2. Tab **Settings** -> **Builds & deployments** -> **Connect to Git** (bzw.
   beim ersten Deploy fragt Cloudflare direkt danach)
3. Repo `hubhub8/signal-board` auswaehlen, Branch `main`
4. Build-Einstellungen: kein Build-Command noetig (statisches HTML),
   **Build output directory**: `public`
5. Speichern -> ab jetzt deployt Cloudflare Pages automatisch bei jedem Push
   auf `main`

## Python-Pipeline

```bash
cd python
pip install -r requirements.txt
python generate_signals.py --force
```

`--force` ist noetig, wenn NY-Zeit gerade vor 18:00 liegt (Rollover-Check,
siehe unten) -- ohne `--force` bricht das Skript dann bewusst mit einer
Fehlermeldung ab, statt einen noch laufenden Handelstag auszuwerten.

Schreibt `public/data/signals.json`. Kann sowohl aus `python/` (`python
generate_signals.py`) als auch vom Repo-Root (`python python/generate_signals.py`,
so ruft es auch die GitHub Action auf) gestartet werden -- Pfade werden relativ
zum Skript selbst aufgeloest, nicht zum Arbeitsverzeichnis.

Nicht erreichbare Instrumente (z.B. Yahoo-Ausfall) werden einzeln uebersprungen
(3 Versuche mit steigender Wartezeit) und als Warnung ausgegeben, statt den
ganzen Lauf abzubrechen -- das Board zeigt dann einfach ein Instrument weniger.
Ebenso normal: ein Instrument kann in `signals.json` fehlen, weil es korrekt
als `NONE` klassifiziert wurde (Level beruehrt, aber kein Pump/Dump-Vorlauf --
wird laut Spezifikation nicht ausgegeben, das ist kein Fehler).

`yfinance-cache` legt einen Plattencache an (Windows:
`%LOCALAPPDATA%\py-yfinance-cache`, Linux/Mac: `~/.cache/py-yfinance-cache`).
Bei wiederholten lokalen Testlaeufen mit bereits bestehendem Cache kann ein
interner Bug der Bibliothek bei der Cache-Ablauf-Pruefung auftreten
(`cannot access local variable 'idx0'`) -- betrifft nur lokale Wiederholungs-
laeufe, nicht den produktiven GitHub-Actions-Lauf (dort ist die VM taeglich
frisch, kein bestehender Cache). Bei Bedarf lokal beheben: Cache-Ordner
loeschen und erneut ausfuehren.

### HTML lokal testen

```bash
cd public
python -m http.server 8000
```

`http://localhost:8000/signal-board.html` oeffnen. Um den Fallback-Pfad zu
testen: `data/signals.json` kurz umbenennen (oder den Ordner `data/` verschieben)
und die Seite neu laden -- das Badge sollte auf "STATIC" wechseln und die
Werte aus `fallback.json` zeigen.

## Netlify

Vollstaendig entfernt (2026-09-24): Site `signal-board-hubhub8`, alle 22
Deploys und alle Netlify-spezifischen Dateien im Repo (`netlify.toml`,
`netlify/functions/`, das einmalig benutzte Cleanup-Skript) sind geloescht.
Cloudflare Pages ist die einzige verbleibende Hosting-Plattform.

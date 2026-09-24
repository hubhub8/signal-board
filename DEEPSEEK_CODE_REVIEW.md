# Code-Review-Auftrag für DeepSeek — python/generate_signals.py

**Kontext:** Dieses Dokument ist eigenständig lesbar — du hast keinen Zugriff auf das
Projekt selbst, nur auf das, was hier steht. Bitte den Code unten auf
Korrektheitsfehler, Edge Cases und Logikfehler prüfen (keine Stilfragen, keine
Refactoring-Vorschläge, außer sie betreffen echte Bugs).

## 1. Was das Projekt ist

„Signal Board" ist ein Handels-Signalboard für eine professionelle Traderin. Es
klassifiziert 15 Instrumente einmal pro Handelstag nach einer CIB/ISD/FRD/FGD/
OUTSIDE-Methodik und zeigt das Ergebnis als Kachel-Board an, das sie täglich auf
dem Handy öffnet — das hier zu prüfende Skript liefert die Zahlen, ein separates
HTML/JS-Frontend (nicht Teil dieses Reviews) rendert sie nur noch.

- Läuft einmal täglich per GitHub Action, schreibt `public/data/signals.json`.
- Datenquelle: Yahoo Finance über die Python-Bibliothek `yfinance-cache`
  (Stundenkerzen, `interval=60m`, `period=3mo`).

## 2. Die maßgebliche Handelstag-Konvention

- Ein Handelstag läuft von **18:00 New Yorker Zeit bis 18:00 New Yorker Zeit**
  (Rollover = CME-Globex-Wiedereröffnung nach der täglichen Wartungspause).
- Der **maßgebliche Schlusskurs** eines Handelstags ist der Kurs um **16:59
  New Yorker Zeit** (bzw. der letzte verfügbare Balken bis einschließlich
  dieser Minute), unabhängig vom Instrumententyp.
- Diese Konvention ist historisch hart erarbeitet: eine frühere Version dieses
  Projekts verglich rohe Yahoo-Tagesbalken direkt zwischen verschiedenen
  Instrumenten und bekam an manchen Tagen widersprüchliche Signale, weil
  Yahoos natives Tagesbalken-Ende nicht bei allen Instrumenten gleich liegt.
  Seitdem wird **grundsätzlich selbst aus Stundenkerzen gebündelt**, nie der
  native Tagesbalken direkt vertraut — **mit einer bewussten, eng gefassten
  Ausnahme** (siehe Abschnitt 4, `DAILY_CLOSE_OVERRIDE_TICKERS`).

## 3. Klassifikationsregeln (klassisches Verhalten, unverändert)

Pro Instrument werden verglichen: heutiges Open/High/Low/Close (`o,h,l,c`)
gegen gestriges High/Low (`pH,pL`), sowie optional Vorwochen-High/Low
(`pwH,pwL`) und Vormonats-High/Low (`pmH,pmL`).

- **CIB** (Close Is Beyond): `c > pH` → CIB ▲, `c < pL` → CIB ▼.
- **ISD** (Inside Day): `c` liegt in `[pL, pH]` UND kein Level wurde berührt
  (weder PDH/PDL noch PWH/PWL/PMH/PML).
- **FRD/FGD**: mindestens ein Level wurde berührt (z.B. `h > pH` oder
  `l < pL`), aber der Schluss kehrt zurück in `[pL, pH]`. Kandidat ist FRD
  wenn `c < o`, sonst FGD. Der Kandidat wird nur bestätigt, wenn der
  **Vortag selbst** ein "up"-Tag war (für FRD) bzw. ein "down"-Tag (für FGD)
  — "up"/"down" eines Tages bedeutet: dessen eigener Schluss lag über dem
  High des Tages davor (up) bzw. unter dessen Low (down). Fehlt dieser
  Vorlauf, ist das Ergebnis `NONE` und wird NICHT ausgegeben (kein
  Fallback auf ISD).
- **Badges** (nur für CIB-Tage): HCOM/LCOM (höchster/tiefster CIB-Schluss des
  Monats), HCOW/LCOW (der Woche), plus PWH/PWL/PMH/PML falls die jeweilige
  Vorwochen-/Vormonatsgrenze auch berührt wurde. `wk`/`mo` sind Listen der
  Schlusskurse aller Tage der laufenden Woche/des laufenden Monats **die
  selbst ein CIB-Tag waren** (dir != null), vor dem heutigen Tag.

## 4. NEU (heute in dieser Session hinzugefügt) — bitte besonders kritisch prüfen

### 4a. OUTSIDE als eigene Kategorie

Neue Regel, von der Auftraggeberin heute explizit so festgelegt: Wenn sowohl
`h > pH` ALS AUCH `l < pL` (beide Vortagesgrenzen wurden getriggert — ein
"Outside Day"), ist die Kategorie **immer** `OUTSIDE`, unabhängig davon wo der
Schluss landet — das hat **Vorrang vor CIB**, auch wenn der Schluss klar
jenseits eines Levels liegt. OUTSIDE hat keine Richtung (`dir` bleibt 0) und
bekommt keine Badges (Badges sind weiterhin CIB-exklusiv).

**Konkret zu prüfen:** Ist diese Priorisierung (OUTSIDE übersteuert CIB
komplett) in sich konsistent? Gibt es einen Fall, in dem das zu einem
kontraintuitiven Ergebnis führt (z.B. ein Instrument, das klar und deutlich
nach oben ausbricht, aber trotzdem nur als neutrales OUTSIDE ohne Richtung
gezeigt wird, obwohl der Ausbruch der eigentlich relevante Fakt wäre)?

### 4b. DAILY_CLOSE_OVERRIDE_TICKERS (DAX-Schlusskurs-Fix)

Konkret gefundener Bug heute: `^GDAXI` (DAX) handelt an der Xetra, deren
Sitzung (ca. 09:00–17:30 CET = ca. 03:00–11:30 EDT) lange vor 16:59 NY endet.
Yahoos **Stundenbalken** für `^GDAXI` enden mit dem letzten Balken der
regulären fortlaufenden Sitzung und erfassen die tatsächliche
**Schlussauktion** (die den amtlichen Tagesschluss bestimmt) NICHT. Das ergab
am 23.09.2026 einen um 16 Punkte falschen Schlusskurs (25426,55 aus dem
Stundenbalken-Fallback statt echtem Xetra-Schluss 25410,63 aus Yahoos eigenem
Tagesbalken) — in diesem konkreten Fall genug, um die Kategorie zu kippen
(fälschlich `NONE`/ausgeblendet statt korrekt `CIB`).

**Fix:** Für Ticker in `DAILY_CLOSE_OVERRIDE_TICKERS` (aktuell nur `^GDAXI`)
wird zusätzlich ein `interval=1d`-Abruf gemacht, und dessen `Close`-Wert
ersetzt NUR den Schlusskurs (nicht High/Low/Open, nicht die
Handelstag-Zuordnung) — siehe `fetch_daily_closes()` und der Override-Block
in `bucket_hourly()`.

**Konkret zu prüfen:**
- Ist der Date-Matching-Mechanismus robust? `bucket_hourly()` sammelt pro
  Handelstag-Bucket die Menge der `local_date`-Werte (das Datum JEDES
  Stundenbalkens in seiner ORIGINALEN Börsenzeitzone, siehe `local_date` in
  `fetch_hour_bars()`) und übernimmt den Override nur, wenn diese Menge genau
  EIN Datum enthält (`if len(local_dates) == 1`). Ist das die richtige
  Absicherung, oder gibt es einen Fall, in dem das falsch matcht oder zu
  restriktiv ist und den Override fälschlich NICHT anwendet?
- Ist es ein Problem, dass `daily_closes` (aus `fetch_daily_closes()`) nach
  dem ORIGINALEN Börsenkalendertag indiziert ist (`ts.date()` des
  `interval=1d`-Abrufs, ohne Zeitzonenumrechnung), während die
  `local_dates`-Menge in `bucket_hourly()` ebenfalls aus den ORIGINALEN
  (nicht NY-konvertierten) Stundenbalken-Zeitstempeln kommt? Sind das
  garantiert dieselben Kalendertage, oder könnte es hier eine stille
  Off-by-one-Verschiebung geben (z.B. durch Sommerzeit-Umstellung, oder
  weil `yfinance`/`yfinance-cache` Tagesbalken manchmal mit einem
  Mitternachts-Zeitstempel ohne echte Uhrzeit-Bedeutung liefert)?
- Ist `retry()` (siehe Abschnitt Hilfsfunktionen) für `fetch_daily_closes()`
  angemessen, gegeben dass bei Fehlschlag einfach ein leeres Dict
  zurückgegeben wird (stiller Fallback auf die alte Stundenbalken-Logik)?
  Sollte das lauter fehlschlagen oder ist der stille Fallback hier
  gewünscht (Robustheit vor Vollständigkeit)?
- Sollte diese Override-Logik auch für andere Instrumente mit früh
  endender Sitzung gelten (aktuell nur DAX in der Liste, aber z.B. auch
  US-Cash-Indizes `^GSPC`/`^DJI`/`^NDX`/`^RUT`, deren reguläre Sitzung um
  16:00 ET endet, knapp vor 16:59)? Bisher wurde das nur für DAX empirisch
  bestätigt als nötig (US-Werte stimmten in Stichproben schon mit der
  Referenz überein), aber ist das strukturell auch für die US-Indizes ein
  Risiko?

## 5. Weitere historisch bekannte Sonderfälle (unverändert, zur Info)

- **BTC-USD**: nur Montag–Freitag relevant (Wochenend-Buckets werden
  verworfen), obwohl BTC technisch 24/7 handelt — explizite Vorgabe der
  Auftraggeberin.
- **Unvollständiger laufender Tag**: `bucket_complete()` verwirft den letzten
  Bucket, wenn "jetzt" (NY-Zeit) noch vor dessen Rollover (18:00 NY) liegt —
  verhindert, dass ein Live-Zwischenstand als abgeschlossener Handelstag
  behandelt wird.
- **DST-Sicherheit**: Vorwoche wird über reine Kalendertag-Arithmetik
  berechnet (`today_monday - timedelta(days=7)`), nicht über
  Millisekunden-Subtraktion, um Sommerzeit-Umstellungen nicht zu einem
  falschen Wochentag springen zu lassen.

## 6. Vollständiger Quellcode (python/generate_signals.py)

```python
#!/usr/bin/env python3
"""
Signal-Board Datenpipeline.
[... siehe Repo fuer den vollstaendigen, identischen Kommentarkopf ...]
"""

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance_cache as yfc

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "public" / "data" / "signals.json"

INSTRUMENTS = [
    {"ticker": "^GDAXI", "sym": "DE40", "sec": "Indizes", "dec": 1},
    {"ticker": "^GSPC", "sym": "US500", "sec": "Indizes", "dec": 2},
    {"ticker": "^DJI", "sym": "US30", "sec": "Indizes", "dec": 0},
    {"ticker": "^NDX", "sym": "USTEC", "sec": "Indizes", "dec": 2},
    {"ticker": "^RUT", "sym": "US2000", "sec": "Indizes", "dec": 1},
    {"ticker": "GC=F", "sym": "GC", "sec": "Metalle", "dec": 1},
    {"ticker": "SI=F", "sym": "SI", "sec": "Metalle", "dec": 3},
    {"ticker": "PL=F", "sym": "PL", "sec": "Metalle", "dec": 1},
    {"ticker": "HG=F", "sym": "HG", "sec": "Metalle", "dec": 4},
    {"ticker": "CL=F", "sym": "CL", "sec": "Energie", "dec": 2},
    {"ticker": "EURUSD=X", "sym": "EURUSD", "sec": "Forex", "dec": 5},
    {"ticker": "GBPUSD=X", "sym": "GBPUSD", "sec": "Forex", "dec": 5},
    {"ticker": "AUDUSD=X", "sym": "AUDUSD", "sec": "Forex", "dec": 5},
    {"ticker": "CAD=X", "sym": "USDCAD", "sec": "Forex", "dec": 5},
    {"ticker": "BTC-USD", "sym": "BTCUSD", "sec": "Krypto", "dec": 2},
]

ROLLOVER_HOUR_NY = 18
CLOSE_REF_MIN_NY = 16 * 60 + 59
ORDER = ["HCOM", "LCOM", "HCOW", "LCOW", "PWH", "PWL", "PMH", "PML"]


def safe_float(value):
    try:
        f = float(value)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def retry(fn, attempts=3, base_delay=2.0):
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_err


def fetch_hour_bars(ticker):
    def _do():
        df = yfc.Ticker(ticker).history(
            period="3mo", interval="60m", adjust_splits=False, adjust_divs=False
        )
        if df is None or df.empty:
            raise ValueError(f"Keine Daten fuer {ticker}")
        return df

    df = retry(_do)
    df = flatten_columns(df)

    bars = []
    for ts, row in df.iterrows():
        o = safe_float(row.get("Open"))
        h = safe_float(row.get("High"))
        l = safe_float(row.get("Low"))
        c = safe_float(row.get("Close"))
        if o is None or h is None or l is None or c is None:
            continue
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        bars.append({"ts": ts.tz_convert(NY), "local_date": ts.date(), "o": o, "h": h, "l": l, "c": c})
    return bars


DAILY_CLOSE_OVERRIDE_TICKERS = {"^GDAXI"}


def fetch_daily_closes(ticker):
    def _do():
        df = yfc.Ticker(ticker).history(
            period="3mo", interval="1d", adjust_splits=False, adjust_divs=False
        )
        if df is None or df.empty:
            raise ValueError(f"Keine Tagesdaten fuer {ticker}")
        return df

    try:
        df = retry(_do)
    except Exception:
        return {}
    df = flatten_columns(df)

    closes = {}
    for ts, row in df.iterrows():
        c = safe_float(row.get("Close"))
        if c is not None:
            closes[ts.date()] = c
    return closes


def trading_day_of(ny_dt):
    d = ny_dt.date()
    if ny_dt.hour >= ROLLOVER_HOUR_NY:
        d = d + timedelta(days=1)
    return d


def bucket_hourly(bars, daily_closes=None):
    daily_closes = daily_closes or {}
    buckets = {}
    for b in bars:
        day = trading_day_of(b["ts"])
        buckets.setdefault(day, []).append(b)

    days = []
    for day in sorted(buckets.keys()):
        day_bars = sorted(buckets[day], key=lambda b: b["ts"])
        h = max(b["h"] for b in day_bars)
        l = min(b["l"] for b in day_bars)
        close_bar = None
        for b in day_bars:
            minute_of_day = b["ts"].hour * 60 + b["ts"].minute
            if minute_of_day <= CLOSE_REF_MIN_NY:
                close_bar = b
        c = close_bar["c"] if close_bar else day_bars[-1]["c"]

        local_dates = {b["local_date"] for b in day_bars}
        if len(local_dates) == 1:
            override = daily_closes.get(next(iter(local_dates)))
            if override is not None:
                c = override

        days.append({"day": day, "o": day_bars[0]["o"], "h": h, "l": l, "c": c})
    return days


def bucket_complete(bucket_day, now_ny):
    end = datetime.combine(bucket_day, datetime.min.time(), tzinfo=NY).replace(hour=ROLLOVER_HOUR_NY)
    return now_ny >= end


def monday_of(d):
    return d - timedelta(days=d.weekday())


def month_key_of(d):
    return d.year * 100 + d.month


def prev_month_first(d):
    first_of_this_month = d.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_prev_month.replace(day=1)


def day_dir(cur, prev):
    if prev is None:
        return None
    if cur["c"] > prev["h"]:
        return "up"
    if cur["c"] < prev["l"]:
        return "down"
    return None


def compute_instrument(inst, now_ny):
    bars = fetch_hour_bars(inst["ticker"])
    if len(bars) < 10:
        raise ValueError("zu wenige Balken")

    daily_closes = None
    if inst["ticker"] in DAILY_CLOSE_OVERRIDE_TICKERS:
        daily_closes = fetch_daily_closes(inst["ticker"])
    days = bucket_hourly(bars, daily_closes)

    if inst["ticker"] == "BTC-USD":
        days = [d for d in days if d["day"].weekday() < 5]

    if len(days) < 5:
        raise ValueError("zu wenige Handelstage")

    if not bucket_complete(days[-1]["day"], now_ny):
        days = days[:-1]
    if len(days) < 5:
        raise ValueError("zu wenige abgeschlossene Handelstage")

    today_idx = len(days) - 1
    yest_idx = today_idx - 1
    if today_idx < 2 or yest_idx < 1:
        raise ValueError("zu kurze Historie")

    today = days[today_idx]
    yest = days[yest_idx]
    dirs = [None] + [day_dir(days[i], days[i - 1]) for i in range(1, len(days))]

    today_day = today["day"]
    today_monday = monday_of(today_day)
    today_month_key = month_key_of(today_day)
    prev_week_monday = today_monday - timedelta(days=7)
    prev_month_key = month_key_of(prev_month_first(today_day))

    wk, mo = [], []
    pw_bars, pm_bars = [], []
    for i in range(1, today_idx):
        d = days[i]["day"]
        if monday_of(d) == today_monday and dirs[i]:
            wk.append(days[i]["c"])
        if month_key_of(d) == today_month_key and dirs[i]:
            mo.append(days[i]["c"])
        if monday_of(d) == prev_week_monday:
            pw_bars.append(days[i])
        if month_key_of(d) == prev_month_key:
            pm_bars.append(days[i])

    return {
        "sym": inst["sym"], "sec": inst["sec"], "dec": inst["dec"],
        "o": today["o"], "h": today["h"], "l": today["l"], "c": today["c"],
        "pH": yest["h"], "pL": yest["l"],
        "pwH": max((b["h"] for b in pw_bars), default=None),
        "pwL": min((b["l"] for b in pw_bars), default=None),
        "pmH": max((b["h"] for b in pm_bars), default=None),
        "pmL": min((b["l"] for b in pm_bars), default=None),
        "wk": wk, "mo": mo,
        "yesterdayDir": dirs[yest_idx],
    }


def classify(item):
    touched = []
    if item["h"] > item["pH"]:
        touched.append("PDH")
    if item["l"] < item["pL"]:
        touched.append("PDL")
    if item["pwH"] is not None and item["h"] > item["pwH"]:
        touched.append("PWH")
    if item["pwL"] is not None and item["l"] < item["pwL"]:
        touched.append("PWL")
    if item["pmH"] is not None and item["h"] > item["pmH"]:
        touched.append("PMH")
    if item["pmL"] is not None and item["l"] < item["pmL"]:
        touched.append("PML")

    direction = 0
    if "PDH" in touched and "PDL" in touched:
        cat = "OUTSIDE"
    elif item["c"] > item["pH"]:
        cat, direction = "CIB", 1
    elif item["c"] < item["pL"]:
        cat, direction = "CIB", -1
    elif touched:
        candidate = "FRD" if item["c"] < item["o"] else "FGD"
        needed = "up" if candidate == "FRD" else "down"
        cat = candidate if item["yesterdayDir"] == needed else "NONE"
    else:
        cat = "ISD"

    badges = []
    if cat == "CIB":
        wk, mo = item.get("wk"), item.get("mo")
        if wk is not None:
            if not wk:
                badges.append("HCOW" if direction == 1 else "LCOW")
            else:
                if direction == 1 and item["c"] > max(wk):
                    badges.append("HCOW")
                if direction == -1 and item["c"] < min(wk):
                    badges.append("LCOW")
        if mo is not None:
            if not mo:
                badges.append("HCOM" if direction == 1 else "LCOM")
            else:
                if direction == 1 and item["c"] > max(mo):
                    badges.append("HCOM")
                if direction == -1 and item["c"] < min(mo):
                    badges.append("LCOM")
        for x in ("PWH", "PWL", "PMH", "PML"):
            if x in touched:
                badges.append(x)
        badges.sort(key=lambda b: ORDER.index(b))

    return {"cat": cat, "dir": direction, "touched": touched, "badges": badges}


def build_signals(now_ny):
    trading_day = trading_day_of(now_ny)

    signals = []
    errors = []
    for inst in INSTRUMENTS:
        try:
            item = compute_instrument(inst, now_ny)
            cls = classify(item)
            if cls["cat"] == "NONE":
                continue
            signals.append({
                "sym": item["sym"], "sec": item["sec"], "dec": item["dec"],
                "o": round_price(item["o"]), "h": round_price(item["h"]),
                "l": round_price(item["l"]), "c": round_price(item["c"]),
                "pH": round_price(item["pH"]), "pL": round_price(item["pL"]),
                "pwH": round_price(item["pwH"]), "pwL": round_price(item["pwL"]),
                "pmH": round_price(item["pmH"]), "pmL": round_price(item["pmL"]),
                "yesterdayDir": item["yesterdayDir"],
                "cls": cls,
            })
        except Exception as e:
            errors.append(f"{inst['sym']}: {e}")
            print(f"WARNUNG: {inst['sym']} uebersprungen ({e})")

    payload = {
        "updated": datetime.now(BERLIN).isoformat(timespec="seconds"),
        "tradingDay": trading_day.isoformat(),
        "kw": trading_day.isocalendar()[1],
        "signals": signals,
    }
    return payload, errors


def round_price(v):
    return None if v is None else round(v, 6)


def write_signals_file(payload):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="Signal-Board Datenpipeline")
    parser.add_argument(
        "--force", action="store_true",
        help="Rollover-Check ueberspringen (z.B. fuer manuelle Testlaeufe vor 18:00 NY)",
    )
    return parser.parse_args()


def check_rollover(now_ny, force):
    if now_ny.hour < ROLLOVER_HOUR_NY and not force:
        raise SystemExit(
            f"Rollover noch nicht erreicht ({now_ny.strftime('%H:%M %Z')}, "
            f"Rollover ist {ROLLOVER_HOUR_NY}:00 NY). Abbruch -- mit --force "
            f"ueberschreiben (z.B. fuer manuelle Testlaeufe)."
        )


def main():
    args = parse_args()
    now_ny = datetime.now(NY)
    check_rollover(now_ny, args.force)

    payload, errors = build_signals(now_ny)
    write_signals_file(payload)
    print(f"OK: {len(payload['signals'])} von {len(INSTRUMENTS)} Instrumenten nach {OUTPUT_PATH} geschrieben.")
    if errors:
        print(f"{len(errors)} Instrument(e) uebersprungen:")
        for e in errors:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
```

## 7. Was ich von dir brauche

1. Gehe die Klassifikationslogik (`classify()`) Schritt für Schritt durch und
   bestätige oder widerlege, dass sie exakt die Regeln aus Abschnitt 3 und 4a
   umsetzt.
2. Prüfe `bucket_hourly()`/`fetch_daily_closes()` (Abschnitt 4b) speziell auf
   das Datums-Matching-Risiko — gibt es einen Fall, in dem `local_date` (aus
   den Stundenbalken) und der Index von `fetch_daily_closes()` (aus dem
   Tagesbalken) für denselben realen Handelstag unterschiedliche Werte
   liefern könnten?
3. Prüfe `compute_instrument()` auf Off-by-one-Fehler bei den Indizes
   (`today_idx`, `yest_idx`, die Slice-Grenzen in der wk/mo/pw/pm-Schleife).
4. Gibt es einen Fall, in dem `wk`/`mo` fälschlich den heutigen Tag selbst
   mit einschließen oder einen Tag doppelt zählen?
5. Sonstige Korrektheitsfehler, die dir auffallen.

Bitte mit konkreten Beispieldaten (welche Eingabe, welches falsche Ergebnis)
antworten, keine allgemeinen Stilhinweise.

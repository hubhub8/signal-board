#!/usr/bin/env python3
"""
Signal-Board Datenpipeline.

Ersetzt den bisherigen Netlify-Proxy (netlify/functions/yahoo.js) UND die
client-seitige Klassifikationslogik in public/signal-board.html: dieses
Skript holt die Stundenkerzen ueber yfinance-cache, buendelt sie zu
Handelstagen (Rollover 18:00 NY), klassifiziert jedes Instrument (CIB/ISD/
FRD/FGD) und schreibt das fertige Ergebnis nach public/data/signals.json.
Cloudflare Pages liefert diese Datei anschliessend als ganz normale
statische Datei aus -- kein R2, kein Live-Fetch im Browser mehr.

Die Buendelungs- und Klassifikationsregeln sind 1:1 aus der bisherigen
JavaScript-Logik in public/signal-board.html portiert (bucketHourly(),
classify(), fetchYahoo()) -- bei Aenderungen an einer Seite IMMER auch die
andere pruefen, beide muessen exakt zusammenpassen.
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

# Pfade relativ zum Skript selbst (nicht zum Arbeitsverzeichnis) aufloesen --
# das Skript muss sowohl per "python python/generate_signals.py" vom Repo-Root
# als auch per "cd python && python generate_signals.py" funktionieren.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "public" / "data" / "signals.json"

# ===========================
# INSTRUMENTE -- muss exakt mit der INSTRUMENTS-Liste in signal-board.html
# uebereinstimmen (gleiche Ticker, gleiche Anzeigenamen, gleiche Sektionen).
# ===========================
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

ROLLOVER_HOUR_NY = 18  # Handelstag-Rollover (Globex-Wiedereroeffnung nach CME-Wartungspause)
CLOSE_REF_MIN_NY = 16 * 60 + 59  # 16:59 NY -- gilt fuer ALLE Instrumente gleich
ORDER = ["HCOM", "LCOM", "HCOW", "LCOW", "PWH", "PWL", "PMH", "PML"]


# ===========================
# HILFSFUNKTIONEN
# ===========================
def safe_float(value):
    """Wandelt einen Wert robust in float um. None/NaN/nicht-konvertierbare
    Werte ergeben None statt eines Crashs (z.B. bei Yahoo-Datenluecken)."""
    try:
        f = float(value)
        if f != f:  # NaN-Check ohne math-Import (NaN ist nie gleich sich selbst)
            return None
        return f
    except (TypeError, ValueError):
        return None


def flatten_columns(df):
    """Reduziert ein MultiIndex-Spaltenschema (liefert yfinance je nach
    Abrufform, z.B. ('Open','GC=F')) auf die erste Ebene (Open/High/Low/Close)."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def retry(fn, attempts=3, base_delay=2.0):
    """Ruft fn() auf, mit bis zu `attempts` Versuchen und exponentiell
    wachsender Wartezeit zwischen den Versuchen (Yahoo ist gelegentlich
    kurzzeitig nicht erreichbar / liefert 429)."""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 -- bewusst breit, Retry soll alles abfangen
            last_err = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_err


# ===========================
# DATENABRUF
# ===========================
def fetch_hour_bars(ticker):
    """Holt 60-Minuten-Balken der letzten 3 Monate ueber yfinance-cache und
    wandelt sie in eine einfache Liste von Dicts um. Zeitstempel werden auf
    NY-Zeit umgerechnet (tz-aware), damit die Buendelung unabhaengig von der
    Boersenzeitzone des jeweiligen Tickers funktioniert. 'local_date' behaelt
    zusaetzlich das Datum in der ORIGINALEN Boersenzeitzone (vor der NY-
    Umrechnung) -- wird fuer den Tagesbalken-Abgleich in
    DAILY_BAR_OVERRIDE_TICKERS gebraucht."""

    def _do():
        # yfinance-cache hat kein auto_adjust= (Signatur weicht von reinem
        # yfinance ab) -- Split-/Dividenden-Anpassung stattdessen einzeln
        # abschalten, gleichbedeutend mit auto_adjust=False.
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


# Instrumente, deren Boersensitzung lange vor 16:59 NY endet (z.B. Xetra bis
# ca. 11:30 EDT): Yahoos Stundenbalken erfassen dort NICHT die eigentliche
# Schlussauktion, nur den letzten Balken der regulaeren Sitzung -- das ergab
# bei DE40 am 23.09.2026 einen um 16 Punkte falschen Schlusskurs (25426,55
# statt echtem Xetra-Schluss 25410,63), der in Grenzfaellen die Klassifikation
# kippen kann.
#
# Seit 25.09.2026 wird fuer diese Ticker der GESAMTE Tagesbalken herangezogen
# (Hoch/Tief/Schluss), nicht mehr nur der Schlusskurs. Grund: Wird nur der
# Schluss ersetzt, koennen die Tageswerte in sich widerspruechlich werden --
# der Schluss aus der Tagesdatei (mit Auktion) lag bei DE40 an 8 von 66 Tagen
# AUSSERHALB der Spanne aus den Stundenbalken (7x unter dem Tief, 1x ueber dem
# Hoch), z.B. 07.07.2026: Stunden l=25475,49 h=25811,97, Tagesschluss 25465,25.
# Folgen: der Close-Marker wurde ausserhalb des Spannenbalkens gezeichnet und
# ein echtes PDH/PDL konnte unbemerkt bleiben.
#
# Bewusst NICHT fuer alle Instrumente -- und weiterhin NICHT fuer die
# Handelstag-Zuordnung oder die Buendelung: nur Hoch/Tief/Schluss des
# betroffenen Tages stammen aus dem Tagesbalken.
DAILY_BAR_OVERRIDE_TICKERS = {"^GDAXI"}


def fetch_daily_bars(ticker):
    """Holt native Tagesbalken und liefert Hoch/Tief/Schluss je Kalendertag in
    der Boersenzeitzone des Tickers (Schluss inklusive Schlussauktion)."""

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

    out = {}
    for ts, row in df.iterrows():
        bar = {
            "o": safe_float(row.get("Open")),
            "h": safe_float(row.get("High")),
            "l": safe_float(row.get("Low")),
            "c": safe_float(row.get("Close")),
        }
        if bar["c"] is not None:
            out[ts.date()] = bar
    return out


# ===========================
# BUENDELUNG ZU HANDELSTAGEN (1:1 aus bucketHourly() in signal-board.html)
# ===========================
def trading_day_of(ny_dt):
    """Ordnet einen NY-lokalen Zeitpunkt seinem Handelstag zu (Rollover 18:00 NY)."""
    d = ny_dt.date()
    if ny_dt.hour >= ROLLOVER_HOUR_NY:
        d = d + timedelta(days=1)
    return d


def bucket_hourly(bars, daily_bars=None):
    """Buendelt Stundenbalken zu Handelstagen. Schluss = letzter Balken bis
    einschliesslich 16:59 NY (nicht exakte Minutenuebereinstimmung), sonst
    der letzte verfuegbare Balken des Tages.

    Falls daily_bars uebergeben wird (siehe DAILY_BAR_OVERRIDE_TICKERS) und ein
    passender nativer Tagesbalken existiert, stammen Hoch/Tief/Schluss dieses
    Tages aus dem Tagesbalken (er enthaelt die Schlussauktion). Gebildet wird
    die VEREINIGUNG aus Stunden- und Tagesbalken (max/min): so geht kein
    Extremwert der Stundenbalken verloren, und der Schluss liegt garantiert
    innerhalb von [l, h]. Die Handelstag-Zuordnung bleibt unangetastet."""
    daily_bars = daily_bars or {}
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
            override = daily_bars.get(next(iter(local_dates)))
            if override:
                if override["c"] is not None:
                    c = override["c"]
                if override["h"] is not None:
                    h = max(h, override["h"])
                if override["l"] is not None:
                    l = min(l, override["l"])

        days.append({"day": day, "o": day_bars[0]["o"], "h": h, "l": l, "c": c})
    return days


def bucket_complete(bucket_day, now_ny):
    """Ein Bucket gilt erst als abgeschlossen, wenn sein Rollover (18:00 NY)
    erreicht ist -- CIB/ISD/FRD/FGD sind End-of-Day-Klassifikationen, kein
    Intraday-Zwischenstand."""
    end = datetime.combine(bucket_day, datetime.min.time(), tzinfo=NY).replace(hour=ROLLOVER_HOUR_NY)
    return now_ny >= end


def monday_of(d):
    return d - timedelta(days=d.weekday())


def month_key_of(d):
    return d.year * 100 + d.month


def prev_month_first(d):
    """Erster Tag des Vormonats von d (unabhaengig vom Tag-des-Monats von d)."""
    first_of_this_month = d.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_prev_month.replace(day=1)


def day_dir(cur, prev):
    """'up', wenn der Schluss ueber dem Vortageshoch liegt, 'down' wenn er
    unter dem Vortagestief liegt, sonst None (kein eigener CIB-Tag)."""
    if prev is None:
        return None
    if cur["c"] > prev["h"]:
        return "up"
    if cur["c"] < prev["l"]:
        return "down"
    return None


def is_outside_day(cur, prev):
    """Outside Day: heutiges Hoch UND Tief liegen beide jenseits des Vortags.
    Muss mit der OUTSIDE-Pruefung in classify() uebereinstimmen (dort ueber die
    'touched'-Liste PDH+PDL, also dieselbe Regel) -- bei Aenderungen beide
    Stellen anpassen."""
    if prev is None:
        return False
    return cur["h"] > prev["h"] and cur["l"] < prev["l"]


# ===========================
# PRO INSTRUMENT: Buendelung + Vorwoche/Vormonat/wk/mo
# ===========================
def compute_instrument(inst, now_ny):
    bars = fetch_hour_bars(inst["ticker"])
    if len(bars) < 10:
        raise ValueError("zu wenige Balken")

    daily_bars = None
    if inst["ticker"] in DAILY_BAR_OVERRIDE_TICKERS:
        daily_bars = fetch_daily_bars(inst["ticker"])
        if not daily_bars:
            # fetch_daily_bars() faengt eigene Fehler ab und gibt dann ein
            # leeres Dict zurueck (Robustheit) -- das darf aber nicht lautlos
            # passieren, sonst faellt der DAX-Tagesbalken-Abgleich unbemerkt
            # auf die ungenaue Stundenbalken-Variante zurueck.
            print(f"WARNUNG: Tagesbalken-Abgleich fuer {inst['ticker']} nicht verfuegbar, "
                  f"verwende Stundenbalken-Fallback fuer Hoch/Tief/Schluss.")
    days = bucket_hourly(bars, daily_bars)

    # Selbstpruefung: der Schluss muss innerhalb der Tagesspanne liegen. Nach
    # der Vereinigung aus Stunden- und Tagesbalken kann das eigentlich nicht
    # mehr passieren (vorher: 8 von 66 DE40-Tagen), wird aber gemeldet statt
    # verschwiegen -- beide Quellen sind Yahoo-Serien, die voneinander
    # abweichen koennen (siehe USTEC-Fall vom 23.09.2026).
    for d in days:
        if not (d["l"] <= d["c"] <= d["h"]):
            print(f"WARNUNG: {inst['sym']} {d['day']}: Schluss ausserhalb der "
                  f"Tagesspanne (l={d['l']}, c={d['c']}, h={d['h']}).")

    if inst["ticker"] == "BTC-USD":
        # BTC ist nur Mo-Fr relevant -- Wochenend-Buckets verwerfen
        # (siehe Projektnotizen: BTC handelt zwar 24/7, wird hier aber wie
        # alle anderen Instrumente nur an Wochentagen bewertet).
        days = [d for d in days if d["day"].weekday() < 5]

    if len(days) < 5:
        raise ValueError("zu wenige Handelstage")

    # Noch laufenden letzten Bucket verwerfen -- Schlusskurs des letzten
    # ABGESCHLOSSENEN Handelstags, kein Live-Zwischenstand.
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
    # Outside Days (Hoch UND Tief jenseits des Vortags) sind eine eigene
    # Kategorie ohne Richtung und ohne Badges -- sie duerfen deshalb NICHT als
    # Referenz fuer HCOM/LCOM/HCOW/LCOW herangezogen werden, auch wenn ihr
    # Schluss jenseits der Vortagesspanne liegt. Gemessen: 15 von 447
    # Handelstagen (3,4 %) hatten einen solchen Tag; stellte er das Monats-
    # oder Wochenextrem, fehlte die Badge des heutigen CIB-Tages.
    outside = [False] + [is_outside_day(days[i], days[i - 1]) for i in range(1, len(days))]

    today_day = today["day"]
    today_monday = monday_of(today_day)
    today_month_key = month_key_of(today_day)
    prev_week_monday = today_monday - timedelta(days=7)  # DST-sicher: Kalendertag-Arithmetik
    prev_month_key = month_key_of(prev_month_first(today_day))

    wk, mo = [], []
    pw_bars, pm_bars = [], []
    for i in range(1, today_idx):
        d = days[i]["day"]
        if monday_of(d) == today_monday and dirs[i] and not outside[i]:
            wk.append(days[i]["c"])
        if month_key_of(d) == today_month_key and dirs[i] and not outside[i]:
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


# ===========================
# KLASSIFIKATION (1:1 aus classify() in signal-board.html)
# ===========================
def classify(item):
    """OUTSIDE: Hoch UND Tief beide jenseits des Vortags (hat Vorrang vor
    allem anderen, keine Richtung). CIB: Close jenseits PDH/PDL (und NICHT
    Outside Day). ISD: Close in Vortagesspanne UND kein Level beruehrt.
    FRD/FGD: Level beruehrt, Close zurueck in der Spanne, UND Vortag war
    selbst Pump(FRD)/Dump(FGD). Level beruehrt + Pump/Dump-Vorlauf fehlt =>
    cat='NONE' (wird nicht ausgegeben, NIE Fallback auf ISD)."""
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
        # Outside Day -- dieselbe Regel wie is_outside_day(); bei Aenderungen
        # beide Stellen anpassen.
        # Outside Day: heutiges Hoch UND Tief liegen beide jenseits des
        # Vortags -- eigene Kategorie, unabhaengig davon wo der Schluss
        # landet, hat Vorrang vor CIB (auch ein klarer Ausbruch bleibt
        # OUTSIDE, wenn beide Seiten getriggert wurden). Keine Richtung.
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
        # Ist heute der erste CIB-Tag der Woche/des Monats, ist er trivialerweise
        # Hoechst- UND Tiefstschluss -- angezeigt wird nur der zur Richtung
        # passende Wert.
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


# ===========================
# GESAMTLAUF
# ===========================
def build_signals(now_ny):
    # tradingDay ist der Tag, FUER den die Signale gelten (heute, aus der
    # Wanduhr) -- nicht der Tag, aus dem der Schlusskurs stammt (gestern
    # 16:59). Die Daten sind bewusst von gestern, gelten aber fuer den
    # kommenden/aktuellen Handelstag.
    trading_day = trading_day_of(now_ny)

    signals = []
    errors = []
    for inst in INSTRUMENTS:
        try:
            item = compute_instrument(inst, now_ny)
            cls = classify(item)
            if cls["cat"] == "NONE":
                # Level beruehrt, aber kein Pump/Dump-Vorlauf -- wird laut
                # Vorgabe nicht ausgegeben (kein Fallback auf ISD).
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
        except Exception as e:  # noqa: BLE001 -- ein fehlgeschlagenes Instrument darf den Lauf nicht abbrechen
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
    """Rundet einen Preis fuers JSON auf 6 Nachkommastellen -- entfernt das
    Float32-Rauschen aus yfinance (z.B. 7761.93994140625), ohne echte
    Praezision zu verlieren (alle Instrumente brauchen hoechstens 5 Dezimalen
    fuer die Anzeige). Wird erst NACH der Klassifikation angewendet, damit
    classify() weiterhin mit den vollen Rohwerten vergleicht.
    """
    return None if v is None else round(v, 6)


def write_signals_file(payload):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="Signal-Board Datenpipeline")
    parser.add_argument(
        "--force", action="store_true",
        help="Rollover-Hinweis unterdruecken (der Lauf wird nicht mehr abgebrochen)",
    )
    return parser.parse_args()


def check_rollover(now_ny, force):
    """Hinweis VOR dem eigentlichen Lauf -- bricht NICHT mehr ab.

    Vorfall 25.09.2026: Der GitHub-Actions-Scheduler hat den naechtlichen Lauf
    5,5 h verspaetet gestartet (03:00 UTC geplant, 08:34 UTC gefeuert = 04:34
    NY). Der hier frueher stehende harte Abbruch hat den Lauf deshalb mit
    Exit-Code 1 beendet, der Commit-Schritt wurde uebersprungen und das Board
    blieb einen Tag alt -- obwohl das Ergebnis identisch gewesen waere:

    Innerhalb des offenen Handelstagsfensters [18:00 D-1, 18:00 D) liefert
    trading_day_of() immer D, und der noch laufende Bucket D wird von
    bucket_complete() ohnehin verworfen. days[-1] ist damit immer der zuletzt
    ABGESCHLOSSENE Tag D-1 -- egal ob der Lauf um 19:00 NY am Vortag oder um
    04:00 NY am Folgetag startet. Die praezise Absicherung pro Instrument
    leistet weiterhin bucket_complete() in compute_instrument(); dieser Check
    liefert nur noch Kontext fuers Log.
    """
    if now_ny.hour < ROLLOVER_HOUR_NY and not force:
        print(f"HINWEIS: NY-Zeit {now_ny.strftime('%H:%M %Z')} liegt noch vor dem "
              f"{ROLLOVER_HOUR_NY}:00-Rollover. Es wird der zuletzt abgeschlossene "
              f"Handelstag verwendet.")


def main():
    args = parse_args()
    now_ny = datetime.now(NY)
    check_rollover(now_ny, args.force)

    payload, errors = build_signals(now_ny)

    if not payload["signals"]:
        # Nichts schreiben, wenn KEIN einziges Instrument erfolgreich war
        # (z.B. kompletter Yahoo-Ausfall) -- sonst wuerde eine leere
        # signals.json das zuletzt funktionierende Board committen und
        # ueberschreiben, obwohl der Lauf komplett fehlgeschlagen ist. Die
        # alte, zuletzt gute Datei bleibt so unangetastet liegen.
        print("FEHLER: Kein einziges Instrument erfolgreich -- signals.json NICHT ueberschrieben.")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)

    write_signals_file(payload)
    print(f"OK: {len(payload['signals'])} von {len(INSTRUMENTS)} Instrumenten nach {OUTPUT_PATH} geschrieben.")
    if errors:
        print(f"{len(errors)} Instrument(e) uebersprungen:")
        for e in errors:
            print(f"  - {e}")


if __name__ == "__main__":
    main()

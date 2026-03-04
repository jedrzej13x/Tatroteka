"""
TATRY FLOW — IMGW Weather Collector
Pobiera dane pogodowe dla Kasprowego Wierchu i Zakopanego
z API IMGW i zapisuje do tatry_segments.db + weather_data.json.

Uruchomienie:
    python imgw_fetcher.py              # pobierz i zapisz
    python imgw_fetcher.py --export     # tylko eksport JSON z bazy
    python imgw_fetcher.py --report     # pokaż dane w bazie
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
import requests
from datetime import date
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH   = os.getenv("DB_PATH", "tatry_segments.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

IMGW_SYNOP_URL = "https://danepubliczne.imgw.pl/api/data/synop"
IMGW_STACJE = {
    "kasprowy_wierch": {"id": "12650", "nazwa": "Kasprowy Wierch", "lat": 49.2319, "lon": 19.9817},
    "zakopane":        {"id": "12640", "nazwa": "Zakopane",        "lat": 49.2992, "lon": 19.9742},
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("imgw")

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_key     TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    temperatura     REAL,
    predkosc_wiatru INTEGER,
    kierunek_wiatru INTEGER,
    wilgotnosc      REAL,
    suma_opadu      REAL,
    cisnienie       REAL,
    UNIQUE(station_key, captured_at)
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def collect(today=None):
    if today is None:
        today = date.today().isoformat()

    log.info(f"Pobieram dane IMGW dla {today}...")
    try:
        resp = requests.get(IMGW_SYNOP_URL, timeout=30)
        resp.raise_for_status()
        wszystkie = resp.json()
    except Exception as e:
        log.error(f"Blad pobierania IMGW: {e}")
        sys.exit(1)

    stacje_index = {s["id_stacji"]: s for s in wszystkie}
    conn = get_db()

    for key, meta in IMGW_STACJE.items():
        dane = stacje_index.get(meta["id"])
        if not dane:
            log.warning(f"Brak danych dla {meta['nazwa']} (id={meta['id']})")
            continue

        def safe_float(v):
            try: return float(v) if v is not None else None
            except: return None

        def safe_int(v):
            try: return int(v) if v is not None else None
            except: return None

        try:
            conn.execute("""
                INSERT INTO weather_snapshots
                    (station_key, captured_at, temperatura, predkosc_wiatru,
                     kierunek_wiatru, wilgotnosc, suma_opadu, cisnienie)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(station_key, captured_at) DO UPDATE SET
                    temperatura      = excluded.temperatura,
                    predkosc_wiatru  = excluded.predkosc_wiatru,
                    kierunek_wiatru  = excluded.kierunek_wiatru,
                    wilgotnosc       = excluded.wilgotnosc,
                    suma_opadu       = excluded.suma_opadu,
                    cisnienie        = excluded.cisnienie
            """, (
                key, today,
                safe_float(dane.get("temperatura")),
                safe_int(dane.get("predkosc_wiatru")),
                safe_int(dane.get("kierunek_wiatru")),
                safe_float(dane.get("wilgotnosc_wzgledna")),
                safe_float(dane.get("suma_opadu")),
                safe_float(dane.get("cisnienie")),
            ))
            log.info(f"{meta['nazwa']}: {dane.get('temperatura')}°C, "
                     f"wiatr {dane.get('predkosc_wiatru')} m/s, "
                     f"opady {dane.get('suma_opadu')} mm, "
                     f"wilgotnosc {dane.get('wilgotnosc_wzgledna')}%")
        except Exception as e:
            log.error(f"Blad zapisu ({key}): {e}")

    conn.commit()
    conn.close()
    log.info("Zapis do bazy OK.")


def export_json(output_path="weather_data.json"):
    """
    Buduje weather_data.json z tabeli weather_snapshots.
    Struktura:
    {
      "kasprowy_wierch": {
        "meta": {"nazwa": "Kasprowy Wierch", "lat": 49.2319, "lon": 19.9817},
        "series": {
          "2026-03-03": {
            "temperatura": -2.1, "predkosc_wiatru": 8,
            "kierunek_wiatru": 270, "wilgotnosc": 79.0,
            "suma_opadu": 0.0, "cisnienie": null
          },
          ...
        }
      },
      "zakopane": { ... }
    }
    """
    conn = get_db()
    series_map = defaultdict(dict)

    for row in conn.execute("""
        SELECT station_key, captured_at, temperatura, predkosc_wiatru,
               kierunek_wiatru, wilgotnosc, suma_opadu, cisnienie
        FROM weather_snapshots
        ORDER BY captured_at
    """):
        series_map[row["station_key"]][row["captured_at"]] = {
            "temperatura":     row["temperatura"],
            "predkosc_wiatru": row["predkosc_wiatru"],
            "kierunek_wiatru": row["kierunek_wiatru"],
            "wilgotnosc":      row["wilgotnosc"],
            "suma_opadu":      row["suma_opadu"],
            "cisnienie":       row["cisnienie"],
        }

    result = {}
    for key, meta in IMGW_STACJE.items():
        result[key] = {
            "meta":   {"nazwa": meta["nazwa"], "lat": meta["lat"], "lon": meta["lon"]},
            "series": series_map.get(key, {}),
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    conn.close()
    log.info(f"Eksport: {output_path} "
             f"({sum(len(v['series']) for v in result.values())} snapshotow)")


def report():
    conn = get_db()
    rows = conn.execute("""
        SELECT station_key, captured_at, temperatura, predkosc_wiatru,
               kierunek_wiatru, wilgotnosc, suma_opadu, cisnienie
        FROM weather_snapshots
        ORDER BY station_key, captured_at
    """).fetchall()

    if not rows:
        print("Brak danych w weather_snapshots.")
        conn.close()
        return

    print(f"\n{'='*70}")
    print(f"  IMGW — Dane pogodowe ({len(rows)} snapshotow)")
    print(f"{'='*70}")
    print(f"  {'Stacja':<20} {'Data':<12} {'Temp':>6} {'Wiatr':>8} {'Kier':>5} {'Wilg':>6} {'Opady':>6} {'Cisn':>8}")
    print(f"  {'-'*66}")
    for row in rows:
        print(f"  {row['station_key']:<20} {row['captured_at']:<12} "
              f"{str(row['temperatura']) + '°C':>6} "
              f"{str(row['predkosc_wiatru']) + ' m/s':>8} "
              f"{str(row['kierunek_wiatru']) + '°':>5} "
              f"{str(row['wilgotnosc']) + '%':>6} "
              f"{str(row['suma_opadu']) + 'mm':>6} "
              f"{str(row['cisnienie']) + 'hPa':>8}")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="IMGW Weather Collector dla Tatr")
    parser.add_argument("--export", action="store_true", help="Eksportuj weather_data.json z bazy")
    parser.add_argument("--report", action="store_true", help="Pokaz dane pogodowe z bazy")
    parser.add_argument("--date",   default=None,        help="Data kolekcji YYYY-MM-DD (domyslnie: dzis)")
    args = parser.parse_args()

    if args.report:
        report(); return
    if args.export:
        export_json(); return

    # Domyslnie: pobierz + eksportuj
    collect(today=args.date)
    export_json()


if __name__ == "__main__":
    main()
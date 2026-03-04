"""
TATRY FLOW — Weather Collector
Źródła:
  - IMGW synop API     → Kasprowy Wierch (PL, 1987m), Zakopane (PL, 857m)
  - Open-Meteo API     → Łomnica (SK, 2634m), Szczyrbskie Jezioro (SK, 1346m)

Zapis: tatry_segments.db (tabela weather_snapshots) + weather_data.json

Użycie:
  python imgw_fetcher.py                       # pobierz dziś + eksportuj
  python imgw_fetcher.py --date 2026-03-03     # backfill konkretnej daty
  python imgw_fetcher.py --export              # tylko eksport JSON z bazy
  python imgw_fetcher.py --report              # podgląd danych w bazie
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
import requests
from datetime import date, datetime, timedelta
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH   = os.getenv("DB_PATH", "tatry_segments.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Stacje ─────────────────────────────────────────────────────────────────────
# Każda stacja: id (dla IMGW) lub None (dla Open-Meteo), lat, lon, wysokość, źródło
STACJE = {
    "kasprowy_wierch": {
        "nazwa":   "Kasprowy Wierch",
        "lat":     49.2319, "lon": 19.9817, "alt": 1987,
        "kraj":    "PL",
        "zrodlo":  "imgw",
        "imgw_id": "12650",
    },
    "zakopane": {
        "nazwa":   "Zakopane",
        "lat":     49.2992, "lon": 19.9742, "alt": 857,
        "kraj":    "PL",
        "zrodlo":  "imgw",
        "imgw_id": "12640",
    },
    "lomnica": {
        "nazwa":   "Łomnica",
        "lat":     49.1953, "lon": 20.2131, "alt": 2634,
        "kraj":    "SK",
        "zrodlo":  "open-meteo",
    },
    "strbske_pleso": {
        "nazwa":   "Szczyrbskie Jezioro",
        "lat":     49.1197, "lon": 20.0611, "alt": 1346,
        "kraj":    "SK",
        "zrodlo":  "open-meteo",
    },
}

IMGW_SYNOP_URL  = "https://danepubliczne.imgw.pl/api/data/synop"

# Open-Meteo historical — dla dat z przeszłości (w tym wczoraj)
# Open-Meteo forecast   — dla daty dzisiejszej (past_days=0)
OPEN_METEO_HIST = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORE = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_VARS = "temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation,surface_pressure"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weather")

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_key     TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    temperatura     REAL,
    predkosc_wiatru REAL,
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


def save_row(conn, key, today, temp, wind_spd, wind_dir, hum, rain, pressure):
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
        """, (key, today, temp, wind_spd, wind_dir, hum, rain, pressure))
        conn.commit()
    except Exception as e:
        log.error(f"Błąd zapisu ({key} {today}): {e}")


# ── IMGW ───────────────────────────────────────────────────────────────────────

def collect_imgw(conn, today):
    log.info("IMGW: pobieranie...")
    try:
        resp = requests.get(IMGW_SYNOP_URL, timeout=30)
        resp.raise_for_status()
        wszystkie = {s["id_stacji"]: s for s in resp.json()}
    except Exception as e:
        log.error(f"IMGW błąd: {e}")
        return

    def sf(v):
        try: return float(v) if v is not None else None
        except: return None
    def si(v):
        try: return int(v) if v is not None else None
        except: return None

    for key, meta in STACJE.items():
        if meta["zrodlo"] != "imgw":
            continue
        dane = wszystkie.get(meta["imgw_id"])
        if not dane:
            log.warning(f"IMGW: brak danych dla {meta['nazwa']}")
            continue
        save_row(conn, key, today,
                 sf(dane.get("temperatura")),
                 sf(dane.get("predkosc_wiatru")),
                 si(dane.get("kierunek_wiatru")),
                 sf(dane.get("wilgotnosc_wzgledna")),
                 sf(dane.get("suma_opadu")),
                 sf(dane.get("cisnienie")))
        log.info(f"IMGW {meta['nazwa']}: {dane.get('temperatura')}°C, "
                 f"wiatr {dane.get('predkosc_wiatru')} m/s, "
                 f"opady {dane.get('suma_opadu')} mm")


# ── Open-Meteo ─────────────────────────────────────────────────────────────────

def collect_open_meteo(conn, today):
    """
    Dla dat historycznych używa archive-api (dane dzienne — średnia/suma z godzinowych).
    Dla daty dzisiejszej używa forecast API z past_days=1 i bierze ostatnią dostępną godzinę.
    """
    today_date = date.fromisoformat(today)
    is_today   = (today_date == date.today())

    for key, meta in STACJE.items():
        if meta["zrodlo"] != "open-meteo":
            continue

        log.info(f"Open-Meteo: pobieranie {meta['nazwa']} ({today})...")

        try:
            if is_today:
                # Forecast API — bieżące dane
                url    = OPEN_METEO_FORE
                params = {
                    "latitude":   meta["lat"],
                    "longitude":  meta["lon"],
                    "hourly":     OPEN_METEO_VARS,
                    "past_days":  1,
                    "forecast_days": 1,
                    "timezone":   "Europe/Warsaw",
                }
            else:
                # Historical API — dane archiwalne
                url    = OPEN_METEO_HIST
                params = {
                    "latitude":   meta["lat"],
                    "longitude":  meta["lon"],
                    "hourly":     OPEN_METEO_VARS,
                    "start_date": today,
                    "end_date":   today,
                    "timezone":   "Europe/Warsaw",
                }

            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

        except Exception as e:
            log.error(f"Open-Meteo błąd ({meta['nazwa']}): {e}")
            continue

        hourly = data.get("hourly", {})
        times  = hourly.get("time", [])
        if not times:
            log.warning(f"Open-Meteo: brak danych godzinowych dla {meta['nazwa']}")
            continue

        # Dla dnia historycznego — agreguj: temp średnia, wiatr max, opady suma
        # Filtruj tylko godziny z żądanego dnia
        idx_dnia = [i for i, t in enumerate(times) if t.startswith(today)]
        if not idx_dnia:
            log.warning(f"Open-Meteo: brak godzin dla {today} ({meta['nazwa']})")
            continue

        def vals(key_h):
            v = hourly.get(key_h, [])
            return [v[i] for i in idx_dnia if i < len(v) and v[i] is not None]

        temps    = vals("temperature_2m")
        winds    = vals("wind_speed_10m")
        wind_dir = vals("wind_direction_10m")
        hums     = vals("relative_humidity_2m")
        rains    = vals("precipitation")
        press    = vals("surface_pressure")

        def avg(lst): return round(sum(lst) / len(lst), 1) if lst else None
        def mx(lst):  return round(max(lst), 1) if lst else None
        def sm(lst):  return round(sum(lst), 1) if lst else None

        temp_sr   = avg(temps)
        wind_max  = mx(winds)
        # kierunek z godziny o największym wietrze
        wind_d    = None
        if winds and wind_dir:
            idx_max = winds.index(max(winds))
            wind_d  = int(wind_dir[idx_max]) if idx_max < len(wind_dir) else None
        hum_sr    = avg(hums)
        rain_sum  = sm(rains)
        press_sr  = avg(press)

        save_row(conn, key, today,
                 temp_sr, wind_max, wind_d, hum_sr, rain_sum, press_sr)
        log.info(f"Open-Meteo {meta['nazwa']}: {temp_sr}°C, "
                 f"wiatr max {wind_max} m/s, opady {rain_sum} mm")


# ── Kolekcja główna ────────────────────────────────────────────────────────────

def collect(today=None):
    if today is None:
        today = date.today().isoformat()
    conn = get_db()
    collect_imgw(conn, today)
    collect_open_meteo(conn, today)
    conn.close()
    log.info("Kolekcja pogody zakończona.")


# ── Eksport JSON ───────────────────────────────────────────────────────────────

def export_json(output_path="weather_data.json"):
    """
    Buduje weather_data.json.
    Struktura:
    {
      "kasprowy_wierch": {
        "meta": {"nazwa": "Kasprowy Wierch", "lat": 49.2319, "lon": 19.9817,
                 "alt": 1987, "kraj": "PL"},
        "series": {
          "2026-03-03": {
            "temperatura": -2.1, "predkosc_wiatru": 8.0,
            "kierunek_wiatru": 270, "wilgotnosc": 79.0,
            "suma_opadu": 0.0, "cisnienie": null
          }, ...
        }
      },
      "zakopane":        { ... },
      "lomnica":         { ... },
      "strbske_pleso":   { ... }
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
    for key, meta in STACJE.items():
        result[key] = {
            "meta": {
                "nazwa": meta["nazwa"],
                "lat":   meta["lat"],
                "lon":   meta["lon"],
                "alt":   meta["alt"],
                "kraj":  meta["kraj"],
            },
            "series": series_map.get(key, {}),
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total = sum(len(v["series"]) for v in result.values())
    log.info(f"Eksport: {output_path} ({total} snapshotów, {len(result)} stacji)")
    conn.close()


# ── Raport ─────────────────────────────────────────────────────────────────────

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

    print(f"\n{'='*80}")
    print(f"  TATRY FLOW — Dane pogodowe ({len(rows)} snapshotów)")
    print(f"{'='*80}")
    print(f"  {'Stacja':<22} {'Data':<12} {'Temp':>6} {'Wiatr':>8} {'Kier':>5} {'Wilg':>6} {'Opady':>7} {'Cisn':>9}")
    print(f"  {'-'*76}")
    for row in rows:
        print(f"  {row['station_key']:<22} {row['captured_at']:<12} "
              f"{str(row['temperatura'])+'°C':>6} "
              f"{str(row['predkosc_wiatru'])+' m/s':>8} "
              f"{str(row['kierunek_wiatru'])+'°':>5} "
              f"{str(row['wilgotnosc'])+'%':>6} "
              f"{str(row['suma_opadu'])+'mm':>7} "
              f"{str(row['cisnienie'])+'hPa':>9}")
    conn.close()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tatry Flow — Weather Collector (IMGW + Open-Meteo)")
    parser.add_argument("--export", action="store_true", help="Eksportuj weather_data.json z bazy")
    parser.add_argument("--report", action="store_true", help="Pokaż dane pogodowe z bazy")
    parser.add_argument("--date",   default=None,        help="Data kolekcji YYYY-MM-DD (domyślnie: dziś)")
    parser.add_argument("--backfill", action="store_true",
                        help="Backfill od 2026-03-03 do wczoraj (tylko Open-Meteo)")
    args = parser.parse_args()

    if args.report:
        report(); return
    if args.export:
        export_json(); return
    if args.backfill:
        # Wypełnij historyczne dane Open-Meteo od 03.03 do wczoraj
        start = date(2026, 3, 3)
        end   = date.today() - timedelta(days=1)
        d     = start
        conn  = get_db()
        while d <= end:
            collect_open_meteo(conn, d.isoformat())
            d += timedelta(days=1)
        conn.close()
        export_json()
        return

    # Domyślnie: pobierz dziś (IMGW + Open-Meteo) + eksportuj
    collect(today=args.date)
    export_json()


if __name__ == "__main__":
    main()
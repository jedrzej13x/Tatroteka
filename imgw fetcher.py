"""
TATRY FLOW - Weather Collector
Stacje:
  IMGW synop (fizyczne pomiary):
    - Kasprowy Wierch (1987m)  id=12650
    - Zakopane (857m)          id=12640
    UWAGA: To sa JEDYNE stacje tatrzanskie w sieci IMGW /api/data/synop.
    Hala Gasienicowa i Morskie Oko maja stacje IMGW, ale nie sa w sieci synoptycznej
    dostepnej przez publiczne API. Uzywamy dla nich Open-Meteo.

  Open-Meteo NWP (model numeryczny - najlepsze dostepne dane dla tych punktow):
    - Morskie Oko (PL, 1395m)
    - Hala Gasienicowa (PL, 1520m)
    - Dolina Chocholowska (PL, 1146m)
    - Lomnica (SK, 2634m)
    - Szczyrbskie Jezioro (SK, 1346m)
    - Tatrzanska Kotlina (SK, 668m)

Uzycie:
  python "imgw fetcher.py"                  # pobierz dzis + DB + eksport JSON
  python "imgw fetcher.py" --live           # live JSON bez DB (co godzine przez cron)
  python "imgw fetcher.py" --export         # eksport z DB do JSON
  python "imgw fetcher.py" --report         # podglad danych w DB
  python "imgw fetcher.py" --backfill       # backfill Open-Meteo od 2026-03-03
  python "imgw fetcher.py" --date 2026-03-04
"""

import os, sys, json, sqlite3, logging, argparse, requests
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

DB_PATH   = os.getenv("DB_PATH", "tatry_segments.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

STACJE = {
    # -- IMGW synop - fizyczne stacje pomiarowe -----------------------------------
    "kasprowy_wierch": {
        "nazwa": "Kasprowy Wierch", "lat": 49.2319, "lon": 19.9817, "alt": 1987,
        "kraj": "PL", "zrodlo": "imgw", "imgw_id": "12650",
    },
    "zakopane": {
        "nazwa": "Zakopane", "lat": 49.2992, "lon": 19.9742, "alt": 857,
        "kraj": "PL", "zrodlo": "imgw", "imgw_id": "12640",
    },
    # -- Open-Meteo NWP - model numeryczny ---------------------------------------
    "morskie_oko": {
        "nazwa": "Morskie Oko", "lat": 49.2003, "lon": 20.0694, "alt": 1395,
        "kraj": "PL", "zrodlo": "open-meteo",
    },
    "hala_gasienicowa": {
        "nazwa": "Hala Gasienicowa", "lat": 49.2347, "lon": 20.0006, "alt": 1520,
        "kraj": "PL", "zrodlo": "open-meteo",
    },
    "dolina_chocholowska": {
        "nazwa": "Dolina Chocholowska", "lat": 49.2808, "lon": 19.8472, "alt": 1146,
        "kraj": "PL", "zrodlo": "open-meteo",
    },
    "lomnica": {
        "nazwa": "Lomnica", "lat": 49.1953, "lon": 20.2131, "alt": 2634,
        "kraj": "SK", "zrodlo": "open-meteo",
    },
    "strbske_pleso": {
        "nazwa": "Szczyrbskie Jezioro", "lat": 49.1197, "lon": 20.0611, "alt": 1346,
        "kraj": "SK", "zrodlo": "open-meteo",
    },
    "tatrzanska_kotlina": {
        "nazwa": "Tatrzanska Kotlina", "lat": 49.1667, "lon": 20.1167, "alt": 668,
        "kraj": "SK", "zrodlo": "open-meteo",
    },
}

IMGW_URL    = "https://danepubliczne.imgw.pl/api/data/synop"
OM_ARCHIVE  = "https://archive-api.open-meteo.com/v1/archive"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
OM_VARS     = "temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation,surface_pressure"

logging.basicConfig(level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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
    godzina_pomiaru TEXT,
    UNIQUE(station_key, captured_at)
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Migracja: dodaj kolumny jesli nie istnieja (stara baza bez nich)
    for col, typedef in [
        ("godzina_pomiaru", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE weather_snapshots ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass  # kolumna juz istnieje
    conn.commit()
    return conn


def sf(v):
    try: return float(v) if v is not None and v != "" else None
    except: return None

def si(v):
    try: return int(float(v)) if v is not None and v != "" else None
    except: return None


def upsert(conn, key, today, temp, wind, wdir, hum, rain, press, godzina=None):
    conn.execute("""
        INSERT INTO weather_snapshots
            (station_key,captured_at,temperatura,predkosc_wiatru,kierunek_wiatru,
             wilgotnosc,suma_opadu,cisnienie,godzina_pomiaru)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(station_key,captured_at) DO UPDATE SET
            temperatura=excluded.temperatura, predkosc_wiatru=excluded.predkosc_wiatru,
            kierunek_wiatru=excluded.kierunek_wiatru, wilgotnosc=excluded.wilgotnosc,
            suma_opadu=excluded.suma_opadu, cisnienie=excluded.cisnienie,
            godzina_pomiaru=excluded.godzina_pomiaru
    """, (key, today, temp, wind, wdir, hum, rain, press, godzina))
    conn.commit()


# -- IMGW -----------------------------------------------------------------------

def collect_imgw(conn, today):
    log.info("IMGW: pobieranie...")
    try:
        r = requests.get(IMGW_URL, timeout=30)
        r.raise_for_status()
        dane_all = {s["id_stacji"]: s for s in r.json()}
    except Exception as e:
        log.error(f"IMGW blad: {e}"); return

    for key, meta in STACJE.items():
        if meta["zrodlo"] != "imgw": continue
        dane = dane_all.get(meta["imgw_id"])
        if not dane:
            log.warning(f"IMGW: brak danych dla {meta['nazwa']}"); continue

        godzina = None
        try:
            dp = dane.get("data_pomiaru", "")
            gp = dane.get("godzina_pomiaru", "")
            if dp and gp:
                godzina = f"{dp}T{int(gp):02d}:00"
        except: pass

        upsert(conn, key, today,
               sf(dane.get("temperatura")), sf(dane.get("predkosc_wiatru")),
               si(dane.get("kierunek_wiatru")), sf(dane.get("wilgotnosc_wzgledna")),
               sf(dane.get("suma_opadu")), sf(dane.get("cisnienie")), godzina)
        log.info(f"  {meta['nazwa']}: {dane.get('temperatura')}C, "
                 f"wiatr {dane.get('predkosc_wiatru')} m/s, pomiar: {godzina}")


# -- Open-Meteo -----------------------------------------------------------------

def collect_open_meteo_date(conn, today):
    today_date = date.fromisoformat(today)
    is_today   = (today_date == date.today())

    for key, meta in STACJE.items():
        if meta["zrodlo"] != "open-meteo": continue
        log.info(f"Open-Meteo: {meta['nazwa']} ({today})...")
        try:
            if is_today:
                r = requests.get(OM_FORECAST, params={
                    "latitude": meta["lat"], "longitude": meta["lon"],
                    "hourly": OM_VARS, "past_days": 1, "forecast_days": 1,
                    "timezone": "Europe/Warsaw",
                }, timeout=30)
            else:
                r = requests.get(OM_ARCHIVE, params={
                    "latitude": meta["lat"], "longitude": meta["lon"],
                    "hourly": OM_VARS, "start_date": today, "end_date": today,
                    "timezone": "Europe/Warsaw",
                }, timeout=30)
            r.raise_for_status()
            hourly = r.json().get("hourly", {})
        except Exception as e:
            log.error(f"Open-Meteo blad ({meta['nazwa']}): {e}"); continue

        times  = hourly.get("time", [])
        idx_d  = [i for i, t in enumerate(times) if t.startswith(today)]
        if not idx_d: continue

        def vals(k): return [hourly.get(k,[])[i] for i in idx_d if i < len(hourly.get(k,[])) and hourly[k][i] is not None]
        def avg(l): return round(sum(l)/len(l),1) if l else None
        def mx(l):  return round(max(l),1) if l else None
        def sm(l):  return round(sum(l),1) if l else None

        temps = vals("temperature_2m"); winds = vals("wind_speed_10m")
        wdirs = vals("wind_direction_10m"); hums = vals("relative_humidity_2m")
        rains = vals("precipitation"); press = vals("surface_pressure")

        wdir = None
        if winds and wdirs:
            wdir = int(wdirs[winds.index(max(winds))]) if wdirs else None

        godzina = times[idx_d[-1]] if idx_d else None
        upsert(conn, key, today, avg(temps), mx(winds), wdir,
               avg(hums), sm(rains), avg(press), godzina)
        log.info(f"  {meta['nazwa']}: {avg(temps)}C, wiatr max {mx(winds)} m/s")


# -- Live JSON (odswiezanie co godzine) -----------------------------------------

def fetch_live_json(output_path="weather_data.json"):
    """
    Pobiera aktualne dane ze wszystkich zrodel i zapisuje weather_data.json.
    IMGW: godzina pomiaru z pola godzina_pomiaru.
    Open-Meteo: ostatnia pelna godzina.
    Zapisuje last_updated w formacie ISO "YYYY-MM-DDTHH:MM".
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    today   = date.today().isoformat()
    result  = {}

    for key, meta in STACJE.items():
        result[key] = {
            "meta": {"nazwa": meta["nazwa"], "lat": meta["lat"],
                     "lon": meta["lon"], "alt": meta["alt"], "kraj": meta["kraj"]},
            "series": {}, "last_updated": None,
        }

    # IMGW
    try:
        r = requests.get(IMGW_URL, timeout=30)
        r.raise_for_status()
        dane_all = {s["id_stacji"]: s for s in r.json()}
        for key, meta in STACJE.items():
            if meta["zrodlo"] != "imgw": continue
            dane = dane_all.get(meta["imgw_id"])
            if not dane: continue
            godzina = None
            try:
                dp, gp = dane.get("data_pomiaru",""), dane.get("godzina_pomiaru","")
                if dp and gp: godzina = f"{dp}T{int(gp):02d}:00"
            except: pass
            result[key]["series"][today] = {
                "temperatura":     sf(dane.get("temperatura")),
                "predkosc_wiatru": sf(dane.get("predkosc_wiatru")),
                "kierunek_wiatru": si(dane.get("kierunek_wiatru")),
                "wilgotnosc":      sf(dane.get("wilgotnosc_wzgledna")),
                "suma_opadu":      sf(dane.get("suma_opadu")),
                "cisnienie":       sf(dane.get("cisnienie")),
            }
            result[key]["last_updated"] = godzina or now_iso
            log.info(f"IMGW live {meta['nazwa']}: {dane.get('temperatura')}C, pomiar: {godzina}")
    except Exception as e:
        log.error(f"IMGW live blad: {e}")

    # Open-Meteo - ostatnia pelna godzina
    now_h = datetime.now().strftime("%Y-%m-%dT%H:00")
    for key, meta in STACJE.items():
        if meta["zrodlo"] != "open-meteo": continue
        try:
            r = requests.get(OM_FORECAST, params={
                "latitude": meta["lat"], "longitude": meta["lon"],
                "hourly": OM_VARS, "past_days": 0, "forecast_days": 1,
                "timezone": "Europe/Warsaw",
            }, timeout=30)
            r.raise_for_status()
            hourly = r.json().get("hourly", {})
            times  = hourly.get("time", [])

            past = [i for i, t in enumerate(times) if t <= now_h]
            if not past: continue
            i = past[-1]

            def hv(field):
                v = hourly.get(field, [])
                return v[i] if i < len(v) else None

            result[key]["series"][today] = {
                "temperatura":     hv("temperature_2m"),
                "predkosc_wiatru": hv("wind_speed_10m"),
                "kierunek_wiatru": int(hv("wind_direction_10m")) if hv("wind_direction_10m") is not None else None,
                "wilgotnosc":      hv("relative_humidity_2m"),
                "suma_opadu":      hv("precipitation"),
                "cisnienie":       hv("surface_pressure"),
            }
            result[key]["last_updated"] = times[i]
            log.info(f"Open-Meteo live {meta['nazwa']}: {hv('temperature_2m')}C @ {times[i]}")
        except Exception as e:
            log.error(f"Open-Meteo live blad ({meta['nazwa']}): {e}")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log.info(f"Live JSON zapisany: {output_path} ({len(result)} stacji)")
    return result


# -- Kolekcja dzienna (do DB) ---------------------------------------------------

def collect(today=None):
    if not today: today = date.today().isoformat()
    conn = get_db()
    collect_imgw(conn, today)
    collect_open_meteo_date(conn, today)
    conn.close()
    log.info("Kolekcja zakonczona.")


# -- Eksport z DB do JSON -------------------------------------------------------

def export_json(output_path="weather_data.json"):
    conn    = get_db()
    series  = defaultdict(dict)
    updated = {}
    for row in conn.execute("""
        SELECT station_key,captured_at,temperatura,predkosc_wiatru,
               kierunek_wiatru,wilgotnosc,suma_opadu,cisnienie,godzina_pomiaru
        FROM weather_snapshots ORDER BY captured_at
    """):
        series[row["station_key"]][row["captured_at"]] = {
            "temperatura":     row["temperatura"],
            "predkosc_wiatru": row["predkosc_wiatru"],
            "kierunek_wiatru": row["kierunek_wiatru"],
            "wilgotnosc":      row["wilgotnosc"],
            "suma_opadu":      row["suma_opadu"],
            "cisnienie":       row["cisnienie"],
        }
        if row["godzina_pomiaru"]:
            updated[row["station_key"]] = row["godzina_pomiaru"]

    result = {}
    for key, meta in STACJE.items():
        result[key] = {
            "meta": {"nazwa": meta["nazwa"], "lat": meta["lat"],
                     "lon": meta["lon"], "alt": meta["alt"], "kraj": meta["kraj"]},
            "series": series.get(key, {}),
            "last_updated": updated.get(key),
        }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    total = sum(len(v["series"]) for v in result.values())
    log.info(f"Eksport: {output_path} ({total} rekordow, {len(result)} stacji)")
    conn.close()


def report():
    conn = get_db()
    rows = conn.execute(
        "SELECT station_key,captured_at,temperatura,predkosc_wiatru,godzina_pomiaru "
        "FROM weather_snapshots ORDER BY station_key,captured_at"
    ).fetchall()
    if not rows: print("Brak danych w weather_snapshots.")
    else:
        print(f"\n{'='*80}\n  Dane pogodowe ({len(rows)} rekordow)\n{'='*80}")
        for r in rows:
            print(f"  {r['station_key']:<22} {r['captured_at']:<12} "
                  f"{str(r['temperatura'])+'C':>8} {str(r['predkosc_wiatru'])+' m/s':>9} "
                  f"  pomiar: {r['godzina_pomiaru'] or '?'}")
    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export",   action="store_true", help="Eksport DB -> JSON")
    p.add_argument("--report",   action="store_true", help="Podglad DB")
    p.add_argument("--live",     action="store_true", help="Live JSON bez DB (co godzine)")
    p.add_argument("--date",     default=None,        help="Konkretna data YYYY-MM-DD")
    p.add_argument("--backfill", action="store_true", help="Backfill Open-Meteo od 2026-03-03")
    args = p.parse_args()

    if args.report:   report();           return
    if args.export:   export_json();      return
    if args.live:     fetch_live_json();  return
    if args.backfill:
        conn = get_db()
        d = date(2026, 3, 3)
        while d < date.today():
            collect_open_meteo_date(conn, d.isoformat())
            d += timedelta(days=1)
        conn.close(); export_json(); return

    collect(today=args.date)
    export_json()


if __name__ == "__main__":
    main()
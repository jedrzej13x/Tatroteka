"""
TATRY FLOW — Strava Segment Collector
======================================
Codziennie odpytuje Strava API o segmenty w bbox TPN/TANAP,
zapisuje snapshoty do SQLite i buduje szereg czasowy natężenia ruchu.

Uruchomienie:
    python collector.py               # jednorazowy snapshot
    python collector.py --init        # pierwsze uruchomienie (tworzy bazę)
    python collector.py --report      # podsumowanie zebranych danych

Automatyzacja (cron):
    0 6 * * * /usr/bin/python3 /path/to/collector.py >> /var/log/tatry.log 2>&1
"""

import os
import sys
import time
import json
import math
import sqlite3
import logging
import argparse
import requests
from datetime import datetime, date

# Ładuje zmienne z pliku .env jeśli istnieje
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv niezainstalowany — używa zmiennych systemowych

# ── Konfiguracja ───────────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID",     "TWÓJ_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "TWÓJ_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "TWÓJ_REFRESH_TOKEN")

DB_PATH       = os.getenv("DB_PATH", "tatry_segments.db")
LOG_LEVEL     = os.getenv("LOG_LEVEL", "INFO")

# Bbox TPN + TANAP (z małym marginesem)
BBOX = {
    "min_lat": 49.10,
    "max_lat": 49.35,
    "min_lng": 19.60,
    "max_lng": 20.25,
}

# Siatka sub-bbox — im więcej kafelków, tym więcej segmentów
# 5×5 = 25 requestów, wystarczy dla TPN/TANAP
GRID_ROWS = 5
GRID_COLS = 5

# Typy aktywności które nas interesują
ACTIVITY_TYPES = ["hiking", "running", "walking"]

# Strava API
TOKEN_URL    = "https://www.strava.com/oauth/token"
SEGMENTS_URL = "https://www.strava.com/api/v3/segments/explore"

# Rate limiting — bezpieczny odstęp między requestami
REQUEST_DELAY = 12.0  # sekundy

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tatry")

# ── Baza danych ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    activity_type   TEXT,
    start_lat       REAL,
    start_lng       REAL,
    end_lat         REAL,
    end_lng         REAL,
    climb_category  INTEGER,
    avg_grade       REAL,
    elev_difference REAL,
    distance        REAL,
    polyline        TEXT,
    osm_way_id      INTEGER,
    first_seen      TEXT,
    last_seen       TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id     INTEGER NOT NULL,
    captured_at    TEXT NOT NULL,
    effort_count   INTEGER,
    athlete_count  INTEGER,
    FOREIGN KEY (segment_id) REFERENCES segments(id),
    UNIQUE(segment_id, captured_at)
);

CREATE TABLE IF NOT EXISTS collection_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    tiles_queried INTEGER,
    segments_found INTEGER,
    snapshots_saved INTEGER,
    errors       INTEGER,
    status       TEXT
);

CREATE VIEW IF NOT EXISTS traffic AS
    SELECT
        s1.segment_id,
        s1.captured_at                    AS date,
        s1.effort_count                   AS effort_count_cumulative,
        s1.effort_count - COALESCE(
            (SELECT s2.effort_count
             FROM snapshots s2
             WHERE s2.segment_id = s1.segment_id
               AND s2.captured_at < s1.captured_at
             ORDER BY s2.captured_at DESC
             LIMIT 1), 0
        )                                 AS daily_efforts,
        s1.athlete_count
    FROM snapshots s1;
"""


def get_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    log.info(f"Inicjalizacja bazy danych: {DB_PATH}")
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.info("Baza gotowa.")

# ── Autoryzacja Strava ─────────────────────────────────────────────────────────

def get_access_token():
    """
    Strava używa OAuth2 z refresh tokenem.
    Access token wygasa po 6h — zawsze odświeżamy przed kolekcją.
    """
    log.info("Pobieram access token ze Strava...")
    resp = requests.post(TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires = datetime.fromtimestamp(data["expires_at"]).strftime("%H:%M:%S")
    log.info(f"Token OK, wygasa o {expires}")
    return token

# ── Siatka kafelków ────────────────────────────────────────────────────────────

def build_tiles(bbox, rows, cols):
    """
    Dzieli bbox na siatkę rows×cols sub-obszarów.
    Każdy kafelek = osobny request do Strava API.

    Strava segments/explore przyjmuje:
    bounds = "min_lat,min_lng,max_lat,max_lng"
    """
    tiles = []
    lat_step = (bbox["max_lat"] - bbox["min_lat"]) / rows
    lng_step = (bbox["max_lng"] - bbox["min_lng"]) / cols

    for r in range(rows):
        for c in range(cols):
            tile = {
                "min_lat": bbox["min_lat"] + r * lat_step,
                "max_lat": bbox["min_lat"] + (r + 1) * lat_step,
                "min_lng": bbox["min_lng"] + c * lng_step,
                "max_lng": bbox["min_lng"] + (c + 1) * lng_step,
            }
            tiles.append(tile)

    log.info(f"Siatka {rows}×{cols} = {len(tiles)} kafelków")
    return tiles

# ── Pobieranie segmentów ───────────────────────────────────────────────────────

def fetch_segments_for_tile(tile, activity_type, token):
    """
    Pobiera segmenty z jednego kafelka dla jednego typu aktywności.
    Zwraca listę segmentów lub [] w przypadku błędu.
    """
    bounds = f"{tile['min_lat']},{tile['min_lng']},{tile['max_lat']},{tile['max_lng']}"
    params = {
        "bounds":        bounds,
        "activity_type": activity_type,
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(SEGMENTS_URL, params=params, headers=headers, timeout=30)

        # Rate limit hit
        if resp.status_code == 429:
            log.warning("Rate limit! Czekam 15 minut...")
            time.sleep(900)
            return []

        # Unauthorized
        if resp.status_code == 401:
            log.error("Token wygasł lub nieprawidłowy!")
            return []

        resp.raise_for_status()
        data = resp.json()
        segments = data.get("segments", [])
        return segments

    except requests.RequestException as e:
        log.error(f"Błąd requestu dla kafelka {bounds}: {e}")
        return []


def upsert_segment(conn, seg, activity_type, today):
    """
    Wstawia nowy segment lub aktualizuje last_seen jeśli już istnieje.
    """
    existing = conn.execute(
        "SELECT id FROM segments WHERE id = ?", (seg["id"],)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE segments SET last_seen = ? WHERE id = ?",
            (today, seg["id"])
        )
    else:
        conn.execute("""
            INSERT INTO segments
                (id, name, activity_type, start_lat, start_lng,
                 end_lat, end_lng, climb_category, avg_grade,
                 elev_difference, distance, polyline, first_seen, last_seen)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            seg["id"],
            seg.get("name", ""),
            activity_type,
            seg.get("start_latlng", [None, None])[0],
            seg.get("start_latlng", [None, None])[1],
            seg.get("end_latlng",   [None, None])[0],
            seg.get("end_latlng",   [None, None])[1],
            seg.get("climb_category", 0),
            seg.get("avg_grade", 0),
            seg.get("elev_difference", 0),
            seg.get("distance", 0),
            json.dumps(seg.get("points", "")),
            today,
            today,
        ))
        log.debug(f"Nowy segment: [{seg['id']}] {seg.get('name', '?')}")


def fetch_segment_detail(segment_id, token):
    """
    Pobiera szczegóły pojedynczego segmentu przez GET /segments/{id}.
    To jedyny endpoint który zwraca prawdziwy effort_count i athlete_count.
    """
    url     = f"https://www.strava.com/api/v3/segments/{segment_id}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code == 429:
            log.warning("Rate limit! Czekam 15 minut...")
            time.sleep(900)
            return None

        if resp.status_code == 401:
            log.error("Token wygasł!")
            return None

        if resp.status_code == 404:
            log.debug(f"Segment {segment_id} nie istnieje")
            return None

        resp.raise_for_status()
        return resp.json()

    except requests.RequestException as e:
        log.error(f"Błąd pobierania segmentu {segment_id}: {e}")
        return None


def save_snapshot(conn, segment_id, effort_count, athlete_count, captured_at):
    """
    Zapisuje snapshot. UNIQUE constraint zapobiega duplikatom.
    """
    try:
        conn.execute("""
            INSERT OR IGNORE INTO snapshots
                (segment_id, captured_at, effort_count, athlete_count)
            VALUES (?, ?, ?, ?)
        """, (segment_id, captured_at, effort_count, athlete_count))
        return conn.total_changes > 0
    except sqlite3.Error as e:
        log.error(f"Błąd zapisu snapshotu {segment_id}: {e}")
        return False

# ── Główna pętla kolekcji ──────────────────────────────────────────────────────

def collect(token):
    """
    Główna funkcja kolekcji — dwa etapy:
    1. segments/explore → zbiera ID segmentów z siatki kafelków
    2. /segments/{id}   → pobiera prawdziwy effort_count dla każdego ID
    """
    today      = date.today().isoformat()
    started_at = datetime.now().isoformat()
    tiles      = build_tiles(BBOX, GRID_ROWS, GRID_COLS)

    total_segments  = 0
    total_snapshots = 0
    total_errors    = 0
    seen_ids        = set()  # deduplikacja między kafelkami

    conn = get_db()

    # ── Etap 1: zbierz wszystkie ID segmentów z siatki ──────────────────────
    log.info("Etap 1: zbieranie ID segmentów z siatki kafelków...")
    for tile_idx, tile in enumerate(tiles, 1):
        for activity_type in ACTIVITY_TYPES:
            log.debug(f"Kafelek {tile_idx}/{len(tiles)} | {activity_type}")
            segments = fetch_segments_for_tile(tile, activity_type, token)
            for seg in segments:
                if seg["id"] not in seen_ids:
                    upsert_segment(conn, seg, activity_type, today)
                    seen_ids.add(seg["id"])
            conn.commit()
            time.sleep(REQUEST_DELAY)

    log.info(f"Znaleziono {len(seen_ids)} unikalnych segmentów")

    # ── Etap 2: pobierz effort_count dla każdego segmentu ───────────────────
    log.info("Etap 2: pobieranie effort_count per segment...")
    all_ids = [row[0] for row in conn.execute("SELECT id FROM segments").fetchall()]

    for i, seg_id in enumerate(all_ids, 1):
        if i % 50 == 0:
            log.info(f"  Postęp: {i}/{len(all_ids)} segmentów...")

        detail = fetch_segment_detail(seg_id, token)
        if detail is None:
            total_errors += 1
            continue

        effort_count  = detail.get("effort_count",  0)
        athlete_count = detail.get("athlete_count", 0)

        saved = save_snapshot(conn, seg_id, effort_count, athlete_count, today)
        if saved:
            total_snapshots += 1
        total_segments += 1

        conn.commit()
        time.sleep(REQUEST_DELAY)

    # Zapisz log kolekcji
    finished_at = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO collection_log
            (started_at, finished_at, tiles_queried, segments_found,
             snapshots_saved, errors, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (started_at, finished_at, len(tiles) * len(ACTIVITY_TYPES),
          total_segments, total_snapshots, total_errors, "OK"))
    conn.commit()
    conn.close()

    log.info(f"=== Kolekcja zakończona ===")
    log.info(f"Segmentów: {total_segments} | Snapshotów: {total_snapshots} | Błędów: {total_errors}")

# ── Raport ─────────────────────────────────────────────────────────────────────

def report():
    """Podsumowanie zebranych danych."""
    conn = get_db()

    total_segments  = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    total_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    date_range      = conn.execute(
        "SELECT MIN(captured_at), MAX(captured_at) FROM snapshots"
    ).fetchone()

    print(f"\n{'='*50}")
    print(f"  TATRY FLOW — Raport bazy danych")
    print(f"{'='*50}")
    print(f"  Segmentów w bazie:  {total_segments}")
    print(f"  Snapshotów łącznie: {total_snapshots}")
    print(f"  Zakres dat:         {date_range[0]} → {date_range[1]}")

    print(f"\n  TOP 10 segmentów wg aktywności (ostatni snapshot):")
    top = conn.execute("""
        SELECT s.name, s.activity_type, sn.effort_count, sn.captured_at
        FROM segments s
        JOIN snapshots sn ON s.id = sn.segment_id
        WHERE sn.captured_at = (
            SELECT MAX(captured_at) FROM snapshots WHERE segment_id = s.id
        )
        ORDER BY sn.effort_count DESC
        LIMIT 10
    """).fetchall()

    for i, row in enumerate(top, 1):
        print(f"  {i:2}. [{row['effort_count']:>8}] {row['name'][:45]} ({row['activity_type']})")

    print(f"\n  Historia kolekcji:")
    logs = conn.execute("""
        SELECT started_at, segments_found, snapshots_saved, errors, status
        FROM collection_log
        ORDER BY started_at DESC
        LIMIT 10
    """).fetchall()

    for row in logs:
        print(f"  {row['started_at'][:10]} | "
              f"segm: {row['segments_found']:3} | "
              f"snap: {row['snapshots_saved']:3} | "
              f"err: {row['errors']:2} | {row['status']}")

    print(f"{'='*50}\n")
    conn.close()


def export_traffic_json(output_path="traffic_data.json"):
    """
    Eksportuje szereg czasowy natężenia do JSON dla frontendu.
    Format: { segment_id: { date: daily_efforts, ... }, ... }
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT segment_id, date, daily_efforts, effort_count_cumulative
        FROM traffic
        WHERE daily_efforts >= 0
        ORDER BY segment_id, date
    """).fetchall()

    # Wzbogać o geometrię segmentu
    segments_meta = {}
    for row in conn.execute("SELECT id, name, start_lat, start_lng, polyline FROM segments"):
        segments_meta[row["id"]] = {
            "name":      row["name"],
            "lat":       row["start_lat"],
            "lng":       row["start_lng"],
            "polyline":  row["polyline"],
        }

    result = {}
    for row in rows:
        sid = row["segment_id"]
        if sid not in result:
            result[sid] = {
                "meta":   segments_meta.get(sid, {}),
                "series": {}
            }
        result[sid]["series"][row["date"]] = row["daily_efforts"]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info(f"Eksport JSON: {output_path} ({len(result)} segmentów)")
    conn.close()

# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tatry Flow — Strava Collector")
    parser.add_argument("--init",   action="store_true", help="Inicjalizuj bazę danych")
    parser.add_argument("--report", action="store_true", help="Pokaż raport")
    parser.add_argument("--export", action="store_true", help="Eksportuj JSON dla frontendu")
    args = parser.parse_args()

    if args.init:
        init_db()
        return

    if args.report:
        report()
        return

    if args.export:
        export_traffic_json()
        return

    # Domyślnie: zbierz snapshoty
    init_db()  # idempotent — bezpieczne przy każdym uruchomieniu
    try:
        token = get_access_token()
        collect(token)
    except KeyboardInterrupt:
        log.info("Przerwano przez użytkownika.")
    except Exception as e:
        log.error(f"Krytyczny błąd: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
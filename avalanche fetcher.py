"""
TATRY FLOW — Avalanche Bulletin Fetcher
Źródła:
  - lawiny.topr.pl/getwidget  → Tatry Polskie (TOPR)
  - hzs.sk/vysoke-tatry/      → Wysokie Tatry SK (HZS)
  - hzs.sk/zapadne-tatry/     → Zachodnie Tatry SK (HZS)

Zapis: tatry_segments.db (tabela avalanche_bulletins) + avalanche_data.json

Użycie:
  python avalanche_fetcher.py              # pobierz dziś + eksportuj
  python avalanche_fetcher.py --export     # tylko eksport JSON z bazy
  python avalanche_fetcher.py --report     # podgląd danych w bazie
"""

import os
import re
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

# Stopnie lawinowe EAWS — nazwa polska / słowacka
STOPNIE_PL = {1: "Małe", 2: "Umiarkowane", 3: "Znaczne", 4: "Duże", 5: "Bardzo duże"}
STOPNIE_SK = {1: "Malé", 2: "Mierne", 3: "Zvýšené", 4: "Veľké", 5: "Veľmi veľké"}

# Kolory EAWS (standard europejski)
KOLORY = {1: "#8BC34A", 2: "#FFC107", 3: "#FF9800", 4: "#F44336", 5: "#7B1FA2"}

ŹRÓDŁA = {
    "topr_tatry_polskie": {
        "nazwa":  "Tatry Polskie (TOPR)",
        "url":    "https://lawiny.topr.pl/getwidget",
        "region": "PL",
        "parser": "topr",
    },
    "hzs_wysokie_tatry": {
        "nazwa":  "Wysokie Tatry (HZS)",
        "url":    "https://hzs.sk/vysoke-tatry/",
        "region": "SK",
        "parser": "hzs",
    },
    "hzs_zachodnie_tatry": {
        "nazwa":  "Zachodnie Tatry (HZS)",
        "url":    "https://hzs.sk/zapadne-tatry/",
        "region": "SK",
        "parser": "hzs",
    },
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("avalanche")

SCHEMA = """
CREATE TABLE IF NOT EXISTS avalanche_bulletins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key   TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    stopien      INTEGER,
    stopien_nazwa TEXT,
    tendencja    TEXT,
    wazne_do     TEXT,
    opis         TEXT,
    UNIQUE(source_key, captured_at)
);
"""

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TatrotekaBottatroteka.pl)"
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Parsery ────────────────────────────────────────────────────────────────────

def parse_topr(html):
    """
    Parsuje widget TOPR: document.write('...')
    Zwraca dict z kluczami: stopien, stopien_nazwa, tendencja, wazne_do, opis
    """
    # Wyciągnij treść z document.write(...)
    m = re.search(r"document\.write\('(.+?)'\)", html, re.DOTALL)
    if not m:
        # Spróbuj bez apostrofów (wariant z cudzysłowami)
        m = re.search(r'document\.write\("(.+?)"\)', html, re.DOTALL)
    tekst = m.group(1) if m else html

    # Ważność: "Obowiązuje do: 05.03.2026 20:00"
    wazne_do = None
    m = re.search(r'Obowi[aą]zuje do:\s*([\d\.\s:]+)', tekst)
    if m:
        wazne_do = m.group(1).strip()

    # Stopień słowny: "Zagrożenie określono jako: **Umiarkowane**"
    stopien_nazwa = None
    stopien = None
    m = re.search(r'Zagro[żz]enie okre[sś]lono jako:\s*\**([A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ ]+?)\**\s*[\n<]', tekst)
    if m:
        stopien_nazwa = m.group(1).strip()
        # Mapuj nazwę na numer
        for k, v in STOPNIE_PL.items():
            if v.lower() == stopien_nazwa.lower():
                stopien = k
                break

    # Tendencja: "Stopień zagrożenia może maleć / rosnąć / nie powinien ulec zmianie"
    tendencja = None
    m = re.search(r'(Stopie[ńn] zagro[żż]enia[^<\n]{5,60})', tekst)
    if m:
        t = m.group(1).strip()
        if "okre[śs]lono" not in t:
            tendencja = t

    return {
        "stopien":      stopien,
        "stopien_nazwa": stopien_nazwa,
        "tendencja":    tendencja,
        "wazne_do":     wazne_do,
        "opis":         None,
    }


def parse_hzs(html):
    """
    Parsuje stronę HZS: wyciąga stopień z alt tekstu obrazka SVG.
    danger_rating_2.svg → stopień 2
    Opis z sekcji Výstrahy.
    """
    stopien = None
    stopien_nazwa = None

    # Szukaj danger_rating_N.svg w alt lub src
    m = re.search(r'danger_rating_(\d)\.svg["\s]+(?:/>|>)?[^"]*?"([^"]*)"', html)
    if not m:
        m = re.search(r'alt="[^"]*?(\d)[^\d][^"]*?lavín[^"]*"', html, re.IGNORECASE)
    if not m:
        # Szukaj po alt zawierającym stopień słowny SK
        for k, v in STOPNIE_SK.items():
            if v.lower() in html.lower():
                stopien = k
                stopien_nazwa = v
                break
    else:
        # Spróbuj wyciągnąć numer z nazwy pliku
        nm = re.search(r'danger_rating_(\d)', html)
        if nm:
            stopien = int(nm.group(1))
            stopien_nazwa = STOPNIE_SK.get(stopien)

    # Jeśli ciągle brak — spróbuj alt obrazka bezpośrednio
    if not stopien:
        m = re.search(r'alt="[^"]*?(\bMalé\b|\bMierne\b|\bZvýšené\b|\bVeľké\b|\bVeľmi veľké\b)[^"]*"', html)
        if m:
            nazwa_raw = m.group(1)
            for k, v in STOPNIE_SK.items():
                if v in nazwa_raw:
                    stopien = k
                    stopien_nazwa = v

    # Opis: pierwsze zdanie z sekcji Výstrahy po opisie lawinowym
    opis = None
    m = re.search(r'lavínové nebezpečenstvo.+?</li>\s*(?:<li>[^<]*</li>\s*)*(.{20,200}?)</[a-z]', html, re.DOTALL)
    if m:
        opis_raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if len(opis_raw) > 15:
            opis = opis_raw[:200]

    return {
        "stopien":       stopien,
        "stopien_nazwa": stopien_nazwa,
        "tendencja":     None,
        "wazne_do":      None,
        "opis":          opis,
    }


# ── Kolekcja ───────────────────────────────────────────────────────────────────

def collect(today=None):
    if today is None:
        today = date.today().isoformat()

    conn = get_db()

    for key, meta in ŹRÓDŁA.items():
        log.info(f"Pobieram: {meta['nazwa']}...")
        try:
            resp = requests.get(meta["url"], headers=HEADERS, timeout=30)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            log.error(f"Błąd pobierania {key}: {e}")
            continue

        if meta["parser"] == "topr":
            dane = parse_topr(html)
        else:
            dane = parse_hzs(html)

        if dane["stopien"] is None:
            log.warning(f"{meta['nazwa']}: nie udało się sparsować stopnia zagrożenia")
        else:
            log.info(f"{meta['nazwa']}: stopień {dane['stopien']} ({dane['stopien_nazwa']})"
                     + (f", tendencja: {dane['tendencja']}" if dane["tendencja"] else ""))

        try:
            conn.execute("""
                INSERT INTO avalanche_bulletins
                    (source_key, captured_at, stopien, stopien_nazwa,
                     tendencja, wazne_do, opis)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(source_key, captured_at) DO UPDATE SET
                    stopien       = excluded.stopien,
                    stopien_nazwa = excluded.stopien_nazwa,
                    tendencja     = excluded.tendencja,
                    wazne_do      = excluded.wazne_do,
                    opis          = excluded.opis
            """, (
                key, today,
                dane["stopien"], dane["stopien_nazwa"],
                dane["tendencja"], dane["wazne_do"], dane["opis"],
            ))
        except Exception as e:
            log.error(f"Błąd zapisu ({key}): {e}")

    conn.commit()
    conn.close()
    log.info("Kolekcja lawinowa zakończona.")


# ── Eksport JSON ───────────────────────────────────────────────────────────────

def export_json(output_path="avalanche_data.json"):
    """
    Buduje avalanche_data.json.

    Struktura:
    {
      "topr_tatry_polskie": {
        "meta": {"nazwa": "Tatry Polskie (TOPR)", "region": "PL"},
        "series": {
          "2026-03-04": {
            "stopien": 2,
            "stopien_nazwa": "Umiarkowane",
            "kolor": "#FFC107",
            "tendencja": "Stopień zagrożenia może maleć",
            "wazne_do": "05.03.2026 20:00",
            "opis": null
          }
        }
      },
      "hzs_wysokie_tatry": { ... },
      "hzs_zachodnie_tatry": { ... }
    }
    """
    conn = get_db()
    series_map = defaultdict(dict)

    for row in conn.execute("""
        SELECT source_key, captured_at, stopien, stopien_nazwa,
               tendencja, wazne_do, opis
        FROM avalanche_bulletins
        ORDER BY captured_at
    """):
        series_map[row["source_key"]][row["captured_at"]] = {
            "stopien":       row["stopien"],
            "stopien_nazwa": row["stopien_nazwa"],
            "kolor":         KOLORY.get(row["stopien"]),
            "tendencja":     row["tendencja"],
            "wazne_do":      row["wazne_do"],
            "opis":          row["opis"],
        }

    result = {}
    for key, meta in ŹRÓDŁA.items():
        result[key] = {
            "meta":   {"nazwa": meta["nazwa"], "region": meta["region"]},
            "series": series_map.get(key, {}),
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total = sum(len(v["series"]) for v in result.values())
    log.info(f"Eksport: {output_path} ({total} wpisów)")
    conn.close()


# ── Raport ─────────────────────────────────────────────────────────────────────

def report():
    conn = get_db()
    rows = conn.execute("""
        SELECT source_key, captured_at, stopien, stopien_nazwa, tendencja, wazne_do
        FROM avalanche_bulletins
        ORDER BY source_key, captured_at DESC
    """).fetchall()

    if not rows:
        print("Brak danych w avalanche_bulletins.")
        conn.close()
        return

    print(f"\n{'='*70}")
    print(f"  TATRY FLOW — Dane lawinowe ({len(rows)} wpisów)")
    print(f"{'='*70}")
    for row in rows:
        print(f"  {row['captured_at']}  {row['source_key']:<25} "
              f"stopień {row['stopien']} ({row['stopien_nazwa']:<15}) "
              f"{row['tendencja'] or ''}")
    conn.close()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tatry Flow — Avalanche Bulletin Fetcher")
    parser.add_argument("--export", action="store_true", help="Eksportuj avalanche_data.json z bazy")
    parser.add_argument("--report", action="store_true", help="Pokaż dane lawinowe z bazy")
    parser.add_argument("--date",   default=None,        help="Data kolekcji YYYY-MM-DD (domyślnie: dziś)")
    args = parser.parse_args()

    if args.report:
        report(); return
    if args.export:
        export_json(); return

    # Domyślnie: pobierz + eksportuj
    collect(today=args.date)
    export_json()


if __name__ == "__main__":
    main()
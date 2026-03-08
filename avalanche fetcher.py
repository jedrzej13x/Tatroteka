"""
TATRY FLOW - Avalanche Bulletin Fetcher
Zrodla:
  - lawiny.topr.pl/getwidget  -> Tatry Polskie (TOPR)
  - hzs.sk/vysoke-tatry/      -> Wysokie Tatry SK (HZS)
  - hzs.sk/zapadne-tatry/     -> Zachodnie Tatry SK (HZS)

Uzycie:
  python "avalanche fetcher.py"            # pobierz + DB + JSON
  python "avalanche fetcher.py" --live     # live JSON bez DB (do odswiezania)
  python "avalanche fetcher.py" --export   # eksport z DB
  python "avalanche fetcher.py" --report   # podglad
  python "avalanche fetcher.py" --test     # debug parsera
"""

import os, re, json, sqlite3, logging, argparse, requests
from datetime import date, datetime, timezone
from collections import defaultdict

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

DB_PATH   = os.getenv("DB_PATH", "tatry_segments.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

KOLORY = {1: "#8BC34A", 2: "#FFC107", 3: "#FF9800", 4: "#F44336", 5: "#7B1FA2"}

ZRODLA = {
    "topr_tatry_polskie": {
        "nazwa":  "Tatry Polskie (TOPR)",
        "url":    "https://lawiny.topr.pl/getwidget",
        "region": "PL", "parser": "topr",
    },
    "hzs_wysokie_tatry": {
        "nazwa":  "Wysokie Tatry (HZS)",
        "url":    "https://static.laviny.sk/simple/{date}/SK_sk.html",
        "region": "SK", "parser": "laviny_sk", "region_key": "tatry",
    },
    "hzs_zachodnie_tatry": {
        "nazwa":  "Zachodnie Tatry (HZS)",
        "url":    "https://static.laviny.sk/simple/{date}/SK_sk.html",
        "region": "SK", "parser": "laviny_sk", "region_key": "tatry",
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

logging.basicConfig(level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("avalanche")

SCHEMA = """
CREATE TABLE IF NOT EXISTS avalanche_bulletins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key    TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    stopien       INTEGER,
    stopien_nazwa TEXT,
    tendencja     TEXT,
    wazne_do      TEXT,
    opis          TEXT,
    last_updated  TEXT,
    UNIQUE(source_key, captured_at)
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    # Migracja: dodaj kolumny których może nie być w starej bazie
    # Sprawdzamy przez PRAGMA table_info zamiast polegać na wyjątku
    existing = {row[1] for row in conn.execute("PRAGMA table_info(avalanche_bulletins)")}
    for col, typedef in [("last_updated", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE avalanche_bulletins ADD COLUMN {col} {typedef}")
            conn.commit()
            log.info(f"Migracja: dodano kolumnę {col} do avalanche_bulletins")
    return conn


def _ascii(s):
    return (s.lower()
             .replace("\u0105","a").replace("\u0107","c").replace("\u0119","e")
             .replace("\u0142","l").replace("\u0144","n").replace("\u00f3","o")
             .replace("\u015b","s").replace("\u017a","z").replace("\u017c","z")
             .replace("\u010d","c").replace("\u013e","l").replace("\u0148","n")
             .replace("\u0161","s").replace("\u017e","z"))


MAPA_NAZW = [
    (5, ["bardzo duze", "bardzo duzy", "bardzo vysokie", "very high"]),
    (4, ["duze", "duzy", "vysokie", "velke", "velky", "high"]),
    (3, ["znaczne", "znaczny", "zvysene", "considerable"]),
    (2, ["umiarkowane", "umiarkowany", "mierne", "moderate"]),
    (1, ["male", "maly", "niskie", "low", "gering", "niske"]),
]


def nazwa_do_stopnia(s):
    if not s: return None
    a = _ascii(s)
    for nr, slowa in MAPA_NAZW:
        for slowo in slowa:
            if slowo in a:
                return nr
    return None


# -- Parser TOPR ----------------------------------------------------------------
# UWAGA: lawiny.topr.pl/getwidget zwraca document.write('...')
# Regex [\"'] zatrzymuje sie na pierwszym " wewnatrz tresci (np. w alt="...")
# Dlatego NIE uzywamy regex do wyciagania - usuwamy wrapper bezposrednio.

def parse_topr(raw):
    # Usun document.write wrapper - szukaj od pierwszego ( do ostatniego )
    tekst = raw
    # Usun "document.write( '" z poczatku i "' );" z konca
    tekst = re.sub(r"^\s*document\.write\s*\(\s*'", "", tekst, flags=re.DOTALL)
    tekst = re.sub(r"'\s*\)\s*;?\s*$", "", tekst, flags=re.DOTALL)

    log.debug("TOPR raw tekst (pierwsze 400): " + tekst[:400])

    # Usun HTML tagi
    tekst = re.sub(r"<[^>]+>", " ", tekst)
    # Usun Markdown bold
    tekst = re.sub(r"\*+([^*]+)\*+", r"\1", tekst)
    # Usun Markdown linki
    tekst = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", tekst)
    # Uprosz whitespace
    tekst = re.sub(r"[ \t]+", " ", tekst)

    log.debug("TOPR oczyszczony (pierwsze 400): " + tekst[:400])

    # -- Stopien: "Zagrozenie okreslono jako: Umiarkowane" --
    stopien, stopien_nazwa = None, None
    m = re.search(
        r"okre[s\u015b]lono\s+jako\s*:?\s*"
        r"(Bardzo\s+du[z\u017c]e|Du[z\u017c]e|Znaczne|Umiarkowane|Ma[l\u0142]e"
        r"|Bardzo\s+du[z\u017c]y|Du[z\u017c]y|Znaczny|Umiarkowany|Ma[l\u0142]y"
        r"|Niskie|Wysokie|Bardzo\s+[Ww]ysokie)",
        tekst, re.IGNORECASE
    )
    if m:
        stopien_nazwa = m.group(1).strip()
        stopien       = nazwa_do_stopnia(stopien_nazwa)

    # Fallback: cyfra po "stopien zagrozen"
    if stopien is None:
        m = re.search(r"stopie[n\u0144][^0-9]{0,30}([1-5])", tekst, re.IGNORECASE)
        if m:
            stopien = int(m.group(1))

    # Fallback: szukaj slowa kluczowego w calosci
    if stopien is None:
        for nr, slowa_pl in [(5,["Bardzo du\u017ce"]),(4,["Du\u017ce"]),(3,["Znaczne"]),
                             (2,["Umiarkowane"]),(1,["Ma\u0142e","Niskie"])]:
            for s in slowa_pl:
                if re.search(s, tekst, re.IGNORECASE):
                    stopien = nr; stopien_nazwa = s; break
            if stopien: break

    # -- Waznosc --
    wazne_do = None
    m = re.search(r"Obowi[a\u0105]zuje\s+do\s*:?\s*([\d\.\: ]+)", tekst, re.IGNORECASE)
    if m:
        wazne_do = m.group(1).strip()

    # -- Tendencja -- wyciągaj konkretną frazę ze zdania
    tendencja = None
    m = re.search(
        r"(Stopie[n\u0144]\s+zagro[z\u017c]enia\s+"
        r"(?:nie\s+powinien\s+ulec\s+zmianie"
        r"|mo[z\u017c]e\s+rosn[a\u0105][c\u0107]"
        r"|mo[z\u017c]e\s+male[c\u0107]"
        r"|pozostanie\s+bez\s+zmian))",
        tekst, re.IGNORECASE
    )
    if m:
        tendencja = m.group(1).strip()[:120]

    # -- Opis --
    opis = None
    skip = [r"Komunikat", r"Obowi[a\u0105]zuje", r"okre[s\u015b]lono", r"Stopie[n\u0144]",
            r"Szczeg", r"TURYSTO", r"Twoje bezpiecze", r"TOPR\b", r"^\s*$"]
    for line in tekst.split("\n"):
        l = line.strip()
        if len(l) < 40: continue
        if any(re.search(p, l, re.IGNORECASE) for p in skip): continue
        opis = l[:300]
        break

    log.info(f"TOPR: stopien={stopien} ({stopien_nazwa}), tendencja={tendencja}")
    return {"stopien": stopien, "stopien_nazwa": stopien_nazwa,
            "tendencja": tendencja, "wazne_do": wazne_do,
            "opis": opis, "kolor": KOLORY.get(stopien)}


# -- Parser HZS -----------------------------------------------------------------

def parse_hzs(html):
    stopien, stopien_nazwa = None, None
    STOPNIE_SK = {1:"Malé", 2:"Mierne", 3:"Zvýšené", 4:"Veľké", 5:"Veľmi veľké"}

    m = re.search(r"danger_rating_(\d)\.svg", html, re.IGNORECASE)
    if m:
        stopien       = int(m.group(1))
        stopien_nazwa = STOPNIE_SK.get(stopien)

    if not stopien:
        # Fallback: alt text
        m = re.search(r'alt=["\']([^"\']+stupen[^"\']+)["\']', html, re.IGNORECASE)
        if m:
            stopien_nazwa = m.group(1)
            stopien = nazwa_do_stopnia(stopien_nazwa)

    # Waznosc
    wazne_do = None
    m = re.search(r"platnost[^:]*:\s*([^\n<]{5,40})", html, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4}[^\n<]{0,20})", html)
    if m: wazne_do = m.group(1).strip()[:60]

    # Opis z sekcji Vystrazenia / ostrzezenia
    opis = None
    m = re.search(r"V[yý]strahy?[^<]{0,20}</[^>]+>(.*?)</", html, re.IGNORECASE | re.DOTALL)
    if m:
        opis_raw = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        opis = re.sub(r"\s+", " ", opis_raw)[:300] if len(opis_raw) > 5 else None

    log.info(f"HZS: stopien={stopien} ({stopien_nazwa})")
    return {"stopien": stopien, "stopien_nazwa": stopien_nazwa,
            "tendencja": None, "wazne_do": wazne_do,
            "opis": opis, "kolor": KOLORY.get(stopien)}


# -- Parser laviny.sk (static.laviny.sk/simple/YYYY-MM-DD/SK_sk.html) ----------
# Jedna strona zawiera dane dla wszystkich regionów SK.
# Sekcja "Tatry" (Vysoké, Západné, Nízke Tatry) - pierwsza sekcja bulletinu.

_laviny_sk_cache = {}  # cache: date -> html (żeby nie pobierać 2x dla 2 kluczy)

def parse_laviny_sk(html, region_key="tatry"):
    # Wymuś UTF-8 jeśli bytes, inaczej użyj jak jest
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    # Usuń tagi HTML, zostaw tekst
    tekst = re.sub(r"<[^>]+>", " ", html)
    tekst = re.sub(r"[ \t]+", " ", tekst)

    log.debug("laviny.sk tekst (pierwsze 500): " + tekst[:500])

    stopien = None
    stopien_nazwa = None

    # Szukaj "t.j 2. stupe" - nie używaj ň żeby uniknąć problemów z kodowaniem
    m = re.search(r"t\.j\.?\s*(\d)\.\s*stupe", tekst, re.IGNORECASE)
    if m:
        stopien = int(m.group(1))
        log.debug(f"laviny.sk: znaleziono stopien={stopien} przez 't.j X. stupe'")

    # Fallback: szukaj nazwy słownej przez ASCII (po przez _ascii który usuwa diakrytykę)
    tekst_ascii = _ascii(tekst)
    NAZWY_ASCII = [
        (5, "velmi velke", "Veľmi veľké"),
        (4, "velke", "Veľké"),
        (3, "zvysene", "Zvýšené"),
        (2, "mierne", "Mierne"),
        (1, "male", "Malé"),
    ]
    for nr, slowo, nazwa in NAZWY_ASCII:
        pattern = slowo + r".{0,30}lavinove nebezpecenstvo"
        if re.search(pattern, tekst_ascii, re.IGNORECASE):
            if stopien is None: stopien = nr
            stopien_nazwa = nazwa
            break

    if stopien and not stopien_nazwa:
        stopien_nazwa = {1:"Malé",2:"Mierne",3:"Zvýšené",4:"Veľké",5:"Veľmi veľké"}.get(stopien)

    # Tendencja - przez ASCII
    tendencja = None
    m3 = re.search(r"tendencia[^:]*:?\s*([^\n]{5,80})", tekst_ascii, re.IGNORECASE)
    if m3:
        tendencja = m3.group(1).strip()[:120]

    log.info(f"laviny.sk: stopien={stopien} ({stopien_nazwa}), tendencja={tendencja}")
    return {"stopien": stopien, "stopien_nazwa": stopien_nazwa,
            "tendencja": tendencja, "wazne_do": None,
            "opis": None, "kolor": KOLORY.get(stopien)}


# -- Pobierz + parsuj -----------------------------------------------------------

def pobierz_biuletyn(key, meta):
    today_str = date.today().isoformat()

    if meta["parser"] == "laviny_sk":
        url = meta["url"].replace("{date}", today_str)
        if url not in _laviny_sk_cache:
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                r.encoding = "utf-8"
                _laviny_sk_cache[url] = r.text
                log.info(f"laviny.sk: pobrano {len(r.text)} bajtów")
            except Exception as e:
                log.error(f"{key}: blad pobierania laviny.sk: {e}"); return None
        return parse_laviny_sk(_laviny_sk_cache[url], meta.get("region_key", "tatry"))

    try:
        r = requests.get(meta["url"], headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log.error(f"{key}: blad pobierania: {e}"); return None

    if meta["parser"] == "topr":
        return parse_topr(html)
    elif meta["parser"] == "hzs":
        return parse_hzs(html)
    return None


# -- Zapis do DB ----------------------------------------------------------------

def upsert_biuletyn(conn, key, today, dane):
    now_iso = datetime.now(timezone.utc).isoformat(timespec="minutes")
    conn.execute("""
        INSERT INTO avalanche_bulletins
            (source_key, captured_at, stopien, stopien_nazwa, tendencja, wazne_do, opis, last_updated)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(source_key,captured_at) DO UPDATE SET
            stopien=excluded.stopien, stopien_nazwa=excluded.stopien_nazwa,
            tendencja=excluded.tendencja, wazne_do=excluded.wazne_do,
            opis=excluded.opis, last_updated=excluded.last_updated
    """, (key, today, dane.get("stopien"), dane.get("stopien_nazwa"),
          dane.get("tendencja"), dane.get("wazne_do"), dane.get("opis"), now_iso))
    conn.commit()


# -- Live JSON (bez DB) ---------------------------------------------------------

def fetch_live_json(output_path="avalanche_data.json"):
    """Pobiera biezace komunikaty i zapisuje JSON z last_updated."""
    today   = date.today().isoformat()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    result  = {}

    for key, meta in ZRODLA.items():
        dane = pobierz_biuletyn(key, meta)
        result[key] = {
            "meta": {"nazwa": meta["nazwa"], "region": meta["region"]},
            "series": {},
            "last_updated": now_iso,
        }
        if dane and dane.get("stopien"):
            result[key]["series"][today] = dane
            result[key]["last_updated"]  = now_iso

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log.info(f"Live avalanche JSON zapisany: {output_path}")
    return result


# -- Kolekcja do DB + eksport ---------------------------------------------------

def collect_and_export(output_path="avalanche_data.json"):
    today = date.today().isoformat()
    conn  = get_db()

    for key, meta in ZRODLA.items():
        dane = pobierz_biuletyn(key, meta)
        if dane:
            upsert_biuletyn(conn, key, today, dane)

    # Eksport
    series  = defaultdict(dict)
    updated = {}
    for row in conn.execute(
        "SELECT source_key, captured_at, stopien, stopien_nazwa, tendencja, "
        "wazne_do, opis, last_updated FROM avalanche_bulletins ORDER BY captured_at"
    ):
        series[row["source_key"]][row["captured_at"]] = {
            "stopien":       row["stopien"],
            "stopien_nazwa": row["stopien_nazwa"],
            "tendencja":     row["tendencja"],
            "wazne_do":      row["wazne_do"],
            "opis":          row["opis"],
            "kolor":         KOLORY.get(row["stopien"]),
        }
        if row["last_updated"]:
            updated[row["source_key"]] = row["last_updated"]

    result = {}
    for key, meta in ZRODLA.items():
        result[key] = {
            "meta": {"nazwa": meta["nazwa"], "region": meta["region"]},
            "series": series.get(key, {}),
            "last_updated": updated.get(key),
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log.info(f"Avalanche JSON zapisany: {output_path}")
    conn.close()


def report():
    conn = get_db()
    rows = conn.execute(
        "SELECT source_key, captured_at, stopien, stopien_nazwa, last_updated "
        "FROM avalanche_bulletins ORDER BY source_key, captured_at"
    ).fetchall()
    if not rows:
        print("Brak danych w avalanche_bulletins.")
    else:
        print(f"\n{'='*80}\n  Dane lawinowe ({len(rows)} rekordow)\n{'='*80}")
        for r in rows:
            print(f"  {r['source_key']:<26} {r['captured_at']:<12} "
                  f"stopien={r['stopien']} ({r['stopien_nazwa'] or '?'})"
                  f"  updated: {r['last_updated'] or '?'}")
    conn.close()


def test_parsers():
    """Debug - pobierz i wyswietl surowe dane z parserów."""
    print("\n=== TEST PARSERÓW ===")
    for key, meta in ZRODLA.items():
        print(f"\n--- {key} ---")
        try:
            r = requests.get(meta["url"], headers=HEADERS, timeout=30)
            print(f"HTTP {r.status_code}, {len(r.text)} bajtow")
            print("Raw (pierwsze 500):", repr(r.text[:500]))
            dane = parse_topr(r.text) if meta["parser"] == "topr" else parse_hzs(r.text)
            print("Wynik:", dane)
        except Exception as e:
            print(f"BLAD: {e}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", action="store_true", help="Eksport DB -> JSON")
    p.add_argument("--report", action="store_true", help="Podglad DB")
    p.add_argument("--live",   action="store_true", help="Live JSON bez DB")
    p.add_argument("--test",   action="store_true", help="Debug parsera")
    args = p.parse_args()

    if args.report: report();         return
    if args.test:   test_parsers();   return
    if args.live:   fetch_live_json(); return

    collect_and_export()


if __name__ == "__main__":
    main()
import requests
import folium
import folium.plugins
import time
import math
import json
from shapely.geometry import Point, LineString, MultiLineString, mapping
from shapely.ops import unary_union, linemerge, polygonize

# ── Helpers ────────────────────────────────────────────────────────────────────

def uprość_geometrie(punkty, co_n=2):
    return punkty[::co_n]

def pobierz_dane(query, opis):
    serwery = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
        "https://overpass.openstreetmap.ru/api/interpreter",
    ]
    for serwer in serwery:
        try:
            print(f"Pobieram: {opis} ({serwer})...")
            response = requests.post(serwer, data=query, timeout=180)
            if response.status_code == 200 and response.text.strip():
                print(f"  OK!")
                return response.json()
            else:
                print(f"  Błąd HTTP {response.status_code}, próbuję kolejny serwer...")
        except requests.exceptions.SSLError as e:
            print(f"  SSL error: {e} — próbuję kolejny serwer...")
        except requests.exceptions.Timeout:
            print(f"  Timeout — próbuję kolejny serwer...")
        except requests.exceptions.ConnectionError as e:
            print(f"  Błąd połączenia: {e} — próbuję kolejny serwer...")
        except requests.exceptions.JSONDecodeError:
            print(f"  Błąd parsowania JSON — próbuję kolejny serwer...")
        except Exception as e:
            print(f"  Nieoczekiwany błąd: {e} — próbuję kolejny serwer...")
    print(f"  Wszystkie serwery zawiodły dla: {opis}")
    return {"elements": []}

def oblicz_dlugosc(punkty):
    dlugosc = 0
    for i in range(len(punkty) - 1):
        lat1, lon1 = punkty[i]
        lat2, lon2 = punkty[i+1]
        dlat = (lat2 - lat1) * 111
        dlon = (lon2 - lon1) * 111 * math.cos(math.radians((lat1 + lat2) / 2))
        dlugosc += math.sqrt(dlat**2 + dlon**2)
    return round(dlugosc, 2)

def zbuduj_poligon(data):
    """Zbiera linie ze WSZYSTKICH relacji w odpowiedzi i buduje jeden unary_union poligon."""
    wszystkie_linie = []
    for element in data["elements"]:
        if element["type"] == "relation" and "members" in element:
            for member in element["members"]:
                if member["type"] == "way" and "geometry" in member:
                    punkty = [(p["lon"], p["lat"]) for p in member["geometry"]]
                    if len(punkty) >= 2:
                        wszystkie_linie.append(LineString(punkty))

    if not wszystkie_linie:
        print("  Brak linii do zbudowania poligonu")
        return None

    print(f"  Linii zebranych: {len(wszystkie_linie)}")
    try:
        merged = linemerge(MultiLineString(wszystkie_linie))
        polys  = list(polygonize(merged))
        if not polys:
            print("  polygonize nie zwróciło poligonów — fallback na buforowane linie")
            return unary_union(wszystkie_linie).buffer(0.001)
        result = unary_union(polys).buffer(0)
        print(f"  Poligon OK, powierzchnia: {result.area:.4f}, typ: {result.geom_type}")
        return result
    except Exception as e:
        print(f"  Błąd polygonize: {e}")
        return None

# Połączony obszar obu parków (budowany po zbudowaniu poligonów)
obszar_parki = None  # inicjalizowany poniżej

def procent_w_parku(punkty_latlon):
    """
    Zwraca jaki procent długości waya leży wewnątrz obszaru parków.
    Punkty w formacie [(lat, lon), ...]
    Używa Shapely intersection() — precyzyjne cięcie linii poligonem.
    """
    if not punkty_latlon or len(punkty_latlon) < 2:
        return 0.0
    if obszar_parki is None:
        return 100.0  # brak danych o parkach — przepuść wszystko

    try:
        # Shapely używa (lon, lat)
        line = LineString([(lon, lat) for lat, lon in punkty_latlon])
        total_len = line.length
        if total_len == 0:
            return 0.0

        inside = obszar_parki.intersection(line)
        inside_len = inside.length
        return (inside_len / total_len) * 100.0
    except Exception:
        return 0.0

def kolor_szlaku(element):
    tags    = element.get('tags', {})
    highway = tags.get('highway', '')
    osmc    = tags.get('osmc:symbol', '')
    if osmc:
        kolor = osmc.split(':')[0].strip().lower()
        mapa_kolorow = {
            'red':    '#cc0000',
            'blue':   '#0000cc',
            'green':  '#006600',
            'yellow': '#ccaa00',
            'black':  '#222222',
        }
        if kolor in mapa_kolorow:
            return mapa_kolorow[kolor]
    return STYL.get(highway, {"color": "gray"})["color"]

def sanitize(s):
    if not isinstance(s, str):
        return str(s)
    return s.encode('utf-8', errors='replace').decode('utf-8')

def nazwa_koloru(element):
    tags = element.get('tags', {})
    osmc = tags.get('osmc:symbol', '')
    if osmc:
        kolor = osmc.split(':')[0].strip().lower()
        nazwy = {
            'red':    'Szlak czerwony',
            'blue':   'Szlak niebieski',
            'green':  'Szlak zielony',
            'yellow': 'Szlak żółty',
            'black':  'Szlak czarny',
        }
        if kolor in nazwy:
            return nazwy[kolor]
    highway = tags.get('highway', '')
    return NAZWY_TYPOW.get(highway, highway)

# ── Strava ─────────────────────────────────────────────────────────────────────

def wczytaj_strava(path="traffic_data.json"):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        segmenty = []
        for seg_id, val in data.items():
            meta = val.get("meta", {})
            lat  = meta.get("lat")
            lng  = meta.get("lng")
            if lat is None or lng is None:
                continue
            segmenty.append({
                "id":            int(seg_id),
                "name":          meta.get("name", "Segment"),
                "activity_type": meta.get("activity_type", ""),
                "lat":           lat,
                "lng":           lng,
                "effort_count":  meta.get("effort_count_cumulative", 0),
                "athlete_count": meta.get("athlete_count", 0),
                "distance":      meta.get("distance", 0),
                "avg_grade":     meta.get("avg_grade", 0),
                "last_snapshot": meta.get("last_snapshot", ""),
            })
        print(f"Wczytano {len(segmenty)} segmentów Strava z {path}")
        return segmenty
    except FileNotFoundError:
        print(f"Brak pliku {path} — warstwa natężenia wyłączona")
        return []
    except Exception as e:
        print(f"Błąd wczytywania {path}: {e}")
        return []

def wczytaj_pogode(path="weather_data.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Brak pliku {path} — dane pogodowe wyłączone")
        return {}
    except Exception as e:
        print(f"Blad wczytywania {path}: {e}")
        return {}

def wczytaj_lawiny(path="avalanche_data.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Brak pliku {path} — dane lawinowe wylaczone")
        return {}
    except Exception as e:
        print(f"Blad wczytywania {path}: {e}")
        return {}

def znajdz_najblizszy_segment_punkt(lat, lon, segmenty, promien_km=0.55):
    najblizszy  = None
    min_dystans = float('inf')
    for seg in segmenty:
        dlat = (seg["lat"] - lat) * 111
        dlon = (seg["lng"] - lon) * 111 * math.cos(math.radians(lat))
        d = math.sqrt(dlat**2 + dlon**2)
        if d < min_dystans:
            min_dystans = d
            najblizszy  = seg
    return najblizszy if min_dystans <= promien_km else None

def znajdz_segment_dla_way(punkty, segmenty):
    if not punkty or not segmenty:
        return None
    n = len(punkty)
    indeksy = set([0, n//4, n//2, 3*n//4, n-1])
    indeksy |= set(range(0, n, max(1, n//8)))
    trafienia = []
    for i in indeksy:
        lat, lon = punkty[i]
        seg = znajdz_najblizszy_segment_punkt(lat, lon, segmenty)
        if seg:
            trafienia.append(seg)
    if not trafienia:
        return None
    return max(trafienia, key=lambda s: s["effort_count"])

def effort_do_koloru(effort, max_effort):
    if max_effort <= 0 or effort <= 0:
        return None
    t = math.log(1 + effort) / math.log(1 + max_effort)
    t = min(1.0, max(0.0, t))
    if t < 0.25:
        tt = t / 0.25
        r = int(20  + tt * 40);  g = int(60  + tt * 80);  b = int(180 + tt * 40)
    elif t < 0.5:
        tt = (t - 0.25) / 0.25
        r  = int(60  + tt * 190); g = int(140 + tt * 100); b = int(220 - tt * 200)
    elif t < 0.75:
        tt = (t - 0.5) / 0.25
        r  = 250;                 g = int(240 - tt * 140); b = int(20  - tt * 20)
    else:
        tt = (t - 0.75) / 0.25
        r  = int(250 - tt * 20); g = int(100 - tt * 100); b = 0
    return f"#{r:02x}{g:02x}{b:02x}"

# ── Stałe ──────────────────────────────────────────────────────────────────────

BBOX = "(49.10, 19.60, 49.35, 20.25)"
STALA_GRUBOSC = 3
PROG_W_PARKU = 90.0  # % długości waya który musi leżeć w parku

STYL = {
    "path":        {"color": "#888888", "weight": 2, "grupa": "Szlaki górskie"},
    "via_ferrata": {"color": "darkred", "weight": 3, "grupa": "Via ferraty"},
    "footway":     {"color": "#888888", "weight": 2, "grupa": "Drogi piesze"},
    "pedestrian":  {"color": "#888888", "weight": 2, "grupa": "Drogi piesze"},
    "steps":       {"color": "#888888", "weight": 2, "grupa": "Drogi piesze"},
    "track":       {"color": "#888888", "weight": 2, "grupa": "Drogi leśne"},
}

NAZWY_TYPOW = {
    "path":        "Szlak górski",
    "via_ferrata": "Via ferrata",
    "footway":     "Droga piesza",
    "pedestrian":  "Droga piesza",
    "steps":       "Schody",
    "track":       "Droga leśna",
}

# ── Zapytania Overpass ─────────────────────────────────────────────────────────

query1 = f"""
[out:json][timeout:180];
(
  relation["route"="hiking"]["osmc:symbol"]{BBOX};
  relation["route"="hiking"]["network"~"lwn|rwn|nwn"]{BBOX};
  relation["route"="hiking"]["operator"~"PTTK|TPN|TANAP|KST|Správa"]{BBOX};
);
(._;>>;);
out geom;
"""

query2 = f"""
[out:json][timeout:120];
(
  relation["route"="hiking"]{BBOX};
);
(._;>>;);
out geom;
"""

query_tpn = """
[out:json][timeout:120];
(
  relation["name"="Tatrzański Park Narodowy"]["boundary"="national_park"];
  relation["name"~"Tatrzański Park Narodowy"]["boundary"="national_park"];
);
(._;>>;);
out geom;
"""

query_tanap = """
[out:json][timeout:60];
relation["name"="Tatranský národný park"]["boundary"="national_park"];
(._;>>;);
out geom;
"""

# ── Pobieranie danych ──────────────────────────────────────────────────────────

dane1      = pobierz_dane(query1,      "relacje hiking (oznakowane)")
time.sleep(5)
dane2      = pobierz_dane(query2,      "wszystkie relacje hiking")
time.sleep(5)
tpn_data   = pobierz_dane(query_tpn,   "granice TPN")
time.sleep(5)
tanap_data = pobierz_dane(query_tanap, "granice TANAP")

elementy_all = {}
for el in dane1["elements"] + dane2["elements"]:
    eid = (el['type'], el.get('id'))
    if eid not in elementy_all:
        elementy_all[eid] = el
wszystkie = list(elementy_all.values())

way_ids_w_relacjach = set()
for el in wszystkie:
    if el['type'] == 'relation' and 'members' in el:
        for m in el['members']:
            if m['type'] == 'way':
                way_ids_w_relacjach.add(m['ref'])

print(f"Łącznie: {len(wszystkie)} elementów | Wayów w relacjach: {len(way_ids_w_relacjach)}")

# ── Poligony parków ────────────────────────────────────────────────────────────

obszar_tpn   = zbuduj_poligon(tpn_data)
obszar_tanap = zbuduj_poligon(tanap_data)

# Mały bufor tylko dla tolerancji geometrycznej OSM (~100m)
buf_tpn   = obszar_tpn.buffer(0.001)   if obszar_tpn   else None
buf_tanap = obszar_tanap.buffer(0.001) if obszar_tanap else None

# Połączony obszar używany do filtrowania wayów (union TPN + TANAP)
if buf_tpn is not None and buf_tanap is not None:
    obszar_parki = unary_union([buf_tpn, buf_tanap])
elif buf_tpn is not None:
    obszar_parki = buf_tpn
elif buf_tanap is not None:
    obszar_parki = buf_tanap
else:
    obszar_parki = None

print(f"TPN: {'OK' if obszar_tpn else 'BŁĄD'}, TANAP: {'OK' if obszar_tanap else 'BŁĄD'}")
if obszar_parki:
    print(f"Połączony obszar parków: {obszar_parki.geom_type}, powierzchnia: {obszar_parki.area:.4f}")

# ── Strava ─────────────────────────────────────────────────────────────────────

strava_segmenty = wczytaj_strava("traffic_data.json")
max_effort      = max((s["effort_count"] for s in strava_segmenty), default=1)
strava_dostepna = len(strava_segmenty) > 0
print(f"Max effort_count: {max_effort}")
pogoda_dane     = wczytaj_pogode("weather_data.json")
lawiny_dane     = wczytaj_lawiny("avalanche_data.json")

# ── Geometria wayów ────────────────────────────────────────────────────────────

way_geometry = {}
for element in wszystkie:
    if element['type'] == 'way' and 'geometry' in element:
        wid = element['id']
        if wid not in way_geometry:
            way_geometry[wid] = [(p['lat'], p['lon']) for p in element['geometry']]

for element in dane2["elements"]:
    if element['type'] == 'relation' and 'members' in element:
        for member in element['members']:
            if member['type'] == 'way' and 'geometry' in member:
                wid = member['ref']
                if wid not in way_geometry:
                    way_geometry[wid] = [(p['lat'], p['lon']) for p in member['geometry']]

print(f"Zebrano geometrię dla {len(way_geometry)} wayów")

# ── Relacje ────────────────────────────────────────────────────────────────────

relacje_dla_way  = {}
relacja_do_wayow = {}

for element in dane2["elements"]:
    if element['type'] != 'relation' or 'members' not in element:
        continue
    nazwa_rel  = sanitize(element.get('tags', {}).get('name', 'Brak nazwy'))
    relacja_id = element['id']
    total   = 0
    way_ids = []
    for member in element['members']:
        if member['type'] == 'way':
            wid = member['ref']
            way_ids.append(wid)
            pts = way_geometry.get(wid, [])
            if pts:
                total += oblicz_dlugosc(pts)
    dlugosc_rel = round(total, 2)
    relacja_do_wayow[relacja_id] = way_ids
    for wid in way_ids:
        if wid not in relacje_dla_way:
            relacje_dla_way[wid] = (nazwa_rel, dlugosc_rel, relacja_id)

print(f"Relacji: {len(relacja_do_wayow)} | Wayów z relacją: {len(relacje_dla_way)}")

# ── Filtr: >= 90% długości waya musi leżeć w parku ────────────────────────────
print(f"Filtrowanie wayów (próg: {PROG_W_PARKU}% w parku)...")
ways_w_parku = set()
if obszar_parki is not None:
    for way_id, pts_raw in way_geometry.items():
        pct = procent_w_parku(pts_raw)
        if pct >= PROG_W_PARKU:
            ways_w_parku.add(way_id)
    print(f"Wayów spełniających próg {PROG_W_PARKU}%: {len(ways_w_parku)}")
else:
    ways_w_parku = set(way_geometry.keys())
    print("Brak poligonów parków — pokazuję wszystkie waye")

# ── Filtr topologiczny: usuń izolowane waye (min. 2 sąsiadów w sieci) ─────────
# Buduje graf połączeń: waye dzielące węzeł (punkt końcowy) są sąsiadami.
# Waye z 0 sąsiadów w ways_w_parku to odcięte "kikuty" — usuwamy je.
# Powtarzamy w pętli aż sieć się ustabilizuje (cascade removal).
if ways_w_parku:
    print("Filtr topologiczny — usuwanie izolowanych fragmentów...")

    # Indeks: punkt (zaokrąglony) → zbiór way_id które przez niego przechodzą
    punkt_do_wayow = {}
    for wid in ways_w_parku:
        pts = way_geometry.get(wid, [])
        if not pts:
            continue
        # Rejestruj tylko punkty końcowe (węzły sieci)
        for pt in [pts[0], pts[-1]]:
            key = (round(pt[0], 5), round(pt[1], 5))
            punkt_do_wayow.setdefault(key, set()).add(wid)

    def sasiedzi(wid):
        """Zwraca zbiór wayów sąsiadujących z wid przez węzły końcowe."""
        pts = way_geometry.get(wid, [])
        if not pts:
            return set()
        s = set()
        for pt in [pts[0], pts[-1]]:
            key = (round(pt[0], 5), round(pt[1], 5))
            s |= punkt_do_wayow.get(key, set())
        s.discard(wid)
        return s & ways_w_parku

    iteracja = 0
    while True:
        iteracja += 1
        do_usuniecia = set()
        for wid in ways_w_parku:
            if len(sasiedzi(wid)) == 0:
                do_usuniecia.add(wid)
        if not do_usuniecia:
            break
        # Usuń z indeksu punkt→way
        for wid in do_usuniecia:
            pts = way_geometry.get(wid, [])
            for pt in ([pts[0], pts[-1]] if pts else []):
                key = (round(pt[0], 5), round(pt[1], 5))
                punkt_do_wayow.get(key, set()).discard(wid)
        ways_w_parku -= do_usuniecia
        print(f"  Iteracja {iteracja}: usunięto {len(do_usuniecia)} izolowanych wayów, pozostało {len(ways_w_parku)}")

    print(f"Po filtrze topologicznym: {len(ways_w_parku)} wayów")

# ── Spatial join + propagacja (tylko waye w parku) ────────────────────────────

kolory_wayow   = {}
kolory_relacji = {}

if strava_dostepna:
    print("Przebieg 1: spatial join...")
    for way_id in ways_w_parku:
        pts_raw = way_geometry.get(way_id, [])
        if not pts_raw:
            continue
        pts_skr = uprość_geometrie(pts_raw)
        seg = znajdz_segment_dla_way(pts_skr, strava_segmenty)
        if not seg:
            continue
        kolory_wayow[way_id] = seg
        if way_id in relacje_dla_way:
            _, _, relacja_id = relacje_dla_way[way_id]
            prev = kolory_relacji.get(relacja_id)
            if prev is None or seg["effort_count"] > prev["effort_count"]:
                kolory_relacji[relacja_id] = seg
    print(f"Dopasowano: {len(kolory_wayow)} wayów | Relacji: {len(kolory_relacji)}")

    propagowane = 0
    for relacja_id, seg in kolory_relacji.items():
        for way_id in relacja_do_wayow.get(relacja_id, []):
            if way_id not in kolory_wayow and way_id in ways_w_parku:
                kolory_wayow[way_id] = seg
                propagowane += 1
    print(f"Propagacja: +{propagowane} | Łącznie: {len(kolory_wayow)}")

    print("Przebieg 1c: flood fill (tylko waye w parku)...")
    propagowane_ff = 0
    for relacja_id, way_ids in relacja_do_wayow.items():
        seg_rel = kolory_relacji.get(relacja_id)
        if not seg_rel:
            continue
        for way_id in way_ids:
            if way_id in kolory_wayow:
                continue
            if way_id not in ways_w_parku:
                continue
            kolory_wayow[way_id] = seg_rel
            propagowane_ff += 1
    print(f"Flood fill relacji: +{propagowane_ff} | Łącznie: {len(kolory_wayow)}")

# ── Mapa ───────────────────────────────────────────────────────────────────────

mapa = folium.Map(
    location=[49.23, 19.98],
    zoom_start=11,
    control_scale=True,
    tiles="CartoDB dark_matter"
)

folium.TileLayer(
    tiles="CartoDB positron",
    name="Jasny",
    attr="CartoDB",
).add_to(mapa)

grupy = {
    "Szlaki górskie": folium.FeatureGroup(name="Szlaki górskie", show=True),
    "Via ferraty":    folium.FeatureGroup(name="Via ferraty",    show=True),
    "Drogi piesze":   folium.FeatureGroup(name="Drogi piesze",   show=True),
    "Drogi leśne":    folium.FeatureGroup(name="Drogi leśne",    show=True),
    "Pozostałe":      folium.FeatureGroup(name="Pozostałe",      show=True),
}

# ── Rysowanie ──────────────────────────────────────────────────────────────────

print("Przebieg 2: rysowanie...")
popupy_relacji = {}
kolory_bazowe  = {}
odfiltrowane   = 0

for element in wszystkie:
    if element['type'] != 'way' or 'geometry' not in element:
        continue

    way_id = element.get('id')
    if way_id not in way_ids_w_relacjach:
        continue

    # FILTR: >= 90% waya w granicach TPN/TANAP
    if way_id not in ways_w_parku:
        odfiltrowane += 1
        continue

    highway   = element.get('tags', {}).get('highway', '')
    styl      = STYL.get(highway, {"color": "#888888", "weight": 2, "grupa": "Szlaki górskie"})
    pts_pelne = [(p['lat'], p['lon']) for p in element['geometry']]
    punkty    = uprość_geometrie(pts_pelne)

    kolor_oryginalny = kolor_szlaku(element)
    typ_nazwa        = nazwa_koloru(element)

    if way_id in relacje_dla_way:
        nazwa_rel, dlugosc_total, relacja_id = relacje_dla_way[way_id]
        nazwa        = sanitize(nazwa_rel)
        info_dlugosc = f"Długość całkowita: {dlugosc_total} km"
        klasa_css    = f"trasa-{relacja_id}"
    else:
        nazwa        = sanitize(element.get('tags', {}).get('name', 'Brak nazwy'))
        info_dlugosc = f"Długość odcinka: {oblicz_dlugosc(punkty)} km"
        klasa_css    = f"trasa-way-{way_id}"
        relacja_id   = None

    if klasa_css not in kolory_bazowe:
        kolory_bazowe[klasa_css] = kolor_oryginalny

    seg = kolory_wayow.get(way_id)
    if seg is None and relacja_id is not None:
        seg = kolory_relacji.get(relacja_id)

    kolor_finalny  = kolor_oryginalny
    weight_finalny = styl["weight"]

    if seg:
        kolor_heat = effort_do_koloru(seg["effort_count"], max_effort)
        if kolor_heat:
            kolor_finalny  = kolor_heat
            weight_finalny = STALA_GRUBOSC

    linia = folium.PolyLine(
        punkty,
        color=kolor_finalny,
        weight=weight_finalny,
        opacity=0.8,
        tooltip=sanitize(nazwa),
    )
    linia.options['className'] = klasa_css

    if klasa_css not in popupy_relacji:
        mid = punkty[len(punkty)//2]
        popupy_relacji[klasa_css] = {
            "nazwa":    nazwa,
            "typ":      typ_nazwa,
            "dlugosc":  info_dlugosc,
            "effort":   seg["effort_count"]   if seg else 0,
            "atleci":   seg["athlete_count"]  if seg else 0,
            "seg_name": sanitize(seg["name"]) if seg else "",
            "snapshot": seg["last_snapshot"]  if seg else "",
            "lat":      mid[0],
            "lon":      mid[1],
        }

    grupy[styl["grupa"]].add_child(linia)

print(f"Odfiltrowano: {odfiltrowane} | Narysowano mapę")

for grupa in grupy.values():
    grupa.add_to(mapa)

# ── Granice TPN (WMS) ──────────────────────────────────────────────────────────

grupy["Granice TPN"] = folium.FeatureGroup(name="Granice TPN", show=True)
folium.WmsTileLayer(
    url="https://sdi.gdos.gov.pl/wms",
    layers="ParkiNarodowe",
    fmt="image/png",
    transparent=True,
    name="Granice TPN (GDOŚ)",
    attr="GDOŚ",
    opacity=0.4
).add_to(grupy["Granice TPN"])
if obszar_tpn:
    folium.GeoJson(
        mapping(obszar_tpn),
        style_function=lambda x: {
            "color": "#2a7a2a", "weight": 3, "opacity": 1.0,
            "fillOpacity": 0,
        },
        interactive=False
    ).add_to(grupy["Granice TPN"])
grupy["Granice TPN"].add_to(mapa)

# ── Granice TANAP ──────────────────────────────────────────────────────────────

grupy["Granice TANAP"] = folium.FeatureGroup(name="Granice TANAP", show=True)
if obszar_tanap:
    folium.GeoJson(
        mapping(obszar_tanap),
        style_function=lambda x: {
            "color": "#1a6b1a", "weight": 3, "opacity": 1.0,
            "fillColor": "#2d8a2d", "fillOpacity": 0.08,
        },
        interactive=False
    ).add_to(grupy["Granice TANAP"])
else:
    for element in tanap_data["elements"]:
        if element["type"] == "relation" and "members" in element:
            for member in element["members"]:
                if member["type"] == "way" and "geometry" in member:
                    pts = [(p["lat"], p["lon"]) for p in member["geometry"]]
                    folium.PolyLine(pts, color="#1a6b1a", weight=3, opacity=1.0).add_to(grupy["Granice TANAP"])
grupy["Granice TANAP"].add_to(mapa)

# ── Waymarked Trails ───────────────────────────────────────────────────────────

folium.TileLayer(
    tiles="https://tile.waymarkedtrails.org/hiking/{z}/{x}/{y}.png",
    attr="Waymarked Trails",
    name="Szlaki oznakowane",
    opacity=0.6,
    overlay=True,
    show=False,
).add_to(mapa)

# ── Dane dla suwaka ────────────────────────────────────────────────────────────

relacja_serie = {}
if strava_dostepna:
    try:
        with open("traffic_data.json", encoding="utf-8") as f:
            traffic_raw = json.load(f)
    except Exception:
        traffic_raw = {}

    for relacja_id, seg in kolory_relacji.items():
        seg_id    = str(seg["id"])
        serie_raw = traffic_raw.get(seg_id, {}).get("series", {})
        daty      = sorted(serie_raw.keys())

        if not daty and seg.get("last_snapshot"):
            daty      = [seg["last_snapshot"]]
            serie_raw = {seg["last_snapshot"]: seg["effort_count"]}

        efforts_delta = []
        for i, d in enumerate(daty):
            cum = serie_raw.get(d, 0)
            if i == 0:
                efforts_delta.append(0)
            else:
                prev_cum = serie_raw.get(daty[i-1], 0)
                efforts_delta.append(max(0, cum - prev_cum))

        relacja_serie[str(relacja_id)] = {
            "dates":   daty,
            "efforts": efforts_delta,
            "max_eff": seg["effort_count"],
        }

wszystkie_daty_raw = sorted(set(d for v in relacja_serie.values() for d in v["dates"]))
wszystkie_daty = wszystkie_daty_raw[1:] if len(wszystkie_daty_raw) > 1 else wszystkie_daty_raw

# ── Legenda ────────────────────────────────────────────────────────────────────

if strava_dostepna:
    mapa.get_root().html.add_child(folium.Element("""
    <div style="position:fixed;bottom:75px;left:10px;z-index:1000;
        background:rgba(0,0,0,0.75);padding:10px 14px;border-radius:6px;
        color:white;font-size:12px;font-family:monospace;
        border:1px solid rgba(255,255,255,0.15);">
        <b>Natężenie ruchu</b><br>
        <div style="width:160px;height:10px;margin:6px 0 3px;
            background:linear-gradient(to right,#143cb4,#3c8cdc,#faf014,#fa6400,#e00000);
            border-radius:3px;"></div>
        <div style="display:flex;justify-content:space-between;width:160px;font-size:10px;color:#aaa">
            <span>Niskie</span><span>Średnie</span><span>Wysokie</span>
        </div>
        <div style="margin-top:6px;font-size:10px;color:#aaa">Szare = brak danych Strava</div>
    </div>
    """))

# ── Kontrolki + CSS suwaka ─────────────────────────────────────────────────────

folium.LayerControl(collapsed=False).add_to(mapa)
folium.plugins.MousePosition(
    position="bottomleft", separator=" | ", prefix="Dł./Szer.:", num_digits=5
).add_to(mapa)

mapa.get_root().html.add_child(folium.Element("""
<style>
    /* ── Suwak czasu ── */
    #tl-panel {
        position:fixed;bottom:0;left:0;right:0;z-index:1000;
        background:rgba(8,11,16,0.96);border-top:1px solid #1a2535;
        padding:7px 18px 9px;display:flex;align-items:center;gap:14px;
        font-family:monospace;
    }
    #tl-date { font-size:11px;color:#f0a030;min-width:68px;letter-spacing:.05em;flex-shrink:0; }
    #tl-wrap { flex:1;display:flex;flex-direction:column;gap:5px; }
    #tl-lbls { display:flex;justify-content:space-between;font-size:8px;color:#3a4a5a;letter-spacing:.03em; }
    #tl-lbls span.act { color:#e07020; }
    input[type=range]#tl-sl {
        -webkit-appearance:none;width:100%;height:3px;
        background:#1a2535;border-radius:2px;outline:none;cursor:pointer;
    }
    input[type=range]#tl-sl::-webkit-slider-thumb {
        -webkit-appearance:none;width:13px;height:13px;
        border-radius:50%;background:#e07020;border:2px solid #080b10;cursor:pointer;
    }
    input[type=range]#tl-sl::-moz-range-thumb {
        width:13px;height:13px;border-radius:50%;background:#e07020;border:2px solid #080b10;cursor:pointer;
    }
    #tl-play {
        background:#1a2535;border:none;color:#8aa0b8;
        width:26px;height:26px;border-radius:2px;cursor:pointer;font-size:13px;flex-shrink:0;
        display:flex;align-items:center;justify-content:center;
    }
    #tl-play.on { background:#e07020;color:white; }

    /* pomiar usunięty */

    /* ── Przycisk trybu ── */
    #theme-btn {
        position:fixed;bottom:55px;right:10px;z-index:1000;
        background:rgba(8,11,16,0.88);border:1px solid rgba(255,255,255,0.2);
        border-radius:6px;padding:6px 10px;cursor:pointer;font-size:12px;color:#ccc;
        font-family:monospace;transition:background .15s;
    }
    #theme-btn:hover { background:rgba(255,255,255,0.1); }
    #theme-btn.light { background:rgba(255,255,255,0.9);border-color:rgba(0,0,0,0.2);color:#333; }

    /* ── LayerControl niżej, nie koliduje z lawiny ── */
    .leaflet-top.leaflet-right { display: none !important; }

    /* ── Tooltip szlaków ── */
    .leaflet-tooltip {
        background:rgba(8,11,16,0.88);border:1px solid rgba(255,255,255,0.15);
        color:#ddd;font-family:monospace;font-size:11px;padding:3px 7px;border-radius:4px;
    }
</style>
<button id="theme-btn" title="Przełącz tryb jasny/ciemny">&#9790; Ciemny</button>
"""))

# ── Wstrzyknij dane inline jako window.TD ─────────────────────────────────────

td = {
    "popupy":       popupy_relacji,
    "relSerie":     relacja_serie,
    "allDates":     wszystkie_daty,
    "koloryBazowe": kolory_bazowe,
    "maxEffort":    max_effort,
    "weatherData":  pogoda_dane,
    "avalancheData": lawiny_dane,
}
td_json = json.dumps(td, ensure_ascii=False)
print(f"window.TD rozmiar: {len(td_json)//1024} KB")
mapa.get_root().html.add_child(folium.Element(
    "<script>window.TD=" + td_json + ";</script>"
))

# ── Cały JS ────────────────────────────────────────────────────────────────────

JS = """
document.addEventListener("DOMContentLoaded", function() {
    var TD  = window.TD || {};
    var popupy        = TD.popupy        || {};
    var relSerie      = TD.relSerie      || {};
    var allDates      = TD.allDates      || [];
    var koloryBazowe  = TD.koloryBazowe  || {};
    var maxEffort     = TD.maxEffort     || 1;
    var weatherData   = TD.weatherData   || {};
    var avalancheData = TD.avalancheData || {};

    var aktywnaKlasa = null;

    var mapaL        = null;
    var playTimer    = null;
    var currentIdx   = 0;

    // \u2500\u2500 Helpers pogodowych \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    function windDir(deg) {
        if (deg === null || deg === undefined) return '\u2013';
        var dirs = ['N','NE','E','SE','S','SW','W','NW'];
        return dirs[Math.round(deg / 45) % 8];
    }

    function distKm(lat1, lon1, lat2, lon2) {
        var R = 6371;
        var dLat = (lat2-lat1)*Math.PI/180, dLon = (lon2-lon1)*Math.PI/180;
        var a = Math.sin(dLat/2)*Math.sin(dLat/2) +
                Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*
                Math.sin(dLon/2)*Math.sin(dLon/2);
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }

    function najblizszaStacja(lat, lon) {
        var bestKey = null, bestDist = Infinity;
        Object.keys(weatherData).forEach(function(key) {
            var m = (weatherData[key].meta || {});
            if (m.lat == null) return;
            var d = distKm(lat, lon, m.lat, m.lon);
            if (d < bestDist) { bestDist = d; bestKey = key; }
        });
        return bestKey;
    }

    function getDaneStacji(key, idx) {
        var serie = (weatherData[key] || {}).series || {};
        var daty = Object.keys(serie).sort();
        if (!daty.length) return null;
        // Zawsze pokazuj najnowsze dostępne dane - pogoda nie zależy od suwaka Strava
        return serie[daty[daty.length - 1]];
    }

    // Renderuje sekcj\u0119 pogodow\u0105 w popupie szlaku
    function fmtTs(ts) {
        // Formatuje ISO timestamp "2026-03-05T14:00" na "05.03 14:00"
        if (!ts) return null;
        try {
            var m = ts.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
            if (m) return m[3]+'.'+m[2]+' '+m[4]+':'+m[5];
        } catch(e) {}
        return ts;
    }

    function renderPogodaPopup(key, idx) {
        var stObj = weatherData[key] || {};
        var meta  = stObj.meta || {};
        var d     = getDaneStacji(key, idx);
        var nazwa = meta.nazwa || key;
        var alt   = meta.alt ? ' (' + meta.alt + '\u00a0m n.p.m.)' : '';
        var kraj  = meta.kraj ? ' \u2022 ' + meta.kraj : '';
        var upd   = fmtTs(stObj.last_updated);

        if (!d) {
            return '<div style="color:#556;font-size:11px;margin-top:4px">Brak danych: ' + nazwa + '</div>';
        }

        var rows = [];
        if (d.temperatura     != null) rows.push(['Temperatura',  d.temperatura + '\u00b0C']);
        if (d.predkosc_wiatru != null) rows.push(['Wiatr',        d.predkosc_wiatru + ' m/s ' + windDir(d.kierunek_wiatru)]);
        if (d.suma_opadu      != null) rows.push(['Opady',        d.suma_opadu + ' mm']);
        if (d.wilgotnosc      != null) rows.push(['Wilgotno\u015b\u0107', d.wilgotnosc + '%']);
        if (d.cisnienie       != null) rows.push(['Ci\u015bnienie', d.cisnienie + ' hPa']);

        var table = rows.map(function(r) {
            return '<tr><td style="color:#8ab4f8;padding-right:8px;white-space:nowrap">' + r[0] + '</td>' +
                   '<td style="color:#eee;font-weight:bold">' + r[1] + '</td></tr>';
        }).join('');

        return '<div style="margin-top:4px">' +
               '<div style="font-size:10px;color:#8ab4f8;margin-bottom:2px">' +
               nazwa + alt + kraj + '</div>' +
               '<table style="font-size:12px;border-collapse:collapse;width:100%">' + table + '</table>' +
               (upd ? '<div style="font-size:9px;color:#445;margin-top:2px">Pomiar: ' + upd + '</div>' : '') +
               '</div>';
    }

    // \u2500\u2500 Helpers lawinowych \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    function getDaneLawiny(key, idx) {
        var serie = (avalancheData[key] || {}).series || {};
        var daty = Object.keys(serie).sort();
        if (!daty.length) return null;
        // Zawsze pokazuj najnowsze dostępne dane - lawiny nie zależą od suwaka Strava
        var lastValid = null;
        for (var i = 0; i < daty.length; i++) {
            if (serie[daty[i]] && serie[daty[i]].stopien) lastValid = serie[daty[i]];
        }
        return lastValid;
    }

    function renderLawinaPopup(idx) {
        var keys = Object.keys(avalancheData);
        if (!keys.length) return '';

        var aktywne = keys.some(function(k) {
            var d = getDaneLawiny(k, idx);
            return d && d.stopien;
        });
        if (!aktywne) return '';

        var html = '<hr style="margin:6px 0;border-color:#2a3040">' +
                   '<div style="font-size:11px;font-weight:bold;color:#f0a030;margin-bottom:4px">' +
                   'Zagro\u017cenie lawinowe</div>';

        keys.forEach(function(key) {
            var avObj = avalancheData[key] || {};
            var meta  = avObj.meta || {};
            var d     = getDaneLawiny(key, idx);
            var upd   = fmtTs(avObj.last_updated);
            if (!d || !d.stopien) return;
            var kolor = d.kolor || '#888';
            html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">' +
                    '<span style="display:inline-block;background:' + kolor +
                    ';color:#000;font-weight:bold;font-size:13px;min-width:20px;text-align:center;' +
                    'padding:1px 5px;border-radius:3px">' + d.stopien + '</span>' +
                    '<div>' +
                    '<div style="font-size:11px;color:#ccc">' + (d.stopien_nazwa || '') +
                    ' <span style="color:#556;font-size:10px">\u2014 ' + meta.nazwa + '</span></div>' +
                    (upd ? '<div style="font-size:9px;color:#445">Odswiezono: ' + upd + '</div>' : '') +
                    '</div></div>';
        });

        return html;
    }

    // \u2500\u2500 Info panel (szlaki) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var panel = document.createElement('div');
    panel.id  = 'info-panel';
    panel.style.cssText = [
        'position:fixed;top:10px;left:10px',
        'background:rgba(8,11,16,0.94);color:white',
        'padding:12px 16px;border-radius:8px',
        'box-shadow:0 4px 20px rgba(0,0,0,0.6)',
        'z-index:1000;max-width:280px;font-size:13px;display:none',
        'border:1px solid rgba(255,255,255,0.1);font-family:monospace',
        'max-height:calc(100vh - 120px);overflow-y:auto'
    ].join(';');
    document.body.appendChild(panel);

    function budujPanel(kl, idx) {
        if (!popupy[kl]) return '';
        var p   = popupy[kl];
        var rid = kl.replace('trasa-', '');
        var s   = relSerie[rid];

        // Nag\u0142\u00F3wek
        var html = '<div style="font-weight:bold;font-size:14px;margin-bottom:4px;color:#fff">' + p.nazwa + '</div>' +
                   '<div style="font-size:11px;color:#8ab4f8">' + p.typ + '</div>' +
                   '<div style="font-size:11px;color:#aaa;margin-bottom:6px">' + p.dlugosc + '</div>';

        // Nat\u0119\u017Cenie ruchu
        if (p.effort > 0) {
            html += '<hr style="margin:6px 0;border-color:#2a3040">' +
                    '<div style="font-size:11px;font-weight:bold;color:#f0a030;margin-bottom:4px">Nat\u0119\u017Cenie ruchu (Strava)</div>';

            if (idx === 0) {
                html += '<table style="font-size:11px;border-collapse:collapse;width:100%">' +
                        '<tr><td style="color:#8ab4f8;padding-right:8px">Przej\u015B\u0107 \u0142\u0105cznie</td>' +
                        '<td style="color:#eee;font-weight:bold">' + p.effort.toLocaleString() + '</td></tr>' +
                        '<tr><td style="color:#8ab4f8;padding-right:8px">Atlet\u00F3w</td>' +
                        '<td style="color:#eee">' + p.atleci.toLocaleString() + '</td></tr>' +
                        '<tr><td style="color:#8ab4f8;padding-right:8px">Segment</td>' +
                        '<td style="color:#aaa;font-size:10px">' + p.seg_name + '</td></tr>' +
                        '<tr><td style="color:#8ab4f8;padding-right:8px">Snapshot</td>' +
                        '<td style="color:#aaa;font-size:10px">' + p.snapshot + '</td></tr>' +
                        '</table>';
            } else {
                var dt = allDates[idx - 1];
                var dzienne = 0;
                if (s) { var di = s.dates.indexOf(dt); dzienne = di >= 0 ? (s.efforts[di] || 0) : 0; }
                var dp = dt.split('-');
                html += '<table style="font-size:11px;border-collapse:collapse;width:100%">' +
                        '<tr><td style="color:#8ab4f8;padding-right:8px">Dzie\u0144</td>' +
                        '<td style="color:#eee;font-weight:bold">' + dp[2] + '.' + dp[1] + '.' + dp[0] + '</td></tr>' +
                        '<tr><td style="color:#8ab4f8;padding-right:8px">Przej\u015B\u0107 tego dnia</td>' +
                        '<td style="color:#eee;font-weight:bold">' + dzienne.toLocaleString() + '</td></tr>' +
                        '</table>';
            }
        }

        // Pogoda z najbli\u017Cszej stacji
        if (Object.keys(weatherData).length > 0 && p.lat != null) {
            var stKey = najblizszaStacja(p.lat, p.lon);
            if (stKey) {
                html += '<hr style="margin:6px 0;border-color:#2a3040">' +
                        '<div style="font-size:11px;font-weight:bold;color:#f0a030;margin-bottom:2px">Pogoda</div>' +
                        renderPogodaPopup(stKey, idx);
            }
        }

        // Zagro\u017Cenie lawinowe
        var lawinaHtml = renderLawinaPopup(idx);
        if (lawinaHtml) html += lawinaHtml;

        html += '<div id="panel-close" style="margin-top:8px;font-size:10px;color:#556;cursor:pointer">' +
                'Zamknij \u2715</div>';
        return html;
    }

    // \u2500\u2500 Przycisk lawinowy (prawy g\u00F3rny r\u00F3g) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var avalancheBtn = document.createElement('button');
    avalancheBtn.id  = 'avalanche-btn';
    avalancheBtn.style.cssText = [
        'position:fixed;top:50px;right:10px',
        'background:rgba(8,11,16,0.92);color:white',
        'padding:6px 12px;border-radius:8px',
        'border:1px solid rgba(255,255,255,0.15)',
        'box-shadow:0 2px 12px rgba(0,0,0,0.5)',
        'z-index:1001;font-size:11px;font-family:monospace;cursor:pointer',
        'display:none;white-space:nowrap'
    ].join(';');
    document.body.appendChild(avalancheBtn);

    var avalanchePanel = document.createElement('div');
    avalanchePanel.id  = 'avalanche-panel';
    avalanchePanel.style.cssText = [
        'position:fixed;top:86px;right:10px',
        'background:rgba(8,11,16,0.96);color:white',
        'padding:12px 16px;border-radius:8px',
        'box-shadow:0 4px 20px rgba(0,0,0,0.6)',
        'z-index:1001;font-size:12px;font-family:monospace',
        'border:1px solid rgba(255,255,255,0.1)',
        'max-width:300px;display:none'
    ].join(';');
    document.body.appendChild(avalanchePanel);

    avalancheBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        avalanchePanel.style.display = avalanchePanel.style.display !== 'none' ? 'none' : 'block';
    });

    document.addEventListener('click', function(e) {
        if (!avalancheBtn.contains(e.target) && !avalanchePanel.contains(e.target))
            avalanchePanel.style.display = 'none';
    });

    function updateAvalancheBtn(idx) {
        var keys = Object.keys(avalancheData);
        if (!keys.length) { avalancheBtn.style.display = 'none'; return; }

        var maxStopien = 0, maxKolor = null;
        keys.forEach(function(k) {
            var d = getDaneLawiny(k, idx);
            if (d && d.stopien && d.stopien > maxStopien) {
                maxStopien = d.stopien; maxKolor = d.kolor;
            }
        });

        if (!maxStopien) { avalancheBtn.style.display = 'none'; return; }

        avalancheBtn.style.display = 'block';
        avalancheBtn.style.borderColor = maxKolor || '#888';
        avalancheBtn.innerHTML =
            'Stopie\u0144 zagro\u017Cenia lawinowego: ' +
            '<span style="display:inline-block;background:' + (maxKolor||'#888') +
            ';color:#000;font-weight:bold;padding:0 6px;border-radius:3px;margin-left:2px">' +
            maxStopien + '</span>';

        // Tre\u015B\u0107 panelu
        var html = '<div style="font-weight:bold;font-size:13px;margin-bottom:10px;color:#f0a030">' +
                   'Zagro\u017Cenie lawinowe</div>';

        keys.forEach(function(key) {
            var meta = (avalancheData[key] || {}).meta || {};
            var d    = getDaneLawiny(key, idx);
            var kolor = (d && d.kolor) || '#555';
            var stopien = (d && d.stopien) || '\u2013';

            var avUpd = fmtTs((avalancheData[key]||{}).last_updated);
            html += '<div style="margin-bottom:10px;padding:8px;background:rgba(255,255,255,0.04);border-radius:6px;border-left:3px solid ' + kolor + '">';
            html += '<div style="display:flex;justify-content:space-between;align-items:baseline">';
            html += '<span style="font-size:10px;color:#8ab4f8">' + meta.nazwa + '</span>';
            html += avUpd ? '<span style="font-size:9px;color:#445">Odsw.: ' + avUpd + '</span>' : '';
            html += '</div>';
            if (d && d.stopien) {
                html += '<div style="display:flex;align-items:center;gap:8px">';
                html += '<span style="background:' + kolor + ';color:#000;font-weight:bold;font-size:20px;width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:4px">' + d.stopien + '</span>';
                html += '<div><div style="font-size:13px;font-weight:bold">' + (d.stopien_nazwa||'') + '</div>';
                html += d.tendencja ? '<div style="font-size:10px;color:#889">' + d.tendencja + '</div>' : '';
                html += d.wazne_do  ? '<div style="font-size:10px;color:#556">Wa\u017Cne do: ' + d.wazne_do + '</div>' : '';
                html += '</div></div>';
                if (d.opis) html += '<div style="margin-top:5px;font-size:10px;color:#9ab;line-height:1.4">' + d.opis + '</div>';
            } else {
                html += '<div style="color:#445;font-size:11px">Brak danych</div>';
            }
            html += '</div>';
        });

        html += '<div style="font-size:10px;color:#445;margin-top:4px">' +
                'Skala EAWS 1\u20135 &nbsp;\u00B7&nbsp; ' +
                '<a href="https://lawiny.topr.pl" target="_blank" style="color:#567">lawiny.topr.pl</a>' +
                ' &nbsp;\u00B7&nbsp; <a href="https://hzs.sk" target="_blank" style="color:#567">hzs.sk</a></div>';

        avalanchePanel.innerHTML = html;
    }

    // \u2500\u2500 Suwak czasu \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    var tlPanel = document.createElement('div');
    tlPanel.id  = 'tl-panel';
    var lbls = allDates.map(function(d, i) {
        var parts = d.slice(5).split('-');
        return '<span id="tll' + (i+1) + '">' + parts[1] + '.' + parts[0] + '</span>';
    }).join('');
    var noData = allDates.length === 0;
    tlPanel.innerHTML =
        '<span id="tl-date">' + (noData ? 'BRAK DANYCH' : 'OG\u00D3\u0141EM') + '</span>' +
        '<div id="tl-wrap"><div id="tl-lbls">' +
        (noData ? '<span style="color:#3a4a5a;font-size:9px">Brak traffic_data.json</span>'
                : '<span id="tll0" class="act">Og\u00F3\u0142em</span>' + lbls) +
        '</div><input type="range" id="tl-sl" min="0" max="' + allDates.length + '" value="0" step="1"' +
        (noData ? ' disabled style="opacity:0.3"' : '') + '></div>' +
        '<button id="tl-play"' + (noData ? ' disabled style="opacity:0.3"' : '') + '>&#9654;</button>';
    document.body.appendChild(tlPanel);

    function eff2col(eff, mx) {
        if (!eff || !mx) return null;
        var t = Math.log(1 + eff) / Math.log(1 + mx);
        t = Math.min(1, Math.max(0, t));
        var r, g, b, tt;
        if (t < 0.25) { tt=t/0.25; r=Math.round(20+tt*40); g=Math.round(60+tt*80); b=Math.round(180+tt*40); }
        else if (t < 0.5) { tt=(t-0.25)/0.25; r=Math.round(60+tt*190); g=Math.round(140+tt*100); b=Math.round(220-tt*200); }
        else if (t < 0.75) { tt=(t-0.5)/0.25; r=250; g=Math.round(240-tt*140); b=Math.round(20-tt*20); }
        else { tt=(t-0.75)/0.25; r=Math.round(250-tt*20); g=Math.round(100-tt*100); b=0; }
        return 'rgb('+r+','+g+','+b+')';
    }

    function recolor(idx) {
        var date = idx === 0 ? null : allDates[idx - 1];
        var dayMax = 0;
        if (date) Object.values(relSerie).forEach(function(s) {
            var i = s.dates.indexOf(date);
            if (i >= 0 && s.efforts[i] > dayMax) dayMax = s.efforts[i];
        });
        document.querySelectorAll('path[class]').forEach(function(el) {
            var kl = Array.from(el.classList).find(function(c) { return c.startsWith('trasa-'); });
            if (!kl) return;
            var rid = kl.replace('trasa-', ''), s = relSerie[rid], baz = koloryBazowe[kl] || '#888888';
            if (!s) { el.style.stroke = baz; return; }
            if (!date) { el.style.stroke = eff2col(s.max_eff, maxEffort) || baz; }
            else { var i = s.dates.indexOf(date); var e = i >= 0 ? s.efforts[i] : 0; el.style.stroke = e > 0 ? eff2col(e, dayMax||1) : baz; }
        });
    }

    function setIdx(idx) {
        currentIdx = idx;
        document.getElementById('tl-sl').value = idx;
        var date = idx === 0 ? null : allDates[idx - 1];
        document.getElementById('tl-date').textContent =
            date ? date.slice(5).split('-').reverse().join('.') : 'OG\u00D3\u0141EM';
        document.querySelectorAll('#tl-lbls span').forEach(function(el, i) {
            el.className = i === idx ? 'act' : '';
        });
        recolor(idx);
        if (aktywnaKlasa && panel.style.display !== 'none')
            panel.innerHTML = budujPanel(aktywnaKlasa, idx);
        updateAvalancheBtn(idx);
    }

    setTimeout(function() {
        document.getElementById('tl-sl').addEventListener('input', function() { setIdx(parseInt(this.value)); });
        document.getElementById('tl-play').addEventListener('click', function() {
            if (playTimer) {
                clearInterval(playTimer); playTimer = null;
                this.innerHTML = '&#9654;'; this.className = '';
            } else {
                var btn = this; btn.innerHTML = '&#9646;&#9646;'; btn.className = 'on';
                var idx = currentIdx >= allDates.length ? 0 : currentIdx;
                playTimer = setInterval(function() {
                    idx++; setIdx(idx);
                    if (idx >= allDates.length) {
                        clearInterval(playTimer); playTimer = null;
                        btn.innerHTML = '&#9654;'; btn.className = '';
                    }
                }, 1500);
            }
        });
        (function applyWhenReady() {
            if (!document.querySelectorAll('path[class]').length) { setTimeout(applyWhenReady, 300); return; }
            // Ustaw suwak na dzisiejsz\u0105 dat\u0119 (lub ostatni\u0105 dost\u0119pn\u0105)
            var today = new Date().toISOString().slice(0, 10);
            var todayIdx = allDates.indexOf(today);
            if (todayIdx >= 0) {
                setIdx(todayIdx + 1); // +1 bo idx=0 to "Og\u00F3\u0142em"
            } else if (allDates.length > 0) {
                setIdx(allDates.length); // ostatnia dost\u0119pna data
            } else {
                setIdx(0);
            }
        })();
    }, 1200);

    function podswietl(kl, on) {
        document.querySelectorAll('path.' + kl).forEach(function(el) {
            el.style.opacity     = on ? '1.0' : '0.8';
            el.style.strokeWidth = on ? '6px' : '';
        });
    }

    setTimeout(function() {
        document.querySelectorAll('path[class]').forEach(function(el) {
            var kl = Array.from(el.classList).find(function(c) { return c.startsWith('trasa-'); });
            if (!kl) return;
            el.style.cursor = 'pointer';
            el.addEventListener('mouseenter', function() { if (!aktywnaKlasa || aktywnaKlasa !== kl) podswietl(kl, true); });
            el.addEventListener('mouseleave', function() { if (aktywnaKlasa !== kl) podswietl(kl, false); });
            el.addEventListener('click', function(e) {
                e.stopPropagation();
                if (aktywnaKlasa && aktywnaKlasa !== kl) podswietl(aktywnaKlasa, false);
                aktywnaKlasa = kl; podswietl(kl, true);
                panel.innerHTML = budujPanel(kl, currentIdx);
                panel.style.display = 'block';
                var closeBtn = document.getElementById('panel-close');
                if (closeBtn) closeBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    podswietl(aktywnaKlasa, false); aktywnaKlasa = null; panel.style.display = 'none';
                });
            });
        });
        document.querySelector('.leaflet-container').addEventListener('click', function() {
            if (aktywnaKlasa) { podswietl(aktywnaKlasa, false); aktywnaKlasa = null; panel.style.display = 'none'; }
        });
    }, 1500);

    setTimeout(function() {
        mapaL = Object.values(window).find(function(v) { return v && v._leaflet_id && v.getCenter; });
        if (!mapaL) return;
        var isDark = true;
        var darkUrl  = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
        var lightUrl = 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png';
        // -- Zmien motyw --
        document.getElementById('theme-btn').addEventListener('click', function() {
            isDark = !isDark;
            mapaL.eachLayer(function(l) { if (l._url) mapaL.removeLayer(l); });
            L.tileLayer(isDark ? darkUrl : lightUrl,
                {attribution: 'CartoDB', subdomains: 'abcd', maxZoom: 19}).addTo(mapaL);
            this.innerHTML = isDark ? '\u263E Zmie\u0144 motyw' : '\u2600\uFE0F Zmie\u0144 motyw';
            this.className = isDark ? '' : 'light';
        });

        // -- Kontrola warstw przez natywne checkboxy Leaflet LayerControl --
        // Leaflet LayerControl renderuje checkboxy w .leaflet-control-layers
        // Mamy go ukrytego przez CSS - ale DOM istnieje i checkboxy dzialaja
        // Klikamy je programowo przez label[title] lub input

        function getLeafletCheckbox(nazwa) {
            // Leaflet renderuje <label><input type=checkbox/span>NazwaGrupy</label>
            var labels = document.querySelectorAll('.leaflet-control-layers-overlays label');
            for (var i = 0; i < labels.length; i++) {
                var span = labels[i].querySelector('span');
                if (span && span.textContent.trim() === nazwa) {
                    return labels[i].querySelector('input');
                }
            }
            return null;
        }

        function toggleWarstwa(nazwa, on) {
            var cb = getLeafletCheckbox(nazwa);
            if (!cb) { return; }
            if (cb.checked !== on) { cb.click(); }
        }

        function isWarstwaDomyslnieWlaczona(nazwa) {
            var cb = getLeafletCheckbox(nazwa);
            return cb ? cb.checked : true;
        }

        // -- Przycisk "Szlaki oznakowane" (lewy gorny) --
        var oznBtn = document.createElement('button');
        oznBtn.id = 'ozn-btn';
        var oznActive = false;
        // Sprawdz czy szlaki oznakowane sa juz wlaczone domyslnie
        // (moga byc wylaczone - Folium show=False)
        function updateOznBtn() {
            oznBtn.innerHTML = oznActive
                ? '\u2691 Ukryj szlaki oznakowane'
                : '\u2690 Poka\u017C szlaki oznakowane';
            oznBtn.style.background   = oznActive ? 'rgba(224,112,32,0.25)' : 'rgba(8,11,16,0.92)';
            oznBtn.style.borderColor  = oznActive ? '#e07020' : 'rgba(255,255,255,0.15)';
            oznBtn.style.color        = oznActive ? '#e07020' : '#aaa';
        }
        oznBtn.style.cssText = [
            'position:fixed;top:10px;left:10px',
            'padding:6px 12px;border-radius:8px',
            'border:1px solid rgba(255,255,255,0.15)',
            'z-index:1001;font-size:11px;font-family:monospace;cursor:pointer',
            'white-space:nowrap;transition:all .15s'
        ].join(';');
        updateOznBtn();
        document.body.appendChild(oznBtn);

        // Wylacz szlaki oznakowane domyslnie
        toggleWarstwa('Szlaki oznakowane', false);

        oznBtn.addEventListener('click', function() {
            oznActive = !oznActive;
            toggleWarstwa('Szlaki oznakowane', oznActive);
            updateOznBtn();
        });

        // -- Przycisk "Typ szlaku / widok" (obok lawiny) --
        var layerBtn = document.createElement('button');
        layerBtn.id = 'layer-btn';
        layerBtn.innerHTML = '\u2630 Typ szlaku / widok';
        layerBtn.style.cssText = [
            'position:fixed;top:10px;left:230px',
            'background:rgba(8,11,16,0.92);color:white',
            'padding:6px 12px;border-radius:8px',
            'border:1px solid rgba(255,255,255,0.2)',
            'z-index:1001;font-size:11px;font-family:monospace;cursor:pointer',
            'white-space:nowrap;transition:background .15s'
        ].join(';');
        document.body.appendChild(layerBtn);

        // -- Panel warstw --
        var layerPanel = document.createElement('div');
        layerPanel.id = 'layer-panel';
        layerPanel.style.cssText = [
            'position:fixed;top:46px;left:230px',
            'background:rgba(8,11,16,0.96);color:white',
            'padding:12px 16px;border-radius:8px',
            'box-shadow:0 4px 20px rgba(0,0,0,0.6)',
            'z-index:1001;font-size:12px;font-family:monospace',
            'border:1px solid rgba(255,255,255,0.1)',
            'min-width:200px;display:none'
        ].join(';');
        document.body.appendChild(layerPanel);

        var WARSTWY_SZLAKI  = ['Szlaki g\u00F3rskie','Via ferraty','Drogi piesze','Drogi le\u015bne','Pozosta\u0142e'];
        var WARSTWY_GRANICE = ['Granice TPN','Granice TANAP'];

        function buildLayerPanel() {
            var html = '<div style="font-size:10px;color:#8ab4f8;margin-bottom:6px;letter-spacing:.05em">TYPY SZLAK\u00D3W</div>';

            WARSTWY_SZLAKI.forEach(function(nazwa) {
                var cb = getLeafletCheckbox(nazwa);
                var checked = cb ? cb.checked : true;
                var cbId = 'ui-lyr-' + nazwa.replace(/[^a-zA-Z0-9]/g, '_');
                html += '<label style="display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer">'
                     +  '<input type="checkbox" id="' + cbId + '"' + (checked ? ' checked' : '')
                     +  ' style="accent-color:#e07020;width:14px;height:14px;cursor:pointer">'
                     +  '<span style="color:#ddd;font-size:11px">' + nazwa + '</span>'
                     +  '</label>';
            });

            html += '<div style="font-size:10px;color:#7fba7f;margin:8px 0 5px;letter-spacing:.05em;border-top:1px solid rgba(255,255,255,0.08);padding-top:8px">GRANICE</div>';

            WARSTWY_GRANICE.forEach(function(nazwa) {
                var cb = getLeafletCheckbox(nazwa);
                var checked = cb ? cb.checked : true;
                var cbId = 'ui-lyr-' + nazwa.replace(/[^a-zA-Z0-9]/g, '_');
                html += '<label style="display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer">'
                     +  '<input type="checkbox" id="' + cbId + '"' + (checked ? ' checked' : '')
                     +  ' style="accent-color:#7fba7f;width:14px;height:14px;cursor:pointer">'
                     +  '<span style="color:#7fba7f;font-size:11px">' + nazwa + '</span>'
                     +  '</label>';
            });

            layerPanel.innerHTML = html;

            // Podepnij eventy - klikniecie naszego checkboxa = klikniecie Leaflet checkboxa
            WARSTWY_SZLAKI.concat(WARSTWY_GRANICE).forEach(function(nazwa) {
                var uiCb = document.getElementById('ui-lyr-' + nazwa.replace(/[^a-zA-Z0-9]/g, '_'));
                if (!uiCb) return;
                uiCb.addEventListener('change', function() {
                    toggleWarstwa(nazwa, this.checked);
                });
            });
        }

        layerBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var visible = layerPanel.style.display !== 'none';
            if (!visible) { buildLayerPanel(); layerPanel.style.display = 'block'; }
            else layerPanel.style.display = 'none';
        });

        document.addEventListener('click', function(e) {
            if (!layerBtn.contains(e.target) && !layerPanel.contains(e.target))
                layerPanel.style.display = 'none';
        });

    }, 2000);
});
"""
mapa.get_root().script.add_child(folium.Element(JS))

try:
    mapa.save("index.html")
except UnicodeEncodeError:
    html_out = mapa.get_root().render().encode('utf-8', errors='replace').decode('utf-8')
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print("Zapisano index.html (z czyszczeniem surogatów)")
else:
    with open("index.html", encoding="utf-8", errors="replace") as f:
        html_out = f.read()
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print("Gotowe! Zapisano index.html")
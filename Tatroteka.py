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
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
    for serwer in serwery:
        try:
            print(f"Pobieram: {opis} ({serwer})...")
            response = requests.post(serwer, data=query, timeout=180)
            if response.status_code == 200 and response.text.strip():
                print(f"OK!")
                return response.json()
            else:
                print(f"Błąd {response.status_code}, próbuję kolejny serwer...")
        except requests.exceptions.Timeout:
            print(f"Timeout, próbuję kolejny serwer...")
        except requests.exceptions.JSONDecodeError:
            print(f"Błąd parsowania JSON, próbuję kolejny serwer...")
    print(f"Wszystkie serwery zawiodły dla: {opis}")
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
    for element in data["elements"]:
        if element["type"] == "relation" and "members" in element:
            linie = []
            for member in element["members"]:
                if member["type"] == "way" and "geometry" in member:
                    punkty = [(p["lon"], p["lat"]) for p in member["geometry"]]
                    if len(punkty) >= 2:
                        linie.append(LineString(punkty))
            if linie:
                try:
                    merged = linemerge(MultiLineString(linie))
                    polys  = list(polygonize(merged))
                    if polys:
                        result = unary_union(polys).buffer(0)
                        print(f"Poligon OK, powierzchnia: {result.area:.4f}")
                        return result
                except Exception as e:
                    print(f"Błąd polygonize: {e}")
    return None

def w_parku(punkty):
    if not punkty:
        return False
    for obszar in [obszar_tpn_buf, obszar_tanap_buf]:
        if obszar is None:
            continue
        try:
            for lat, lon in punkty:
                if obszar.contains(Point(lon, lat)):
                    return True
        except:
            pass
    return False

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
# Pobieramy TYLKO waye należące do oficjalnych relacji szlaków turystycznych.
# query1 pobiera relacje hiking z osmc:symbol (oficjalne oznakowane szlaki PTTK/TPN/TANAP)
# query2 pobiera relacje hiking z network=lwn/rwn/nwn (regionalne/krajowe/światowe)
# Nie pobieramy luźnych wayów highway=path bo to powoduje śmietnik na mapie.

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
[out:json][timeout:60];
relation["name"="Tatrzański Park Narodowy"]["boundary"="national_park"];
out geom;
"""

query_tanap = """
[out:json][timeout:60];
relation["name"="Tatranský národný park"]["boundary"="national_park"];
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

# Połącz wszystkie elementy, deduplikuj po id
elementy_all = {}
for el in dane1["elements"] + dane2["elements"]:
    eid = (el['type'], el.get('id'))
    if eid not in elementy_all:
        elementy_all[eid] = el
wszystkie = list(elementy_all.values())

# Zbierz ID wayów które należą do relacji hiking — tylko te rysujemy
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
# Bufor 0.03° ≈ 3km — szlaki zaczynające się przed bramą parku też będą widoczne
obszar_tpn_buf   = obszar_tpn.buffer(0.03)   if obszar_tpn   else None
obszar_tanap_buf = obszar_tanap.buffer(0.03) if obszar_tanap else None
print(f"TPN: {'OK' if obszar_tpn else 'BŁĄD'}, TANAP: {'OK' if obszar_tanap else 'BŁĄD'}")

# ── Strava ─────────────────────────────────────────────────────────────────────

strava_segmenty = wczytaj_strava("traffic_data.json")
max_effort      = max((s["effort_count"] for s in strava_segmenty), default=1)
strava_dostepna = len(strava_segmenty) > 0
print(f"Max effort_count: {max_effort}")

# ── Buduj słownik geometrii ────────────────────────────────────────────────────
# Zbieramy geometrię ze WSZYSTKICH źródeł przed filtrowaniem parku.
# To jest kluczowa zmiana — wcześniej filtrowaliśmy za wcześnie
# i waye relacji poza centrum parku nie trafiały do way_geometry.

way_geometry = {}  # way_id → [(lat,lon), ...]

# Ze wszystkich elementów
for element in wszystkie:
    if element['type'] == 'way' and 'geometry' in element:
        wid = element['id']
        if wid not in way_geometry:
            way_geometry[wid] = [(p['lat'], p['lon']) for p in element['geometry']]

# Z members relacji (mogą mieć geometrię której nie ma nigdzie indziej)
for element in dane2["elements"]:
    if element['type'] == 'relation' and 'members' in element:
        for member in element['members']:
            if member['type'] == 'way' and 'geometry' in member:
                wid = member['ref']
                if wid not in way_geometry:
                    way_geometry[wid] = [(p['lat'], p['lon']) for p in member['geometry']]

print(f"Zebrano geometrię dla {len(way_geometry)} wayów")

# ── Buduj relacje ──────────────────────────────────────────────────────────────

relacje_dla_way  = {}  # way_id → (nazwa, dlugosc, relacja_id)
relacja_do_wayow = {}  # relacja_id → [way_id, ...]

for element in dane2["elements"]:
    if element['type'] != 'relation' or 'members' not in element:
        continue
    nazwa_rel  = element.get('tags', {}).get('name', 'Brak nazwy')
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

# ── Przebieg 1: spatial join ALL wayów w parku → Strava ───────────────────────

kolory_wayow   = {}  # way_id → segment Strava
kolory_relacji = {}  # relacja_id → najlepszy segment Strava

if strava_dostepna:
    print("Przebieg 1: spatial join way → Strava...")
    for way_id, pts_raw in way_geometry.items():
        pts_skr = uprość_geometrie(pts_raw)
        if (obszar_tpn_buf is not None or obszar_tanap_buf is not None) and not w_parku(pts_skr):
            continue
        seg = znajdz_segment_dla_way(pts_skr, strava_segmenty)
        if not seg:
            continue
        kolory_wayow[way_id] = seg
        if way_id in relacje_dla_way:
            _, _, relacja_id = relacje_dla_way[way_id]
            prev = kolory_relacji.get(relacja_id)
            if prev is None or seg["effort_count"] > prev["effort_count"]:
                kolory_relacji[relacja_id] = seg
    print(f"Dopasowano: {len(kolory_wayow)} wayów | Relacji z danymi: {len(kolory_relacji)}")

# ── Przebieg 1b: propaguj kolor na WSZYSTKIE waye każdej relacji ───────────────

if strava_dostepna:
    propagowane = 0
    for relacja_id, seg in kolory_relacji.items():
        for way_id in relacja_do_wayow.get(relacja_id, []):
            if way_id not in kolory_wayow:
                kolory_wayow[way_id] = seg
                propagowane += 1
    print(f"Propagacja relacji: +{propagowane} | Łącznie: {len(kolory_wayow)}")

# ── Przebieg 1c: flood fill przez endpoints (~200m siatka) ────────────────────

if strava_dostepna:
    print("Przebieg 1c: flood fill...")
    grid = {}
    for wid, pts in way_geometry.items():
        if len(pts) >= 2:
            for pt in [pts[0], pts[-1]]:
                gk = (round(pt[0] * 100), round(pt[1] * 100))
                grid.setdefault(gk, []).append(wid)

    def sasiedzi(wid):
        pts = way_geometry.get(wid)
        if not pts or len(pts) < 2:
            return set()
        wynik = set()
        for pt in [pts[0], pts[-1]]:
            gk = (round(pt[0] * 100), round(pt[1] * 100))
            for dk in [(-2,-2),(-2,-1),(-2,0),(-2,1),(-2,2),
                       (-1,-2),(-1,-1),(-1,0),(-1,1),(-1,2),
                       ( 0,-2),( 0,-1),( 0,0),( 0,1),( 0,2),
                       ( 1,-2),( 1,-1),( 1,0),( 1,1),( 1,2),
                       ( 2,-2),( 2,-1),( 2,0),( 2,1),( 2,2)]:
                for c in grid.get((gk[0]+dk[0], gk[1]+dk[1]), []):
                    if c != wid:
                        wynik.add(c)
        return wynik

    zmieniono = True
    iteracje  = 0
    while zmieniono and iteracje < 30:
        zmieniono = False
        iteracje += 1
        for way_id, pts in way_geometry.items():
            if way_id in kolory_wayow:
                continue
            pts_skr = uprość_geometrie(pts)
            if (obszar_tpn_buf is not None or obszar_tanap_buf is not None) and not w_parku(pts_skr):
                continue
            for s in sasiedzi(way_id):
                if s in kolory_wayow:
                    kolory_wayow[way_id] = kolory_wayow[s]
                    zmieniono = True
                    break
    print(f"Po {iteracje} iteracjach: {len(kolory_wayow)} wayów z kolorem")

# ── Mapa ───────────────────────────────────────────────────────────────────────

mapa = folium.Map(
    location=[49.23, 19.98],
    zoom_start=11,
    control_scale=True,
    tiles="CartoDB dark_matter"
)

grupy = {
    "Szlaki górskie": folium.FeatureGroup(name="Szlaki górskie", show=True),
    "Via ferraty":    folium.FeatureGroup(name="Via ferraty",    show=True),
    "Drogi piesze":   folium.FeatureGroup(name="Drogi piesze",   show=True),
    "Drogi leśne":    folium.FeatureGroup(name="Drogi leśne",    show=True),
    "Pozostałe":      folium.FeatureGroup(name="Pozostałe",      show=True),
}

# ── Przebieg 2: rysowanie ──────────────────────────────────────────────────────

print("Przebieg 2: rysowanie...")
popupy_relacji = {}
odfiltrowane   = 0

for element in wszystkie:
    if element['type'] != 'way' or 'geometry' not in element:
        continue

    way_id = element.get('id')

    # Rysuj TYLKO waye które należą do relacji hiking
    if way_id not in way_ids_w_relacjach:
        continue

    highway  = element.get('tags', {}).get('highway', '')
    styl     = STYL.get(highway, {"color": "#888888", "weight": 2, "grupa": "Szlaki górskie"})
    punkty   = [(p['lat'], p['lon']) for p in element['geometry']]
    punkty   = uprość_geometrie(punkty)

    if (obszar_tpn_buf is not None or obszar_tanap_buf is not None) and not w_parku(punkty):
        odfiltrowane += 1
        continue

    kolor_oryginalny = kolor_szlaku(element)
    typ_nazwa        = nazwa_koloru(element)

    if way_id in relacje_dla_way:
        nazwa, dlugosc_total, relacja_id = relacje_dla_way[way_id]
        info_dlugosc = f"Długość całkowita: {dlugosc_total} km"
        klasa_css    = f"trasa-{relacja_id}"
    else:
        nazwa        = element.get('tags', {}).get('name', 'Brak nazwy')
        info_dlugosc = f"Długość odcinka: {oblicz_dlugosc(punkty)} km"
        klasa_css    = f"trasa-way-{way_id}"
        relacja_id   = None

    seg = kolory_wayow.get(way_id)
    if seg is None and relacja_id is not None:
        seg = kolory_relacji.get(relacja_id)

    strava_info    = ""
    kolor_finalny  = kolor_oryginalny
    weight_finalny = styl["weight"]

    if seg:
        kolor_heat = effort_do_koloru(seg["effort_count"], max_effort)
        if kolor_heat:
            kolor_finalny  = kolor_heat
            weight_finalny = STALA_GRUBOSC
        strava_info = f"""
            <hr style="margin:6px 0">
            <b>&#x1F4CA; Natężenie ruchu (Strava)</b><br>
            Przejść łącznie: <b>{seg['effort_count']:,}</b><br>
            Atletów: {seg['athlete_count']:,}<br>
            Segment: {seg['name']}<br>
            Snapshot: {seg['last_snapshot']}
        """

    popup_tekst = (
        f"<b>{nazwa}</b><br>"
        f"Typ: {typ_nazwa}<br>"
        f"{info_dlugosc}"
        f"{strava_info}"
    )

    linia = folium.PolyLine(
        punkty,
        color=kolor_finalny,
        weight=weight_finalny,
        opacity=0.8,
        tooltip=nazwa,
    )
    linia.options['className'] = klasa_css

    if klasa_css not in popupy_relacji:
        popupy_relacji[klasa_css] = popup_tekst

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
    opacity=0.5
).add_to(grupy["Granice TPN"])
grupy["Granice TPN"].add_to(mapa)

# ── Granice TANAP ──────────────────────────────────────────────────────────────

grupy["Granice TANAP"] = folium.FeatureGroup(name="Granice TANAP", show=True)
if obszar_tanap:
    folium.GeoJson(
        mapping(obszar_tanap),
        style_function=lambda x: {
            "color": "darkgreen", "weight": 3, "opacity": 0.9,
            "fillColor": "darkgreen", "fillOpacity": 0.1,
        },
        interactive=False
    ).add_to(grupy["Granice TANAP"])
else:
    for element in tanap_data["elements"]:
        if element["type"] == "relation" and "members" in element:
            for member in element["members"]:
                if member["type"] == "way" and "geometry" in member:
                    pts = [(p["lat"], p["lon"]) for p in member["geometry"]]
                    folium.PolyLine(pts, color="darkgreen", weight=4, opacity=0.9).add_to(grupy["Granice TANAP"])
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

# ── Legenda ────────────────────────────────────────────────────────────────────


# Przygotuj dane szeregów czasowych per relacja dla JS
relacja_serie = {}
if strava_dostepna:
    try:
        with open("traffic_data.json", encoding="utf-8") as f:
            traffic_raw = json.load(f)
    except:
        traffic_raw = {}

    for relacja_id, seg in kolory_relacji.items():
        seg_id = str(seg["id"])
        serie_raw = {}
        if seg_id in traffic_raw:
            serie_raw = traffic_raw[seg_id].get("series", {})

        daty = sorted(serie_raw.keys())

        # Jeśli brak szeregu czasowego — stwórz sztuczny wpis z datą snapshotu
        # żeby dane skumulowane były zawsze dostępne
        if not daty and seg.get("last_snapshot"):
            daty = [seg["last_snapshot"]]
            serie_raw = {seg["last_snapshot"]: seg["effort_count"]}

        relacja_serie[str(relacja_id)] = {
            "dates":    daty,
            "efforts":  [serie_raw.get(d, 0) for d in daty],
            "max_eff":  seg["effort_count"],
            "seg_name": seg["name"],
        }

relacja_serie_json = json.dumps(relacja_serie)

# Zbierz wszystkie unikalne daty — z serii + ze snapshotów
wszystkie_daty = sorted(set(
    d for v in relacja_serie.values() for d in v["dates"]
))
wszystkie_daty_json = json.dumps(wszystkie_daty)

# Zbierz bazowe kolory linii (bez Strava) per klasa CSS — dla reset
kolory_bazowe = {}
for element in wszystkie:
    if element['type'] != 'way' or 'geometry' not in element:
        continue
    way_id = element.get('id')
    if way_id not in way_ids_w_relacjach:
        continue
    if way_id in relacje_dla_way:
        _, _, relacja_id = relacje_dla_way[way_id]
        klasa = f"trasa-{relacja_id}"
    else:
        klasa = f"trasa-way-{way_id}"
    if klasa not in kolory_bazowe:
        kolory_bazowe[klasa] = kolor_szlaku(element)

kolory_bazowe_json = json.dumps(kolory_bazowe)

# Globalny max effort dla skalowania kolorów
max_effort_global = max_effort

if strava_dostepna:
    mapa.get_root().html.add_child(folium.Element("""
    <div id="legenda-natezenia" style="position:fixed;bottom:120px;left:10px;z-index:1000;
        background:rgba(0,0,0,0.82);padding:10px 14px;border-radius:6px;
        color:white;font-size:11px;font-family:monospace;
        border:1px solid rgba(255,255,255,0.12);min-width:170px;">
        <b>Natężenie ruchu</b><br>
        <div style="width:150px;height:8px;margin:6px 0 3px;
            background:linear-gradient(to right,#143cb4,#3c8cdc,#faf014,#fa6400,#e00000);
            border-radius:2px;"></div>
        <div style="display:flex;justify-content:space-between;width:150px;font-size:9px;color:#aaa">
            <span>Niskie</span><span>Wysokie</span>
        </div>
        <div style="margin-top:5px;font-size:9px;color:#aaa">Szare = brak danych</div>
    </div>
    """))

# ── Kontrolki + suwak czasu ────────────────────────────────────────────────────

mapa.get_root().html.add_child(folium.Element("""
<style>
    #pomiar-btn {
        position:fixed;bottom:30px;right:10px;z-index:1000;
        background:rgba(0,0,0,0.7);border:1px solid rgba(255,255,255,0.3);
        border-radius:4px;padding:5px 8px;cursor:pointer;font-size:13px;color:white;
    }
    #pomiar-btn.aktywny { background:#2a6; border-color:#4c8; }
    #pomiar-wynik {
        position:fixed;bottom:60px;right:10px;z-index:1000;
        background:rgba(0,0,0,0.75);border:1px solid rgba(255,255,255,0.2);
        border-radius:4px;padding:5px 10px;font-size:13px;color:white;display:none;
    }
    #timeline-panel {
        position:fixed;bottom:0;left:0;right:0;z-index:1000;
        background:rgba(8,11,16,0.95);border-top:1px solid #182030;
        padding:8px 18px 10px;font-family:monospace;
        backdrop-filter:blur(6px);
    }
    #tl-top {
        display:flex;align-items:center;gap:12px;margin-bottom:6px;
    }
    #tl-date-label {
        font-size:11px;color:#f0a030;letter-spacing:0.06em;
        min-width:90px;flex-shrink:0;
    }
    #tl-seg-label {
        font-size:9px;color:#3a4a5a;flex:1;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    }
    #tl-play-btn {
        background:#182030;border:none;color:#8aa0b8;
        width:24px;height:24px;border-radius:2px;cursor:pointer;font-size:11px;
        flex-shrink:0;
    }
    #tl-play-btn.playing { background:#e07020;color:white; }
    #tl-slider-wrap {
        display:flex;align-items:center;gap:10px;
    }
    #tl-slider-labels {
        display:flex;justify-content:space-between;
        font-size:8px;color:#3a4a5a;letter-spacing:0.03em;
        margin-bottom:4px;
    }
    #tl-slider-labels span.tl-lbl-active { color:#e07020; }
    input[type=range]#tl-slider {
        -webkit-appearance:none;flex:1;height:3px;
        background:#182030;border-radius:2px;outline:none;cursor:pointer;
    }
    input[type=range]#tl-slider::-webkit-slider-thumb {
        -webkit-appearance:none;width:13px;height:13px;border-radius:50%;
        background:#e07020;border:2px solid #080b10;cursor:pointer;
    }
    #tl-all-btn {
        background:#182030;border:1px solid #182030;color:#8aa0b8;
        font-family:monospace;font-size:8px;padding:3px 8px;
        border-radius:2px;cursor:pointer;white-space:nowrap;flex-shrink:0;
        letter-spacing:0.06em;text-transform:uppercase;
    }
    #tl-all-btn.active { background:#f0a030;border-color:#f0a030;color:#000; }
</style>
<button id="pomiar-btn" title="Zmierz odległość">📏 Pomiar</button>
<div id="pomiar-wynik"></div>
"""))

folium.LayerControl(collapsed=False).add_to(mapa)
folium.plugins.MousePosition(
    position="bottomleft", separator=" | ", prefix="Dł./Szer.:", num_digits=5
).add_to(mapa)

popupy_json = json.dumps(popupy_relacji)

mapa.get_root().script.add_child(folium.Element(f"""
    document.addEventListener("DOMContentLoaded", function() {{
        var popupy       = {popupy_json};
        var relSerie     = {relacja_serie_json};
        var allDates     = {wszystkie_daty_json};
        var koloryBazowe = {kolory_bazowe_json};
        var maxEffort    = {max_effort_global};
        var aktywnaKlasa = null;
        var trybPomiaru  = false;
        var punktyPomiar = [];
        var liniePomiar  = [];
        var markerPomiar = [];
        var mapaL        = null;
        var playInterval = null;
        var currentIdx   = 0; // 0 = ogółem, 1..N = data

        // ── Panel info ────────────────────────────────────────────────────────
        var panel = document.createElement('div');
        panel.id  = 'info-panel';
        panel.style.cssText = `
            position:fixed;top:80px;left:10px;
            background:rgba(0,0,0,0.85);color:white;
            padding:10px 14px;border-radius:6px;
            box-shadow:0 2px 12px rgba(0,0,0,0.5);
            z-index:1000;max-width:280px;
            font-size:13px;display:none;
            border:1px solid rgba(255,255,255,0.15);
            font-family:monospace;
        `;
        document.body.appendChild(panel);

        // ── Suwak czasu ───────────────────────────────────────────────────────
        var tlDiv = document.createElement('div');
        tlDiv.id  = 'timeline-panel';
        // Pokaż suwak zawsze gdy mamy dane Strava (nawet z jedną datą)
        var labelsHtml = allDates.map((d,i) =>
            `<span id="tl-lbl-${{i+1}}">${{d.slice(5).replace('-','.')}}</span>`
        ).join('');
        tlDiv.innerHTML = `
            <div id="tl-top">
                <span id="tl-date-label">OGÓŁEM</span>
                <span id="tl-seg-label">Wszystkie dane skumulowane</span>
                <button id="tl-play-btn" title="Odtwarzaj" ${{allDates.length < 2 ? 'disabled style="opacity:0.3"' : ''}}>▶</button>
            </div>
            <div id="tl-slider-labels">
                <span id="tl-lbl-0" class="tl-lbl-active">Ogółem</span>
                ${{labelsHtml}}
            </div>
            <div id="tl-slider-wrap">
                <button id="tl-all-btn" class="active">Ogółem</button>
                <input type="range" id="tl-slider" min="0" max="${{allDates.length}}" value="0" step="1"
                    ${{allDates.length === 0 ? 'disabled' : ''}}>
            </div>
        `;
        document.body.appendChild(tlDiv);

        // ── Kolor z effort ────────────────────────────────────────────────────
        function effortToColor(effort, maxEff) {{
            if (!effort || !maxEff) return null;
            var t = Math.log(1+effort) / Math.log(1+maxEff);
            var r,g,b;
            if (t < 0.25) {{
                var tt=t/0.25; r=Math.round(20+tt*40); g=Math.round(60+tt*80); b=Math.round(180+tt*40);
            }} else if (t < 0.5) {{
                var tt=(t-0.25)/0.25; r=Math.round(60+tt*190); g=Math.round(140+tt*100); b=Math.round(220-tt*200);
            }} else if (t < 0.75) {{
                var tt=(t-0.5)/0.25; r=250; g=Math.round(240-tt*140); b=Math.round(20-tt*20);
            }} else {{
                var tt=(t-0.75)/0.25; r=Math.round(250-tt*20); g=Math.round(100-tt*100); b=0;
            }}
            return `rgb(${{r}},${{g}},${{b}})`;
        }}

        // ── Przemaluj linie na podstawie daty ────────────────────────────────
        function recolorLines(dateIdx) {{
            var date = dateIdx === 0 ? null : allDates[dateIdx-1];

            // Oblicz max effort dla wybranej daty (dla skalowania kolorów)
            var dayMaxEffort = 0;
            if (date) {{
                Object.values(relSerie).forEach(function(s) {{
                    var idx = s.dates.indexOf(date);
                    if (idx >= 0 && s.efforts[idx] > dayMaxEffort) dayMaxEffort = s.efforts[idx];
                }});
            }}

            document.querySelectorAll('path[class]').forEach(function(el) {{
                var klasa = Array.from(el.classList).find(k => k.startsWith('trasa-'));
                if (!klasa) return;
                var relId = klasa.replace('trasa-','');
                var serie = relSerie[relId];
                var bazowy = koloryBazowe[klasa] || '#888888';

                if (!serie) {{
                    el.style.stroke = bazowy;
                    return;
                }}

                if (!date) {{
                    // Widok ogólny — użyj max effort segmentu
                    var kolor = effortToColor(serie.max_eff, maxEffort);
                    el.style.stroke = kolor || bazowy;
                }} else {{
                    var idx = serie.dates.indexOf(date);
                    var eff = idx >= 0 ? serie.efforts[idx] : 0;
                    if (eff > 0) {{
                        el.style.stroke = effortToColor(eff, dayMaxEffort || 1);
                    }} else {{
                        el.style.stroke = bazowy;
                    }}
                }}
            }});
        }}

        // ── Aktualizuj UI suwaka ──────────────────────────────────────────────
        function updateTimelineUI(idx) {{
            currentIdx = idx;
            var date = idx === 0 ? null : allDates[idx-1];

            // Etykiety
            document.querySelectorAll('#tl-slider-labels span').forEach(function(el, i) {{
                el.className = i === idx ? 'tl-lbl-active' : '';
            }});

            // Przycisk Ogółem
            var allBtn = document.getElementById('tl-all-btn');
            if (allBtn) allBtn.className = idx===0 ? 'active' : '';

            // Label daty
            var dateLbl = document.getElementById('tl-date-label');
            var segLbl  = document.getElementById('tl-seg-label');
            if (dateLbl) dateLbl.textContent = date ? date.slice(5).replace('-','.') : 'OGÓŁEM';
            if (segLbl)  segLbl.textContent  = date ? 'Aktywność dzienna' : 'Wszystkie dane skumulowane';

            recolorLines(idx);
        }}

        // ── Inicjalizuj suwak ─────────────────────────────────────────────────
        setTimeout(function() {{
            var slider = document.getElementById('tl-slider');
            if (slider) {{
                slider.addEventListener('input', function() {{
                    updateTimelineUI(parseInt(this.value));
                }});
            }}

            var allBtn = document.getElementById('tl-all-btn');
            if (allBtn) {{
                allBtn.addEventListener('click', function() {{
                    if (slider) slider.value = 0;
                    updateTimelineUI(0);
                }});
            }}

            var playBtn = document.getElementById('tl-play-btn');
            if (playBtn) {{
                playBtn.addEventListener('click', function() {{
                    if (playInterval) {{
                        clearInterval(playInterval);
                        playInterval = null;
                        this.textContent = '▶';
                        this.classList.remove('playing');
                    }} else {{
                        this.textContent = '⏸';
                        this.classList.add('playing');
                        var idx = currentIdx >= allDates.length ? 1 : currentIdx + 1;
                        playInterval = setInterval(function() {{
                            if (slider) slider.value = idx;
                            updateTimelineUI(idx);
                            idx++;
                            if (idx > allDates.length) {{
                                clearInterval(playInterval);
                                playInterval = null;
                                if (playBtn) {{ playBtn.textContent='▶'; playBtn.classList.remove('playing'); }}
                            }}
                        }}, 1500);
                    }}
                }});
            }}

            // Inicjalny kolor — z retry bo SVG może się jeszcze ładować
            function applyInitialColors() {{
                var paths = document.querySelectorAll('path[class]');
                if (paths.length === 0) {{
                    setTimeout(applyInitialColors, 500);
                    return;
                }}
                updateTimelineUI(0);
            }}
            setTimeout(applyInitialColors, 1800);

        // ── Kliknięcia na linie ───────────────────────────────────────────────
        function podswietl(klasa, aktywny) {{
            document.querySelectorAll('path.' + klasa).forEach(function(el) {{
                el.style.opacity     = aktywny ? '1.0' : '0.8';
                el.style.strokeWidth = aktywny ? '6px' : '';
            }});
        }}

        setTimeout(function() {{
            document.querySelectorAll('path[class]').forEach(function(el) {{
                var klasa = Array.from(el.classList).find(k => k.startsWith('trasa-'));
                if (!klasa) return;
                el.addEventListener('click', function(e) {{
                    if (trybPomiaru) return;
                    e.stopPropagation();
                    if (aktywnaKlasa && aktywnaKlasa !== klasa) podswietl(aktywnaKlasa, false);
                    aktywnaKlasa = klasa;
                    podswietl(klasa, true);
                    if (popupy[klasa]) {{
                        panel.innerHTML = popupy[klasa] + '<br><small style="color:#888">Kliknij mapę aby zamknąć</small>';
                        panel.style.display = 'block';
                    }}
                }});
            }});
            document.querySelector('.leaflet-container').addEventListener('click', function() {{
                if (trybPomiaru) return;
                if (aktywnaKlasa) {{
                    podswietl(aktywnaKlasa, false);
                    aktywnaKlasa = null;
                    panel.style.display = 'none';
                }}
            }});
        }}, 1500);

        // ── Pomiar odległości ─────────────────────────────────────────────────
        setTimeout(function() {{
            mapaL = Object.values(window).find(v => v && v._leaflet_id && v.getCenter);
            if (!mapaL) return;

            function obliczDystans(p1, p2) {{
                var R=6371,dLat=(p2.lat-p1.lat)*Math.PI/180,dLon=(p2.lng-p1.lng)*Math.PI/180;
                var a=Math.sin(dLat/2)*Math.sin(dLat/2)+
                      Math.cos(p1.lat*Math.PI/180)*Math.cos(p2.lat*Math.PI/180)*
                      Math.sin(dLon/2)*Math.sin(dLon/2);
                return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
            }}

            function resetPomiar() {{
                liniePomiar.forEach(l=>mapaL.removeLayer(l));
                markerPomiar.forEach(m=>mapaL.removeLayer(m));
                liniePomiar=[];markerPomiar=[];punktyPomiar=[];
                document.getElementById('pomiar-wynik').style.display='none';
            }}

            document.getElementById('pomiar-btn').addEventListener('click', function() {{
                trybPomiaru=!trybPomiaru;
                this.classList.toggle('aktywny',trybPomiaru);
                this.textContent=trybPomiaru?'✖ Zakończ pomiar':'📏 Pomiar';
                if (!trybPomiaru) resetPomiar();
            }});

            mapaL.on('click', function(e) {{
                if (!trybPomiaru) return;
                punktyPomiar.push(e.latlng);
                var marker=L.circleMarker(e.latlng,{{radius:4,color:'cyan',fillColor:'cyan',fillOpacity:1}}).addTo(mapaL);
                markerPomiar.push(marker);
                if (punktyPomiar.length>1) {{
                    var p1=punktyPomiar[punktyPomiar.length-2],p2=punktyPomiar[punktyPomiar.length-1];
                    liniePomiar.push(L.polyline([p1,p2],{{color:'cyan',weight:2,opacity:0.8,dashArray:'6,4'}}).addTo(mapaL));
                    var total=0;
                    for (var i=1;i<punktyPomiar.length;i++) total+=obliczDystans(punktyPomiar[i-1],punktyPomiar[i]);
                    var wynik=document.getElementById('pomiar-wynik');
                    wynik.textContent='Dystans: '+total.toFixed(2)+' km';
                    wynik.style.display='block';
                }}
            }});
        }}, 2000);
    }});
"""))

mapa.save("index.html")
print("Gotowe! Zapisano index.html")
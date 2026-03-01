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

def sanitize(s):
    """Usuń surrogaty i nieprawidłowe znaki Unicode które crashują utf-8 encode."""
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
obszar_tpn_buf   = obszar_tpn.buffer(0.03)   if obszar_tpn   else None
obszar_tanap_buf = obszar_tanap.buffer(0.03) if obszar_tanap else None
print(f"TPN: {'OK' if obszar_tpn else 'BŁĄD'}, TANAP: {'OK' if obszar_tanap else 'BŁĄD'}")

# ── Strava ─────────────────────────────────────────────────────────────────────

strava_segmenty = wczytaj_strava("traffic_data.json")
max_effort      = max((s["effort_count"] for s in strava_segmenty), default=1)
strava_dostepna = len(strava_segmenty) > 0
print(f"Max effort_count: {max_effort}")

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

# ── Spatial join + propagacja ──────────────────────────────────────────────────

kolory_wayow   = {}
kolory_relacji = {}

if strava_dostepna:
    print("Przebieg 1: spatial join...")
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
    print(f"Dopasowano: {len(kolory_wayow)} wayów | Relacji: {len(kolory_relacji)}")

    propagowane = 0
    for relacja_id, seg in kolory_relacji.items():
        for way_id in relacja_do_wayow.get(relacja_id, []):
            if way_id not in kolory_wayow:
                kolory_wayow[way_id] = seg
                propagowane += 1
    print(f"Propagacja: +{propagowane} | Łącznie: {len(kolory_wayow)}")

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

# Dodaj jasny tryb jako alternatywną warstwę bazową
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
        nazwa        = sanitize(nazwa)
        info_dlugosc = f"Długość całkowita: {dlugosc_total} km"
        klasa_css    = f"trasa-{relacja_id}"
    else:
        nazwa        = sanitize(element.get('tags', {}).get('name', 'Brak nazwy'))
        info_dlugosc = f"Długość odcinka: {oblicz_dlugosc(punkty)} km"
        klasa_css    = f"trasa-way-{way_id}"
        relacja_id   = None

    # Zapamiętaj bazowy kolor (bez Strava) do suwaka
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

    # Przechowuj surowe dane — HTML generuje JS
    if klasa_css not in popupy_relacji:
        popupy_relacji[klasa_css] = {
            "nazwa":    nazwa,
            "typ":      typ_nazwa,
            "dlugosc":  info_dlugosc,
            "effort":   seg["effort_count"]   if seg else 0,
            "atleci":   seg["athlete_count"]  if seg else 0,
            "seg_name": sanitize(seg["name"]) if seg else "",
            "snapshot": seg["last_snapshot"]  if seg else "",
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
        relacja_serie[str(relacja_id)] = {
            "dates":   daty,
            "efforts": [serie_raw.get(d, 0) for d in daty],
            "max_eff": seg["effort_count"],
        }

wszystkie_daty = sorted(set(d for v in relacja_serie.values() for d in v["dates"]))

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
    #tl-panel {
        position:fixed;bottom:0;left:0;right:0;z-index:1000;
        background:rgba(8,11,16,0.96);border-top:1px solid #1a2535;
        padding:7px 18px 9px;display:flex;align-items:center;gap:14px;
        font-family:monospace;
    }
    #tl-date {
        font-size:11px;color:#f0a030;min-width:68px;
        letter-spacing:.05em;flex-shrink:0;
    }
    #tl-wrap { flex:1;display:flex;flex-direction:column;gap:5px; }
    #tl-lbls {
        display:flex;justify-content:space-between;
        font-size:8px;color:#3a4a5a;letter-spacing:.03em;
    }
    #tl-lbls span.act { color:#e07020; }
    input[type=range]#tl-sl {
        -webkit-appearance:none;width:100%;height:3px;
        background:#1a2535;border-radius:2px;outline:none;cursor:pointer;
    }
    input[type=range]#tl-sl::-webkit-slider-thumb {
        -webkit-appearance:none;width:13px;height:13px;
        border-radius:50%;background:#e07020;
        border:2px solid #080b10;cursor:pointer;
    }
    input[type=range]#tl-sl::-moz-range-thumb {
        width:13px;height:13px;border-radius:50%;
        background:#e07020;border:2px solid #080b10;cursor:pointer;
    }
    #tl-play {
        background:#1a2535;border:none;color:#8aa0b8;
        width:26px;height:26px;border-radius:2px;
        cursor:pointer;font-size:13px;flex-shrink:0;
        display:flex;align-items:center;justify-content:center;
    }
    #tl-play.on { background:#e07020;color:white; }
</style>
<button id="pomiar-btn" title="Zmierz odległość">&#x1F4CF; Pomiar</button>
<div id="pomiar-wynik"></div>
<button id="theme-btn" title="Przełącz tryb jasny/ciemny">&#9790; Ciemny</button>
<style>
    #theme-btn {
        position:fixed;top:10px;right:10px;z-index:1000;
        background:rgba(0,0,0,0.7);border:1px solid rgba(255,255,255,0.3);
        border-radius:4px;padding:5px 10px;cursor:pointer;font-size:12px;color:white;
        font-family:monospace;
    }
    #theme-btn.light {
        background:rgba(255,255,255,0.9);border-color:rgba(0,0,0,0.2);color:#333;
    }
</style>
"""))

# ── Wstrzyknij dane inline jako window.TD ─────────────────────────────────────

td = {
    "popupy":       popupy_relacji,
    "relSerie":     relacja_serie,
    "allDates":     wszystkie_daty,
    "koloryBazowe": kolory_bazowe,
    "maxEffort":    max_effort,
}
td_json = json.dumps(td, ensure_ascii=True)
print(f"window.TD rozmiar: {len(td_json)//1024} KB")
mapa.get_root().html.add_child(folium.Element(
    "<script>window.TD=" + td_json + ";</script>"
))

# ── Cały JS jako stała string — zero f-stringów, zero konfliktów ───────────────

JS = """
document.addEventListener("DOMContentLoaded", function() {
    var TD  = window.TD || {};
    var popupy       = TD.popupy       || {};
    var relSerie     = TD.relSerie     || {};
    var allDates     = TD.allDates     || [];
    var koloryBazowe = TD.koloryBazowe || {};
    var maxEffort    = TD.maxEffort    || 1;

    var aktywnaKlasa = null;
    var trybPomiaru  = false;
    var punktyPomiar = [];
    var liniePomiar  = [];
    var markerPomiar = [];
    var mapaL        = null;
    var playTimer    = null;
    var currentIdx   = 0;

    // ── Info panel ──────────────────────────────────────────────────────────
    var panel = document.createElement('div');
    panel.id  = 'info-panel';
    panel.style.cssText = [
        'position:fixed;top:80px;left:10px',
        'background:rgba(0,0,0,0.85);color:white',
        'padding:10px 14px;border-radius:6px',
        'box-shadow:0 2px 12px rgba(0,0,0,0.5)',
        'z-index:1000;max-width:280px;font-size:13px;display:none',
        'border:1px solid rgba(255,255,255,0.15);font-family:monospace'
    ].join(';');
    document.body.appendChild(panel);

    // ── Suwak czasu ─────────────────────────────────────────────────────────
    var tlPanel = document.createElement('div');
    tlPanel.id  = 'tl-panel';
    var lbls = allDates.map(function(d, i) {
        return '<span id="tll' + (i+1) + '">' + d.slice(5).replace('-', '.') + '</span>';
    }).join('');
    var noData = allDates.length === 0;
    tlPanel.innerHTML =
        '<span id="tl-date">' + (noData ? 'BRAK DANYCH' : 'OG\u00d3\u0141EM') + '</span>' +
        '<div id="tl-wrap">' +
          '<div id="tl-lbls">' +
            (noData
              ? '<span style="color:#3a4a5a;font-size:9px">Uruchom Tatroteka.py z traffic_data.json aby zobaczy\u0107 natężenie ruchu</span>'
              : '<span id="tll0" class="act">Og\u00f3\u0142em</span>' + lbls
            ) +
          '</div>' +
          '<input type="range" id="tl-sl" min="0" max="' + allDates.length + '" value="0" step="1"' +
            (noData ? ' disabled style="opacity:0.3"' : '') + '>' +
        '</div>' +
        '<button id="tl-play"' + (noData ? ' disabled style="opacity:0.3"' : '') + '>\u25b6</button>';
    document.body.appendChild(tlPanel);

    // ── Kolor z effort ──────────────────────────────────────────────────────
    function eff2col(eff, mx) {
        if (!eff || !mx) return null;
        var t = Math.log(1 + eff) / Math.log(1 + mx);
        var r, g, b, tt;
        if (t < 0.25) {
            tt = t / 0.25;
            r = Math.round(20 + tt*40); g = Math.round(60 + tt*80); b = Math.round(180 + tt*40);
        } else if (t < 0.5) {
            tt = (t - 0.25) / 0.25;
            r = Math.round(60 + tt*190); g = Math.round(140 + tt*100); b = Math.round(220 - tt*200);
        } else if (t < 0.75) {
            tt = (t - 0.5) / 0.25;
            r = 250; g = Math.round(240 - tt*140); b = Math.round(20 - tt*20);
        } else {
            tt = (t - 0.75) / 0.25;
            r = Math.round(250 - tt*20); g = Math.round(100 - tt*100); b = 0;
        }
        return 'rgb(' + r + ',' + g + ',' + b + ')';
    }

    // ── Przemaluj linie SVG ─────────────────────────────────────────────────
    function recolor(idx) {
        var date = idx === 0 ? null : allDates[idx - 1];
        var dayMax = 0;
        if (date) {
            Object.values(relSerie).forEach(function(s) {
                var i = s.dates.indexOf(date);
                if (i >= 0 && s.efforts[i] > dayMax) dayMax = s.efforts[i];
            });
        }
        document.querySelectorAll('path[class]').forEach(function(el) {
            var kl = Array.from(el.classList).find(function(c) {
                return c.startsWith('trasa-');
            });
            if (!kl) return;
            var rid = kl.replace('trasa-', '');
            var s   = relSerie[rid];
            var baz = koloryBazowe[kl] || '#888888';
            if (!s) { el.style.stroke = baz; return; }
            if (!date) {
                el.style.stroke = eff2col(s.max_eff, maxEffort) || baz;
            } else {
                var i = s.dates.indexOf(date);
                var e = i >= 0 ? s.efforts[i] : 0;
                el.style.stroke = e > 0 ? eff2col(e, dayMax || 1) : baz;
            }
        });
    }

    // ── Ustaw pozycję suwaka ────────────────────────────────────────────────
    function setIdx(idx) {
        currentIdx = idx;
        document.getElementById('tl-sl').value = idx;
        var date = idx === 0 ? null : allDates[idx - 1];
        document.getElementById('tl-date').textContent =
            date ? date.slice(5).replace('-', '.') : 'OG\u00d3\u0141EM';
        document.querySelectorAll('#tl-lbls span').forEach(function(el, i) {
            el.className = i === idx ? 'act' : '';
        });
        recolor(idx);
    }

    // ── Eventy suwaka ───────────────────────────────────────────────────────
    setTimeout(function() {
        document.getElementById('tl-sl').addEventListener('input', function() {
            setIdx(parseInt(this.value));
        });
        document.getElementById('tl-play').addEventListener('click', function() {
            if (playTimer) {
                clearInterval(playTimer); playTimer = null;
                this.innerHTML = '\u25b6'; this.className = '';
            } else {
                var btn = this;
                btn.innerHTML = '\u23f8'; btn.className = 'on';
                var idx = currentIdx >= allDates.length ? 0 : currentIdx;
                playTimer = setInterval(function() {
                    idx++;
                    setIdx(idx);
                    if (idx >= allDates.length) {
                        clearInterval(playTimer); playTimer = null;
                        btn.innerHTML = '\u25b6'; btn.className = '';
                    }
                }, 1500);
            }
        });
        // Zastosuj kolory gdy SVG w DOM
        function applyWhenReady() {
            if (document.querySelectorAll('path[class]').length === 0) {
                setTimeout(applyWhenReady, 300);
                return;
            }
            setIdx(0);
        }
        applyWhenReady();
    }, 1200);

    // ── Podświetlanie szlaków ───────────────────────────────────────────────
    function podswietl(kl, on) {
        document.querySelectorAll('path.' + kl).forEach(function(el) {
            el.style.opacity     = on ? '1.0' : '0.8';
            el.style.strokeWidth = on ? '6px' : '';
        });
    }

    setTimeout(function() {
        document.querySelectorAll('path[class]').forEach(function(el) {
            var kl = Array.from(el.classList).find(function(c) {
                return c.startsWith('trasa-');
            });
            if (!kl) return;
            el.addEventListener('click', function(e) {
                if (trybPomiaru) return;
                e.stopPropagation();
                if (aktywnaKlasa && aktywnaKlasa !== kl) podswietl(aktywnaKlasa, false);
                aktywnaKlasa = kl;
                podswietl(kl, true);
                if (popupy[kl]) {
                    var p = popupy[kl];
                    var html = '<b>' + p.nazwa + '</b><br>' +
                               'Typ: ' + p.typ + '<br>' +
                               p.dlugosc;
                    if (p.effort > 0) {
                        html += '<hr style="margin:6px 0">' +
                                '<b>&#x1F4CA; Nat\u0119\u017cenie ruchu (Strava)</b><br>' +
                                'Przej\u015b\u0107 \u0142\u0105cznie: <b>' + p.effort.toLocaleString() + '</b><br>' +
                                'Atle\u0107w: ' + p.atleci.toLocaleString() + '<br>' +
                                'Segment: ' + p.seg_name + '<br>' +
                                'Snapshot: ' + p.snapshot;
                    }
                    panel.innerHTML = html + '<br><small style="color:#888">Kliknij map\u0119 aby zamkn\u0105\u0107</small>';
                    panel.style.display = 'block';
                }
            });
        });
        document.querySelector('.leaflet-container').addEventListener('click', function() {
            if (trybPomiaru) return;
            if (aktywnaKlasa) {
                podswietl(aktywnaKlasa, false);
                aktywnaKlasa = null;
                panel.style.display = 'none';
            }
        });
    }, 1500);

    // ── Przełącznik trybu jasny/ciemny ─────────────────────────────────────
    setTimeout(function() {
        mapaL = Object.values(window).find(function(v) {
            return v && v._leaflet_id && v.getCenter;
        });
        if (!mapaL) return;

        var isDark = true;
        var darkUrl  = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
        var lightUrl = 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png';
        var currentTile = null;

        // Znajdź aktywną warstwę bazową
        mapaL.eachLayer(function(layer) {
            if (layer._url && (layer._url.indexOf('carto') !== -1 || layer._url.indexOf('basemaps') !== -1)) {
                currentTile = layer;
            }
        });

        document.getElementById('theme-btn').addEventListener('click', function() {
            isDark = !isDark;
            // Usuń wszystkie warstwy tileset
            mapaL.eachLayer(function(layer) {
                if (layer._url) mapaL.removeLayer(layer);
            });
            // Dodaj nową
            L.tileLayer(isDark ? darkUrl : lightUrl, {
                attribution: 'CartoDB',
                subdomains: 'abcd',
                maxZoom: 19
            }).addTo(mapaL);

            // Aktualizuj przycisk
            this.innerHTML = isDark ? '\u263E Ciemny' : '\u2600 Jasny';
            this.className = isDark ? '' : 'light';

            // Dostosuj kolor TPN overlay (jasny/ciemny tył)
            document.querySelectorAll('.leaflet-overlay-pane svg').forEach(function(svg) {
                svg.style.filter = isDark ? '' : 'brightness(0.7)';
            });
        });
    }, 2100);

        function dist(p1, p2) {
            var R = 6371;
            var dLat = (p2.lat - p1.lat) * Math.PI / 180;
            var dLon = (p2.lng - p1.lng) * Math.PI / 180;
            var a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                    Math.cos(p1.lat * Math.PI/180) * Math.cos(p2.lat * Math.PI/180) *
                    Math.sin(dLon/2) * Math.sin(dLon/2);
            return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
        }

        function resetPomiar() {
            liniePomiar.forEach(function(l) { mapaL.removeLayer(l); });
            markerPomiar.forEach(function(m) { mapaL.removeLayer(m); });
            liniePomiar = []; markerPomiar = []; punktyPomiar = [];
            document.getElementById('pomiar-wynik').style.display = 'none';
        }

        document.getElementById('pomiar-btn').addEventListener('click', function() {
            trybPomiaru = !trybPomiaru;
            this.classList.toggle('aktywny', trybPomiaru);
            this.textContent = trybPomiaru ? '\u2716 Zako\u0144cz pomiar' : '\uD83D\uDCCF Pomiar';
            if (!trybPomiaru) resetPomiar();
        });

        mapaL.on('click', function(e) {
            if (!trybPomiaru) return;
            punktyPomiar.push(e.latlng);
            var mk = L.circleMarker(e.latlng, {
                radius: 4, color: 'cyan', fillColor: 'cyan', fillOpacity: 1
            }).addTo(mapaL);
            markerPomiar.push(mk);
            if (punktyPomiar.length > 1) {
                var p1 = punktyPomiar[punktyPomiar.length - 2];
                var p2 = punktyPomiar[punktyPomiar.length - 1];
                liniePomiar.push(
                    L.polyline([p1, p2], {
                        color: 'cyan', weight: 2, opacity: 0.8, dashArray: '6,4'
                    }).addTo(mapaL)
                );
                var total = 0;
                for (var i = 1; i < punktyPomiar.length; i++) {
                    total += dist(punktyPomiar[i-1], punktyPomiar[i]);
                }
                var wy = document.getElementById('pomiar-wynik');
                wy.textContent = 'Dystans: ' + total.toFixed(2) + ' km';
                wy.style.display = 'block';
            }
        });
    }, 2000);
});
"""

mapa.get_root().script.add_child(folium.Element(JS))

import io

# Przechwytujemy HTML z Folium i czyścimy surrogaty przed zapisem
buf = io.StringIO()
try:
    mapa.save("index.html")
except UnicodeEncodeError:
    # Folium/branca crashuje na surogatach — generuj HTML ręcznie
    root = mapa.get_root()
    html_out = root.render()
    html_out = html_out.encode('utf-8', errors='replace').decode('utf-8')
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print("Zapisano index.html (z czyszczeniem surogatów)")
else:
    # Jeśli save się udał, przepisz z czyszczeniem na wszelki wypadek
    with open("index.html", encoding="utf-8", errors="replace") as f:
        html_out = f.read()
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print("Gotowe! Zapisano index.html")
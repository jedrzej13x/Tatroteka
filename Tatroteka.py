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

# ── Strava: wczytaj traffic_data.json ─────────────────────────────────────────

def wczytaj_strava(path="traffic_data.json"):
    """
    Wczytuje traffic_data.json i zwraca listę segmentów z effort_count.
    Zwraca [] jeśli plik nie istnieje.
    """
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
    """Zwraca najbliższy segment Strava w promieniu dla jednego punktu."""
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
    """
    Próbkuje wiele punktów wzdłuż odcinka OSM (końce, środek, ćwiartki, co 8. punkt).
    Zwraca segment z najwyższym effort_count spośród trafień.
    """
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
    """
    Mapuje effort_count na kolor w skali niebieski → żółty → czerwony.
    Używa skali logarytmicznej żeby lepiej pokazać różnice.
    """
    if max_effort <= 0 or effort <= 0:
        return None  # brak danych — użyj oryginalnego koloru szlaku

    t = math.log(1 + effort) / math.log(1 + max_effort)

    if t < 0.25:
        # ciemnoniebieski → niebieski
        r = int(20  + t * 4 * 40)
        g = int(60  + t * 4 * 80)
        b = int(180 + t * 4 * 40)
    elif t < 0.5:
        # niebieski → żółty
        tt = (t - 0.25) * 4
        r  = int(60  + tt * 190)
        g  = int(140 + tt * 100)
        b  = int(220 - tt * 200)
    elif t < 0.75:
        # żółty → pomarańczowy
        tt = (t - 0.5) * 4
        r  = int(250)
        g  = int(240 - tt * 140)
        b  = int(20  - tt * 20)
    else:
        # pomarańczowy → czerwony
        tt = (t - 0.75) * 4
        r  = int(250 - tt * 20)
        g  = int(100 - tt * 100)
        b  = 0

    return f"#{r:02x}{g:02x}{b:02x}"

def effort_do_grubosci(effort, max_effort):
    if max_effort <= 0 or effort <= 0:
        return 2
    t = math.log(1 + effort) / math.log(1 + max_effort)
    return round(1.5 + t * 5.5, 1)

# ── Stałe ──────────────────────────────────────────────────────────────────────

BBOX = "(49.10, 19.60, 49.35, 20.25)"

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
[out:json][timeout:120];
(
  way["highway"="path"]{BBOX};
  way["highway"="via_ferrata"]{BBOX};
  way["highway"="footway"]{BBOX};
  way["highway"="pedestrian"]{BBOX};
  way["highway"="steps"]{BBOX};
  way["highway"="track"]{BBOX};
);
out geom;
"""

query2 = f"""
[out:json][timeout:120];
(
  relation["route"="hiking"]{BBOX};
  way["highway"~"secondary|tertiary|unclassified"]["foot"!="no"]{BBOX};
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

dane1      = pobierz_dane(query1,      "ścieżki i szlaki")
time.sleep(5)
dane2      = pobierz_dane(query2,      "drogi i relacje")
time.sleep(5)
tpn_data   = pobierz_dane(query_tpn,   "granice TPN")
time.sleep(5)
tanap_data = pobierz_dane(query_tanap, "granice TANAP")

wszystkie = dane1["elements"] + dane2["elements"]
print(f"Łącznie pobrano {len(wszystkie)} elementów")

# ── Budujemy poligony ──────────────────────────────────────────────────────────

obszar_tpn   = zbuduj_poligon(tpn_data)
obszar_tanap = zbuduj_poligon(tanap_data)

obszar_tpn_buf   = obszar_tpn.buffer(0.01)   if obszar_tpn   else None
obszar_tanap_buf = obszar_tanap.buffer(0.01) if obszar_tanap else None

print(f"TPN: {'OK' if obszar_tpn else 'BŁĄD'}, TANAP: {'OK' if obszar_tanap else 'BŁĄD'}")

# ── Wczytaj dane Strava ───────────────────────────────────────────────────────

strava_segmenty = wczytaj_strava("traffic_data.json")
max_effort      = max((s["effort_count"] for s in strava_segmenty), default=1)
strava_dostepna = len(strava_segmenty) > 0

print(f"Max effort_count: {max_effort}")

# ── Długości i nazwy relacji ───────────────────────────────────────────────────

relacje_dla_way  = {}  # way_id → (nazwa, dlugosc, relacja_id)
relacja_do_wayow = {}  # relacja_id → [way_id, ...]

for element in dane2["elements"]:
    if element['type'] == 'relation':
        nazwa_rel  = element.get('tags', {}).get('name', 'Brak nazwy')
        relacja_id = element['id']
        if 'members' not in element:
            continue
        total   = 0
        way_ids = []
        for member in element['members']:
            if member['type'] == 'way':
                way_ids.append(member['ref'])
                if 'geometry' in member:
                    pts = [(p['lat'], p['lon']) for p in member['geometry']]
                    total += oblicz_dlugosc(pts)
        dlugosc_rel = round(total, 2)
        relacja_do_wayow[relacja_id] = way_ids
        for wid in way_ids:
            relacje_dla_way[wid] = (nazwa_rel, dlugosc_rel, relacja_id)

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

if strava_dostepna:
    grupy["Natężenie ruchu"] = folium.FeatureGroup(name="Natężenie ruchu (Strava)", show=True)

popupy_relacji  = {}
odfiltrowane    = 0
dopasowane      = 0
kolory_wayow    = {}
kolory_relacji  = {}
way_meta        = {}

STALA_GRUBOSC = 3

# ── Przebieg 1: zbierz bezpośrednie dopasowania way → segment Strava ──────────

print("Przebieg 1: spatial join way → Strava...")
for element in wszystkie:
    if element['type'] != 'way' or 'geometry' not in element:
        continue
    punkty_raw = [(p['lat'], p['lon']) for p in element['geometry']]
    punkty     = uprość_geometrie(punkty_raw)
    way_id     = element.get('id')

    if (obszar_tpn_buf is not None or obszar_tanap_buf is not None) and not w_parku(punkty):
        continue

    n  = len(punkty_raw)
    pp = [punkty_raw[i] for i in sorted(set([0, n//4, n//2, 3*n//4, n-1]))]
    way_meta[way_id] = {"pts": pp, "start": punkty_raw[0], "end": punkty_raw[-1]}

    if strava_dostepna:
        seg = znajdz_segment_dla_way(punkty, strava_segmenty)
        if seg:
            kolory_wayow[way_id] = seg
            dopasowane += 1
            if way_id in relacje_dla_way:
                _, _, relacja_id = relacje_dla_way[way_id]
                prev = kolory_relacji.get(relacja_id)
                if prev is None or seg["effort_count"] > prev["effort_count"]:
                    kolory_relacji[relacja_id] = seg

print(f"Dopasowano bezpośrednio: {dopasowane} wayów | Relacji z danymi: {len(kolory_relacji)}")

# ── Przebieg 1b: propaguj kolor na WSZYSTKIE waye tej samej relacji ───────────

if strava_dostepna:
    propagowane = 0
    for relacja_id, seg in kolory_relacji.items():
        for way_id in relacja_do_wayow.get(relacja_id, []):
            if way_id not in kolory_wayow:
                kolory_wayow[way_id] = seg
                propagowane += 1
    print(f"Propagacja relacji: +{propagowane} wayów | Łącznie: {len(kolory_wayow)}")

# ── Przebieg 1c: flood fill po endpoints (tylko wzdłuż połączonych wayów) ─────

if strava_dostepna:
    print("Przebieg 1c: flood fill po połączeniach topologicznych...")

    # Buduj słownik: zaokrąglony punkt → lista way_id (siatka ~50m)
    grid_pts = {}
    for wid, meta in way_meta.items():
        for pt in [meta["start"], meta["end"]]:
            gk = (round(pt[0] * 200), round(pt[1] * 200))
            grid_pts.setdefault(gk, []).append(wid)

    def sasiedzi_przez_endpoint(way_id):
        meta = way_meta[way_id]
        wynik = set()
        for pt in [meta["start"], meta["end"]]:
            gk = (round(pt[0] * 200), round(pt[1] * 200))
            for dk in [(-1,-1),(-1,0),(-1,1),(0,-1),(0,0),(0,1),(1,-1),(1,0),(1,1)]:
                for cand in grid_pts.get((gk[0]+dk[0], gk[1]+dk[1]), []):
                    if cand != way_id:
                        wynik.add(cand)
        return wynik

    zmieniono = True
    iteracje  = 0
    while zmieniono and iteracje < 30:
        zmieniono = False
        iteracje += 1
        for way_id in list(way_meta.keys()):
            if way_id in kolory_wayow:
                continue
            for sasiad_id in sasiedzi_przez_endpoint(way_id):
                if sasiad_id in kolory_wayow:
                    kolory_wayow[way_id] = kolory_wayow[sasiad_id]
                    zmieniono = True
                    break

    print(f"Po {iteracje} iteracjach flood fill: {len(kolory_wayow)} wayów z kolorem")

# ── Przebieg 2: rysuj wszystkie waye ──────────────────────────────────────────

print("Przebieg 2: rysowanie...")
for element in wszystkie:
    if element['type'] == 'way' and 'geometry' in element:
        highway   = element.get('tags', {}).get('highway', '')
        styl      = STYL.get(highway, {"color": "gray", "weight": 1, "grupa": "Pozostałe"})
        punkty    = [(p['lat'], p['lon']) for p in element['geometry']]
        punkty    = uprość_geometrie(punkty)
        way_id    = element.get('id')

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

        # Priorytet: 1) flood fill / bezpośrednie, 2) kolor relacji, 3) oryginalny
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
                weight_finalny = STALA_GRUBOSC  # stała grubość
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

# ── Warstwa WMS: granice TPN ───────────────────────────────────────────────────

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

# ── Warstwa: granice TANAP ─────────────────────────────────────────────────────

grupy["Granice TANAP"] = folium.FeatureGroup(name="Granice TANAP", show=True)

if obszar_tanap:
    folium.GeoJson(
        mapping(obszar_tanap),
        style_function=lambda x: {
            "color":       "darkgreen",
            "weight":      3,
            "opacity":     0.9,
            "fillColor":   "darkgreen",
            "fillOpacity": 0.1,
        },
        interactive=False
    ).add_to(grupy["Granice TANAP"])
else:
    for element in tanap_data["elements"]:
        if element["type"] == "relation" and "members" in element:
            for member in element["members"]:
                if member["type"] == "way" and "geometry" in member:
                    punkty = [(p["lat"], p["lon"]) for p in member["geometry"]]
                    folium.PolyLine(
                        punkty, color="darkgreen", weight=4, opacity=0.9
                    ).add_to(grupy["Granice TANAP"])

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

# ── Legenda natężenia ──────────────────────────────────────────────────────────

if strava_dostepna:
    legenda_html = """
    <div style="
        position: fixed; bottom: 40px; left: 10px; z-index: 1000;
        background: rgba(0,0,0,0.75); padding: 10px 14px;
        border-radius: 6px; color: white; font-size: 12px;
        font-family: monospace; border: 1px solid rgba(255,255,255,0.15);
    ">
        <b>Natężenie ruchu</b><br>
        <div style="
            width: 160px; height: 10px; margin: 6px 0 3px;
            background: linear-gradient(to right, #143cb4, #3c8cdc, #faf014, #fa6400, #e00000);
            border-radius: 3px;
        "></div>
        <div style="display:flex; justify-content:space-between; width:160px; font-size:10px; color:#aaa">
            <span>Niskie</span><span>Średnie</span><span>Wysokie</span>
        </div>
        <div style="margin-top:6px; font-size:10px; color:#aaa">
            Grubość linii = popularność<br>
            Szare = brak danych Strava
        </div>
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(legenda_html))

# ── Kontrolki ──────────────────────────────────────────────────────────────────

folium.LayerControl(collapsed=False).add_to(mapa)

folium.plugins.MousePosition(
    position="bottomleft",
    separator=" | ",
    prefix="Dł./Szer.:",
    num_digits=5
).add_to(mapa)

mapa.get_root().html.add_child(folium.Element("""
<style>
    #pomiar-btn {
        position: fixed; bottom: 30px; right: 10px; z-index: 1000;
        background: rgba(0,0,0,0.7); border: 1px solid rgba(255,255,255,0.3);
        border-radius: 4px; padding: 5px 8px; cursor: pointer;
        font-size: 13px; color: white;
    }
    #pomiar-btn.aktywny { background: #2a6; border-color: #4c8; }
    #pomiar-wynik {
        position: fixed; bottom: 60px; right: 10px; z-index: 1000;
        background: rgba(0,0,0,0.75); border: 1px solid rgba(255,255,255,0.2);
        border-radius: 4px; padding: 5px 10px; font-size: 13px;
        color: white; display: none;
    }
</style>
<button id="pomiar-btn" title="Zmierz odległość">📏 Pomiar</button>
<div id="pomiar-wynik"></div>
"""))

popupy_json = json.dumps(popupy_relacji)

mapa.get_root().script.add_child(folium.Element(f"""
    document.addEventListener("DOMContentLoaded", function() {{
        var popupy = {popupy_json};
        var aktywnaKlasa = null;
        var trybPomiaru  = false;
        var punktyPomiar = [];
        var liniePomiar  = [];
        var markerPomiar = [];
        var mapaL        = null;

        var panel = document.createElement('div');
        panel.id  = 'info-panel';
        panel.style.cssText = `
            position: fixed; top: 80px; left: 10px;
            background: rgba(0,0,0,0.85); color: white;
            padding: 10px 14px; border-radius: 6px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.5);
            z-index: 1000; max-width: 280px;
            font-size: 13px; display: none;
            border: 1px solid rgba(255,255,255,0.15);
            font-family: monospace;
        `;
        document.body.appendChild(panel);

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
                    if (aktywnaKlasa && aktywnaKlasa !== klasa) {{
                        podswietl(aktywnaKlasa, false);
                    }}
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

        setTimeout(function() {{
            mapaL = Object.values(window).find(v => v && v._leaflet_id && v.getCenter);
            if (!mapaL) return;

            function obliczDystans(p1, p2) {{
                var R    = 6371;
                var dLat = (p2.lat - p1.lat) * Math.PI / 180;
                var dLon = (p2.lng - p1.lng) * Math.PI / 180;
                var a    = Math.sin(dLat/2)*Math.sin(dLat/2) +
                           Math.cos(p1.lat*Math.PI/180)*Math.cos(p2.lat*Math.PI/180)*
                           Math.sin(dLon/2)*Math.sin(dLon/2);
                return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            }}

            function resetPomiar() {{
                liniePomiar.forEach(l => mapaL.removeLayer(l));
                markerPomiar.forEach(m => mapaL.removeLayer(m));
                liniePomiar = []; markerPomiar = []; punktyPomiar = [];
                document.getElementById('pomiar-wynik').style.display = 'none';
            }}

            document.getElementById('pomiar-btn').addEventListener('click', function() {{
                trybPomiaru = !trybPomiaru;
                this.classList.toggle('aktywny', trybPomiaru);
                this.textContent = trybPomiaru ? '✖ Zakończ pomiar' : '📏 Pomiar';
                if (!trybPomiaru) resetPomiar();
            }});

            mapaL.on('click', function(e) {{
                if (!trybPomiaru) return;
                punktyPomiar.push(e.latlng);
                var marker = L.circleMarker(e.latlng, {{
                    radius: 4, color: 'cyan', fillColor: 'cyan', fillOpacity: 1
                }}).addTo(mapaL);
                markerPomiar.push(marker);
                if (punktyPomiar.length > 1) {{
                    var p1  = punktyPomiar[punktyPomiar.length-2];
                    var p2  = punktyPomiar[punktyPomiar.length-1];
                    var lin = L.polyline([p1, p2], {{
                        color: 'cyan', weight: 2, opacity: 0.8, dashArray: '6,4'
                    }}).addTo(mapaL);
                    liniePomiar.push(lin);
                    var total = 0;
                    for (var i = 1; i < punktyPomiar.length; i++) {{
                        total += obliczDystans(punktyPomiar[i-1], punktyPomiar[i]);
                    }}
                    var wynik = document.getElementById('pomiar-wynik');
                    wynik.textContent = 'Dystans: ' + total.toFixed(2) + ' km';
                    wynik.style.display = 'block';
                }}
            }});
        }}, 2000);
    }});
"""))

mapa.save("index.html")
print("Gotowe! Zapisano index.html")
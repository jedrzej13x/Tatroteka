import requests
import folium
import folium.plugins
import time
import math
import json
from shapely.geometry import Point, LineString, MultiLineString, mapping
from shapely.ops import unary_union, linemerge, polygonize

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
                print(f"Błąd {response.status_code} lub pusta odpowiedź, próbuję kolejny serwer...")
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

    # Fallback na typ drogi
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

# ── Stałe ─────────────────────────────────────────────────────────────────────

BBOX = "(49.10, 19.60, 49.35, 20.25)"

STYL = {
    "path":        {"color": "red",     "weight": 2, "grupa": "Szlaki górskie"},
    "via_ferrata": {"color": "darkred", "weight": 3, "grupa": "Via ferraty"},
    "footway":     {"color": "blue",    "weight": 2, "grupa": "Drogi piesze"},
    "pedestrian":  {"color": "blue",    "weight": 2, "grupa": "Drogi piesze"},
    "steps":       {"color": "navy",    "weight": 2, "grupa": "Drogi piesze"},
    "track":       {"color": "green",   "weight": 2, "grupa": "Drogi leśne"},
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

# ── Budujemy poligony do filtrowania ──────────────────────────────────────────

obszar_tpn   = zbuduj_poligon(tpn_data)
obszar_tanap = zbuduj_poligon(tanap_data)

obszar_tpn_buf   = obszar_tpn.buffer(0.01)   if obszar_tpn   else None
obszar_tanap_buf = obszar_tanap.buffer(0.01) if obszar_tanap else None

print(f"TPN: {'OK' if obszar_tpn else 'BŁĄD'}, TANAP: {'OK' if obszar_tanap else 'BŁĄD'}")

# ── Długości i nazwy relacji (dopasowanie po id) ───────────────────────────────

relacje_dla_way = {}

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
        for wid in way_ids:
            relacje_dla_way[wid] = (nazwa_rel, dlugosc_rel, relacja_id)

# ── Mapa ───────────────────────────────────────────────────────────────────────

mapa = folium.Map(
    location=[49.23, 19.98],
    zoom_start=11,
    control_scale=True
)

grupy = {
    "Szlaki górskie": folium.FeatureGroup(name="Szlaki górskie", show=True),
    "Via ferraty":    folium.FeatureGroup(name="Via ferraty",    show=True),
    "Drogi piesze":   folium.FeatureGroup(name="Drogi piesze",   show=True),
    "Drogi leśne":    folium.FeatureGroup(name="Drogi leśne",    show=True),
    "Pozostałe":      folium.FeatureGroup(name="Pozostałe",      show=True),
}

popupy_relacji = {}
odfiltrowane   = 0

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

        kolor     = kolor_szlaku(element)
        typ_nazwa = nazwa_koloru(element)

        if way_id in relacje_dla_way:
            nazwa, dlugosc_total, relacja_id = relacje_dla_way[way_id]
            info_dlugosc = f"Długość całkowita: {dlugosc_total} km"
            klasa_css    = f"trasa-{relacja_id}"
        else:
            nazwa        = element.get('tags', {}).get('name', 'Brak nazwy')
            info_dlugosc = f"Długość odcinka: {oblicz_dlugosc(punkty)} km"
            klasa_css    = f"trasa-way-{way_id}"

        popup_tekst = f"<b>{nazwa}</b><br>Typ: {typ_nazwa}<br>{info_dlugosc}"

        linia = folium.PolyLine(
            punkty,
            color=kolor,
            weight=styl["weight"],
            opacity=0.6,
            tooltip=nazwa,
        )
        linia.options['className'] = klasa_css

        if klasa_css not in popupy_relacji:
            popupy_relacji[klasa_css] = popup_tekst

        grupy[styl["grupa"]].add_child(linia)

print(f"Odfiltrowano {odfiltrowane} elementów poza parkami")

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

# ── Warstwa: granice TANAP z wypełnieniem ─────────────────────────────────────

grupy["Granice TANAP"] = folium.FeatureGroup(name="Granice TANAP", show=True)

if obszar_tanap:
    folium.GeoJson(
        mapping(obszar_tanap),
        style_function=lambda x: {
            "color":       "darkgreen",
            "weight":      3,
            "opacity":     0.9,
            "fillColor":   "darkgreen",
            "fillOpacity": 0.15,
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

# ── Warstwa: oznakowane szlaki (Waymarked Trails) ─────────────────────────────

grupy["Szlaki oznakowane"] = folium.FeatureGroup(name="Szlaki oznakowane", show=False)

folium.TileLayer(
    tiles="https://tile.waymarkedtrails.org/hiking/{z}/{x}/{y}.png",
    attr="Waymarked Trails",
    name="Szlaki oznakowane",
    opacity=0.8,
    overlay=True,
).add_to(grupy["Szlaki oznakowane"])

grupy["Szlaki oznakowane"].add_to(mapa)

# ── Kontrolki ──────────────────────────────────────────────────────────────────

folium.LayerControl(collapsed=False).add_to(mapa)

folium.plugins.MousePosition(
    position="bottomleft",
    separator=" | ",
    prefix="Dł. geog./Szer. geog.:",
    num_digits=5
).add_to(mapa)

mapa.get_root().html.add_child(folium.Element("""
<style>
    #pomiar-btn {
        position: fixed;
        bottom: 30px;
        right: 10px;
        z-index: 1000;
        background: white;
        border: 2px solid rgba(0,0,0,0.3);
        border-radius: 4px;
        padding: 5px 8px;
        cursor: pointer;
        font-size: 13px;
    }
    #pomiar-btn.aktywny {
        background: #e8f4e8;
        border-color: #4a4;
    }
    #pomiar-wynik {
        position: fixed;
        bottom: 60px;
        right: 10px;
        z-index: 1000;
        background: white;
        border: 2px solid rgba(0,0,0,0.2);
        border-radius: 4px;
        padding: 5px 10px;
        font-size: 13px;
        display: none;
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
            position: fixed;
            top: 80px;
            left: 10px;
            background: white;
            padding: 10px 14px;
            border-radius: 6px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
            z-index: 1000;
            max-width: 260px;
            font-size: 13px;
            display: none;
        `;
        document.body.appendChild(panel);

        function podswietl(klasa, aktywny) {{
            document.querySelectorAll('path.' + klasa).forEach(function(el) {{
                el.style.opacity     = aktywny ? '1.0' : '0.6';
                el.style.strokeWidth = aktywny ? '5px' : '';
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
                        panel.innerHTML = popupy[klasa] + '<br><small style="color:#999">Kliknij mapę aby zamknąć</small>';
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
                var a    = Math.sin(dLat/2) * Math.sin(dLat/2) +
                           Math.cos(p1.lat * Math.PI / 180) * Math.cos(p2.lat * Math.PI / 180) *
                           Math.sin(dLon/2) * Math.sin(dLon/2);
                return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            }}

            function resetPomiar() {{
                liniePomiar.forEach(l => mapaL.removeLayer(l));
                markerPomiar.forEach(m => mapaL.removeLayer(m));
                liniePomiar  = [];
                markerPomiar = [];
                punktyPomiar = [];
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
                    radius: 4, color: 'purple', fillColor: 'purple', fillOpacity: 1
                }}).addTo(mapaL);
                markerPomiar.push(marker);

                if (punktyPomiar.length > 1) {{
                    var p1  = punktyPomiar[punktyPomiar.length - 2];
                    var p2  = punktyPomiar[punktyPomiar.length - 1];
                    var lin = L.polyline([p1, p2], {{
                        color: 'purple', weight: 3, opacity: 0.7, dashArray: '6,4'
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
print("Gotowe!")
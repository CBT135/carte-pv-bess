"""
Rapprochement registre ODRÉ ↔ OpenStreetMap pour récupérer l'exploitant (tags operator / owner)
et la position réelle des centrales (centre de l'emprise OSM).

Critères : même type (solaire / batterie), distance au centroïde de la commune, puissance
(plant:output:electricity) et similarité des noms. Une centrale OSM peut absorber plusieurs
lignes du registre (une centrale découpée en plusieurs points de livraison) tant que la somme
des puissances reste cohérente.
"""
import json
import math
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
QUERY = """
[out:json][timeout:900];
area["ISO3166-1"~"^(FR|GP|MQ|GF|RE|YT)$"]->.fr;
(
  nwr["power"="plant"]["plant:source"~"solar|battery"](area.fr);
  nwr["power"="generator"]["generator:source"~"solar|battery"]["generator:method"!="thermal"]["operator"](area.fr);
  nwr["power"="generator"]["generator:source"~"solar|battery"]["generator:output:electricity"~"MW"](area.fr);
);
out center tags;
"""

STOP = set("""centrale centrales solaire solaires photovoltaique photovoltaiques pv parc ferme champ champs
de du des la le les l d et en sur sas sarl sa snc sci centr adp stockage stock batterie batteries
bess energie energies station site projet poste installation installations n no n0 nord sud est ouest
au aux ombrieres ombriere toiture toitures sol grand grande petit petite""".split())


def _n(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tokens(s):
    return {t for t in _n(s).split() if len(t) > 2 and t not in STOP and not t.isdigit()
            and not re.match(r"^[a-z]{0,6}\d+$", t)}


def parse_mw(v):
    if not v:
        return None
    s = str(v).lower().replace(",", ".")
    m = re.search(r"([\d.]+)\s*(gw|mw|kw|w)", s)
    if not m:
        return None
    try:
        x = float(m.group(1))
    except ValueError:
        return None
    return x * {"gw": 1000, "mw": 1, "kw": 1e-3, "w": 1e-6}[m.group(2)]


def km(a, b, c, d):
    return math.hypot((c - a) * 111.2, (d - b) * 111.2 * math.cos(math.radians((a + c) / 2)))


def fetch_osm(cache: Path, timeout=1000):
    data = None
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    for url in OVERPASS:
        try:
            print(f"Overpass : {url}…", file=sys.stderr)
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "carte-pv-bess/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            if data.get("elements"):
                break
        except Exception as e:
            print(f"  échec ({e})", file=sys.stderr)
            data = None
    if data and data.get("elements"):
        items = []
        for el in data["elements"]:
            t = el.get("tags", {})
            c = el.get("center") or ({"lat": el["lat"], "lon": el["lon"]} if "lat" in el else None)
            if not c:
                continue
            src = t.get("plant:source") or t.get("generator:source") or ""
            items.append({
                "id": f'{el["type"][0]}{el["id"]}',
                "lat": round(c["lat"], 5), "lon": round(c["lon"], 5),
                "type": "BESS" if "battery" in src and "solar" not in src else "PV",
                "kind": t.get("power"),
                "name": t.get("name", ""),
                "operator": t.get("operator", ""),
                "owner": t.get("owner", ""),
                "mw": parse_mw(t.get("plant:output:electricity") or t.get("generator:output:electricity")),
            })
        cache.write_text(json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"  {len(items)} objets OSM (cache mis à jour)", file=sys.stderr)
        return items, "Overpass"
    if cache.exists():
        print("  Overpass indisponible : utilisation du cache", file=sys.stderr)
        return json.loads(cache.read_text(encoding="utf-8")), "cache"
    return [], "aucun"


def match(recs, osm, canon):
    """recs : installations (lat/lon = centroïde commune). Modifie recs en place. canon(str)->str."""
    grid = {}
    for i, o in enumerate(osm):
        o["_tok"] = tokens(o["name"]) | tokens(o["operator"])
        grid.setdefault((int(o["lat"] * 5), int(o["lon"] * 5)), []).append(i)

    pairs = []
    for ri, r in enumerate(recs):
        rt = tokens(r["nom"])
        ct = tokens(r["commune"])
        R = 12.0
        gy, gx = int(r["lat"] * 5), int(r["lon"] * 5)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for oi in grid.get((gy + dy, gx + dx), []):
                    o = osm[oi]
                    if o["type"] != r["type"]:
                        continue
                    d = km(r["lat"], r["lon"], o["lat"], o["lon"])
                    if d > R:
                        continue
                    jac = len(rt & o["_tok"]) / len(rt | o["_tok"]) if rt and o["_tok"] else 0.0
                    place = 1.0 if ct and ct <= o["_tok"] else 0.0
                    pr = None
                    if o["mw"] and r["mw"]:
                        pr = min(o["mw"], r["mw"]) / max(o["mw"], r["mw"])
                    # garde-fous
                    if pr is not None and pr < 0.35 and jac < 0.4:
                        continue
                    if pr is None and jac == 0 and place == 0 and d > 3:
                        continue
                    if o["kind"] == "generator" and pr is None and jac == 0:
                        continue
                    s = (1 - d / R) * 1.2 + 3 * jac + 0.6 * place + (2 * pr if pr is not None else 0.4)
                    if s >= 1.9:
                        pairs.append((s, ri, oi, pr))

    pairs.sort(key=lambda x: -x[0])
    used_mw = {}
    done = set()
    n_expl = n_loc = 0
    for s, ri, oi, pr in pairs:
        if ri in done:
            continue
        o, r = osm[oi], recs[ri]
        budget = (o["mw"] or 0) * 1.25
        if oi in used_mw:
            # centrale déjà attribuée : on n'accepte un second point de livraison que si la puissance reste cohérente
            if not o["mw"] or used_mw[oi] + r["mw"] > budget or s < 2.6:
                continue
        used_mw[oi] = used_mw.get(oi, 0) + r["mw"]
        done.add(ri)
        r["lat"], r["lon"] = o["lat"], o["lon"]
        r["loc_src"] = "osm"
        r["osm"] = o["id"]
        n_loc += 1
        op = o["operator"] or o["owner"]
        if op and r.get("expl_src") != "fichier":
            r["expl"] = canon(op) or op.strip()
            r["expl_src"] = "osm"
            n_expl += 1
    return n_loc, n_expl

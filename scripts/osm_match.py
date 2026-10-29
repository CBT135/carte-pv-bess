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
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# Requêtes découpées en tuiles (une requête nationale dépasse les limites des serveurs Overpass)
QUERY = """
[out:json][timeout:240][bbox:{bbox}];
(
  nwr["power"="plant"]["plant:source"~"solar|battery"];
  nwr["power"="generator"]["generator:source"~"solar|battery"]["operator"];
  nwr["power"="generator"]["generator:source"~"solar|battery"]["generator:output:electricity"~"MW"];
);
out center tags qt;
"""


def tiles():
    out = []
    lats = [41.3, 43.0, 44.5, 46.0, 47.5, 49.0, 51.2]
    lons = [-5.3, -1.5, 1.5, 4.5, 9.7]
    for a, b in zip(lats, lats[1:]):
        for c, d in zip(lons, lons[1:]):
            out.append((a, c, b, d))
    out += [(15.8, -61.9, 16.6, -60.9), (14.35, -61.3, 14.95, -60.75), (2.1, -54.7, 5.9, -51.5),
            (-21.45, 55.15, -20.8, 55.9), (-13.1, 44.95, -12.55, 45.35)]
    return out


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


def _query(bbox, timeout):
    body = urllib.parse.urlencode({"data": QUERY.replace("{bbox}", ",".join(map(str, bbox)))}).encode()
    last = None
    for url in OVERPASS:
        for attempt in range(1):
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "carte-pv-bess/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    j = json.loads(r.read())
                if "remark" in j and "runtime error" in j["remark"]:
                    raise RuntimeError(j["remark"][:120])
                return j.get("elements", [])
            except Exception as e:
                last = e
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Overpass indisponible ({last})")


def _item(el):
    t = el.get("tags", {})
    c = el.get("center") or ({"lat": el["lat"], "lon": el["lon"]} if "lat" in el else None)
    if not c:
        return None
    src = t.get("plant:source") or t.get("generator:source") or ""
    return {
        "id": f'{el["type"][0]}{el["id"]}',
        "lat": round(c["lat"], 5), "lon": round(c["lon"], 5),
        "type": "BESS" if "battery" in src and "solar" not in src else "PV",
        "kind": t.get("power"),
        "name": t.get("name", ""),
        "operator": t.get("operator", ""),
        "owner": t.get("owner", ""),
        "mw": parse_mw(t.get("plant:output:electricity") or t.get("generator:output:electricity")),
    }


def _tile_key(bb):
    return ",".join(str(x) for x in bb)


def fetch_osm(cache: Path, timeout=200, budget_s=900, max_age_days=14):
    """Extraction OSM par tuiles avec cache par tuile : chaque exécution rafraîchit d'abord les tuiles
    jamais extraites puis les plus anciennes, dans la limite du budget temps ; les autres viennent du cache."""
    today = time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - max_age_days * 86400))
    raw = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    tl = tiles()
    if isinstance(raw, list):  # ancien format : on répartit les objets dans leurs tuiles
        cached = {}
        for o in raw:
            for bb in tl:
                if bb[0] <= o["lat"] < bb[2] and bb[1] <= o["lon"] < bb[3]:
                    cached.setdefault(_tile_key(bb), {"d": today, "items": []})["items"].append(o)
                    break
    else:
        cached = raw.get("tiles", {})
    todo = [bb for bb in tl if _tile_key(bb) not in cached] + \
           sorted([bb for bb in tl if _tile_key(bb) in cached and cached[_tile_key(bb)]["d"] < cutoff],
                  key=lambda bb: cached[_tile_key(bb)]["d"])
    print(f"  Overpass : {len(tl)} tuiles, {len(todo)} à rafraîchir", file=sys.stderr)
    t0, done = time.time(), 0
    for bb in todo:
        if time.time() - t0 > budget_s:
            print("  Overpass : budget temps atteint, suite à la prochaine exécution", file=sys.stderr)
            break
        try:
            els = _query(bb, timeout)
            items = [x for x in (_item(el) for el in els) if x]
            cached[_tile_key(bb)] = {"d": today, "items": items}
            done += 1
            print(f"  Overpass tuile {_tile_key(bb)} : {len(items)} objets", file=sys.stderr)
        except Exception as e:
            print(f"  Overpass tuile {_tile_key(bb)} : échec ({e})", file=sys.stderr)
        time.sleep(2)
    if done:
        cache.write_text(json.dumps({"v": 2, "tiles": cached}, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    seen, items = set(), []
    for t in cached.values():
        for o in t["items"]:
            if o["id"] not in seen:
                seen.add(o["id"])
                items.append(o)
    missing = sum(1 for bb in tl if _tile_key(bb) not in cached)
    print(f"  OSM : {len(items)} objets ({done} tuiles rafraîchies, {missing} jamais extraites)", file=sys.stderr)
    return items, f"Overpass ({len(tl) - missing}/{len(tl)} tuiles)"


def match(recs, osm, canon, src="osm"):
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
        if r.get("loc_src") in (None, "commune"):
            r["lat"], r["lon"] = o["lat"], o["lon"]
            r["loc_src"] = src
            n_loc += 1
        r["osm" if src == "osm" else src] = o["id"]
        op = o["operator"] or o["owner"]
        if op and r.get("expl_src") != "fichier":
            r["expl"] = canon(op) or op.strip()
            r["expl_src"] = src
            n_expl += 1
    return n_loc, n_expl

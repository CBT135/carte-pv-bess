"""
Centrales solaires et de stockage situées en France décrites dans Wikidata avec un exploitant
(P137) ou un propriétaire (P127). Sert de complément au rapprochement OpenStreetMap.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from osm_match import parse_mw  # noqa: F401  (même format d'objets que OSM)

ENDPOINT = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "carte-pv-bess/1.0 (https://github.com/CBT135/carte-pv-bess)",
      "Accept": "application/sparql-results+json"}
QUERY = """
SELECT DISTINCT ?item ?itemLabel ?coord ?opLabel ?ownLabel ?w ?cLabel WHERE {
  ?c wdt:P279* wd:Q159719 .
  ?item wdt:P31 ?c ; wdt:P17 wd:Q142 ; wdt:P625 ?coord .
  OPTIONAL { ?item wdt:P137 ?op }
  OPTIONAL { ?item wdt:P127 ?own }
  FILTER(BOUND(?op) || BOUND(?own))
  OPTIONAL { ?item p:P2109/psn:P2109/wikibase:quantityAmount ?w }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "fr,en". }
}
"""
SOLAR = re.compile(r"solaire|solar|photovolta", re.I)
BATT = re.compile(r"batter|stockage|storage|accumul", re.I)
QID = re.compile(r"^Q\d+$")


def fetch(cache: Path, timeout=180):
    try:
        url = ENDPOINT + "?" + urllib.parse.urlencode({"query": QUERY, "format": "json"})
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            rows = json.loads(r.read())["results"]["bindings"]
    except Exception as e:
        print(f"  Wikidata indisponible ({e})", file=sys.stderr)
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8")), "cache"
        return [], "aucun"
    items = {}
    for b in rows:
        g = lambda k: b.get(k, {}).get("value", "")  # noqa: E731
        txt = g("itemLabel") + " " + g("cLabel")
        typ = "BESS" if BATT.search(txt) and not SOLAR.search(txt) else "PV" if SOLAR.search(txt) else None
        qid = g("item").rsplit("/", 1)[-1]
        if not typ:
            continue
        m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", g("coord"))
        op = next((x for x in (g("opLabel"), g("ownLabel")) if x and not QID.match(x)), "")
        if not m or not op:
            continue
        w = g("w")
        it = items.get(qid) or {
            "id": qid, "lat": round(float(m.group(2)), 5), "lon": round(float(m.group(1)), 5),
            "type": typ, "kind": "plant", "name": g("itemLabel"), "operator": op, "owner": "",
            "mw": round(float(w) / 1e6, 3) if w else None,
        }
        items[qid] = it
    out = list(items.values())
    cache.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  Wikidata : {len(out)} centrales solaires / stockage avec exploitant", file=sys.stderr)
    return out, "Wikidata"

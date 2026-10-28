"""
Rapprochement registre ODRÉ ↔ base SIRENE (API publique recherche-entreprises.api.gouv.fr).

Pour chaque commune portant au moins une installation, on liste les établissements actifs
dont l'activité est « Production d'électricité » (NAF 35.11Z) situés dans la commune.
On rattache ensuite chaque installation à la société la plus plausible (similarité de nom,
unicité dans la commune) et on remonte à l'exploitant via le dirigeant personne morale
(président de la SAS de projet = holding du développeur / exploitant).
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from osm_match import tokens

API = "https://recherche-entreprises.api.gouv.fr/search"
UA = {"User-Agent": "carte-pv-bess/1.0 (GitHub Actions)"}


def _get(params, tries=4):
    url = API + "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (k + 1))
                continue
            if e.code in (400, 404):
                return {"results": []}
            time.sleep(1)
        except Exception:
            time.sleep(1)
    raise RuntimeError("API recherche-entreprises indisponible")


def fetch_commune(insee, max_pages=4):
    out = []
    for page in range(1, max_pages + 1):
        j = _get({"activite_principale": "35.11Z", "code_commune": insee, "etat_administratif": "A",
                  "per_page": 25, "page": page})
        for e in j.get("results", []):
            etabs = [m for m in e.get("matching_etablissements") or []
                     if m.get("commune") == insee and m.get("etat_administratif", "A") == "A"]
            if not etabs:
                continue
            out.append({
                "siren": e.get("siren"),
                "nom": e.get("nom_raison_sociale") or e.get("nom_complet") or "",
                "sigle": e.get("sigle") or "",
                "enseignes": [x for m in etabs for x in (m.get("liste_enseignes") or [])],
                "siret": etabs[0].get("siret"),
                "dirigeants_pm": [d.get("denomination") for d in e.get("dirigeants") or []
                                  if d.get("type_dirigeant") == "personne morale" and d.get("denomination")],
                "siege_cp": (e.get("siege") or {}).get("code_postal"),
            })
        if page >= (j.get("total_pages") or 1):
            break
        time.sleep(0.16)
    return out


def fetch_all(communes, cache: Path, budget_s=1500):
    cache_d = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    res, ok, t0 = {}, 0, time.time()
    for i, c in enumerate(sorted(communes)):
        if time.time() - t0 > budget_s:
            print(f"  SIRENE : budget temps atteint après {i} communes, reste en cache", file=sys.stderr)
            res.update({k: cache_d[k] for k in communes if k not in res and k in cache_d})
            break
        try:
            res[c] = fetch_commune(c)
            ok += 1
        except Exception as e:
            if c in cache_d:
                res[c] = cache_d[c]
            if ok == 0 and i >= 5:  # API inaccessible : on se rabat entièrement sur le cache
                print(f"  SIRENE inaccessible ({e}), utilisation du cache", file=sys.stderr)
                res.update({k: cache_d[k] for k in communes if k in cache_d})
                break
        time.sleep(0.16)
    if ok:
        cache_d.update(res)
        cache.write_text(json.dumps(cache_d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  SIRENE : {ok} communes interrogées, {sum(len(v) for v in res.values())} sociétés 35.11Z", file=sys.stderr)
    return res, ok


def match(recs, by_commune, canon):
    """Ajoute rec['siren'], rec['societe'] et, si possible, l'exploitant (source 'sirene')."""
    per_commune = defaultdict(list)
    for r in recs:
        per_commune[r["insee"]].append(r)
    n = 0
    for insee, rs in per_commune.items():
        cands = by_commune.get(insee) or []
        if not cands:
            continue
        for c in cands:
            c["_tok"] = tokens(" ".join([c["nom"], c["sigle"], *c["enseignes"]]))
        commune_tok = tokens(rs[0]["commune"])
        for r in rs:
            rt = tokens(r["nom"])
            scored = []
            for c in cands:
                common = rt & c["_tok"]
                specific = common - commune_tok  # au-delà du seul nom de commune
                s = 2 * len(specific) + len(common)
                if canon(c["nom"]) or any(canon(d) for d in c["dirigeants_pm"]):
                    s += 0.5  # société rattachée à un acteur connu du PV
                scored.append((s, c))
            scored.sort(key=lambda x: -x[0])
            best, second = scored[0], (scored[1] if len(scored) > 1 else (-1, None))
            same_type = sum(1 for x in rs if x["type"] == r["type"])
            accept = (best[0] >= 2 and best[0] > second[0]) or \
                     (best[0] >= 1.5 and best[0] - second[0] >= 1) or \
                     (len(cands) == 1 and same_type <= 2 and best[0] >= 0.5) or \
                     (len(cands) == 1 and same_type == 1)
            if not accept:
                continue
            c = best[1]
            r["siren"], r["societe"] = c["siren"], c["nom"]
            if r.get("expl_src") in ("fichier",):
                continue
            group = next((canon(d) for d in c["dirigeants_pm"] if canon(d)), None) or canon(c["nom"])
            if group:
                expl = group
            elif r.get("expl_src") == "osm":
                continue  # on garde l'opérateur OSM s'il n'y a rien de mieux
            else:
                expl = (c["dirigeants_pm"][0] if c["dirigeants_pm"] else c["nom"]).strip()
            if expl:
                r["expl"], r["expl_src"] = expl, "sirene"
                n += 1
    return n

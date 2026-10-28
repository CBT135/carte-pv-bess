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
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from osm_match import tokens

API = "https://recherche-entreprises.api.gouv.fr/search"
UA = {"User-Agent": "carte-pv-bess/1.0 (GitHub Actions)"}


_lock = threading.Lock()
_next = [0.0]
RATE = 6.0  # requêtes / s (limite publique de l'API : 7)


def _throttle():
    with _lock:
        now = time.monotonic()
        wait = _next[0] - now
        _next[0] = max(now, _next[0]) + 1.0 / RATE
    if wait > 0:
        time.sleep(wait)


def _get(params, tries=5):
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for k in range(tries):
        _throttle()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                return {"results": []}
            time.sleep(2 * (k + 1) if e.code == 429 else 1)
        except Exception as e:
            last = e
            time.sleep(1)
    raise RuntimeError(f"API recherche-entreprises indisponible ({last})")


ROLE_OK = re.compile(r"pr[eé]sident|g[eé]rant|directeur g[eé]n[eé]ral|administrateur", re.I)
ROLE_KO = re.compile(r"commissaire|liquidateur|mandataire", re.I)


def _holdings(e):
    """Dirigeants personnes morales exerçant la direction (pas les commissaires aux comptes)."""
    out = []
    for d in e.get("dirigeants") or []:
        if d.get("type_dirigeant") != "personne morale" or not d.get("denomination"):
            continue
        q = d.get("qualite") or ""
        if ROLE_KO.search(q) or (q and not ROLE_OK.search(q)):
            continue
        out.append(d["denomination"])
    return out


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
                "dirigeants_pm": _holdings(e),
                "siege_cp": (e.get("siege") or {}).get("code_postal"),
            })
        if page >= (j.get("total_pages") or 1):
            break
    return out


CACHE_V = 2
MAX_AGE_DAYS = 45


def fetch_all(communes, cache: Path, budget_s=1500, workers=6):
    """Interroge les communes absentes ou périmées du cache (incrémental d'une exécution à l'autre)."""
    raw = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    cache_d = raw.get("communes", {}) if raw.get("v") == CACHE_V else {}
    today = time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - MAX_AGE_DAYS * 86400))
    todo = sorted(c for c in communes if c not in cache_d or cache_d[c]["d"] < cutoff)
    # les plus anciennes d'abord ; les nouvelles (jamais vues) en premier
    todo.sort(key=lambda c: cache_d.get(c, {}).get("d", ""))
    print(f"  SIRENE : {len(communes)} communes, {len(todo)} à interroger", file=sys.stderr)
    t0, ok, fails = time.time(), 0, 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {}
        it = iter(todo)
        def submit():
            if time.time() - t0 > budget_s or fails > 50:
                return False
            c = next(it, None)
            if c is None:
                return False
            futs[ex.submit(fetch_commune, c)] = c
            return True
        for _ in range(workers * 2):
            if not submit():
                break
        while futs:
            done, _ = wait(futs, return_when=FIRST_COMPLETED)
            for f in done:
                c = futs.pop(f)
                try:
                    cache_d[c] = {"d": today, "r": f.result()}
                    ok += 1
                except Exception as e:
                    fails += 1
                    if fails <= 3:
                        print(f"  SIRENE {c} : {e}", file=sys.stderr)
                submit()
            if ok and ok % 200 == 0:
                print(f"  SIRENE : {ok} communes ({int(time.time() - t0)} s)", file=sys.stderr)
    if ok:
        cache.write_text(json.dumps({"v": CACHE_V, "communes": cache_d}, ensure_ascii=False,
                                    separators=(",", ":")), encoding="utf-8")
    rest = sum(1 for c in communes if c not in cache_d)
    print(f"  SIRENE : {ok} communes interrogées en {int(time.time() - t0)} s, {fails} échecs, "
          f"{rest} encore jamais interrogées (prochaine exécution)", file=sys.stderr)
    return {c: cache_d[c]["r"] for c in communes if c in cache_d}, ok


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
                     (best[0] >= 1 and best[0] - second[0] >= 1) or \
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


# --------------------------------------------------------------------------- base SIRENE en bloc
SIRENE_DATASET = "https://www.data.gouv.fr/api/1/datasets/5b7ffc618b4c4169d30727e0/"


def _resource(meta, prefix):
    for r in meta.get("resources", []):
        t = (r.get("title") or "") + " " + (r.get("url") or "")
        if prefix in t and "parquet" in t.lower() and "Historique" not in t:
            return r["url"]
    return None


def load_stock(tmp: Path):
    """Établissements actifs NAF 35.11Z depuis le stock SIRENE (Parquet, data.gouv.fr), regroupés par commune."""
    import duckdb  # installé par le workflow
    meta = json.loads(urllib.request.urlopen(urllib.request.Request(SIRENE_DATASET, headers=UA), timeout=60).read())
    u_et, u_ul = _resource(meta, "StockEtablissement"), _resource(meta, "StockUniteLegale")
    if not (u_et and u_ul):
        raise RuntimeError("ressources Parquet SIRENE introuvables")
    tmp.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, u in (("et", u_et), ("ul", u_ul)):
        p = tmp / f"{name}.parquet"
        print(f"  téléchargement {u}", file=sys.stderr)
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=900) as r, p.open("wb") as fh:
            while True:
                b = r.read(1 << 22)
                if not b:
                    break
                fh.write(b)
        paths[name] = str(p)
    rows = duckdb.sql(f"""
        SELECT e.siret, e.siren, e.codeCommuneEtablissement AS insee,
               coalesce(e.enseigne1Etablissement, '') AS ens1,
               coalesce(e.denominationUsuelleEtablissement, '') AS ens2,
               coalesce(u.denominationUniteLegale,
                        trim(coalesce(u.prenom1UniteLegale,'') || ' ' || coalesce(u.nomUniteLegale,''))) AS nom,
               coalesce(u.sigleUniteLegale, '') AS sigle
        FROM read_parquet('{paths['et']}') e
        JOIN read_parquet('{paths['ul']}') u USING (siren)
        WHERE e.activitePrincipaleEtablissement = '35.11Z'
          AND e.etatAdministratifEtablissement = 'A'
          AND e.codeCommuneEtablissement IS NOT NULL
    """).fetchall()
    by = defaultdict(dict)
    for siret, siren, insee, e1, e2, nom, sigle in rows:
        c = by[insee].get(siren)
        if c:
            c["enseignes"] += [x for x in (e1, e2) if x]
            continue
        by[insee][siren] = {"siren": siren, "nom": nom or "", "sigle": sigle, "siret": siret,
                            "enseignes": [x for x in (e1, e2) if x], "dirigeants_pm": [], "siege_cp": None}
    for p in paths.values():
        Path(p).unlink(missing_ok=True)
    print(f"  SIRENE stock : {len(rows)} établissements 35.11Z actifs dans {len(by)} communes", file=sys.stderr)
    return {k: list(v.values()) for k, v in by.items()}


def enrich_holdings(recs, cache: Path, canon, budget_s=900, workers=6):
    """Pour les sociétés rattachées, récupère le président / gérant personne morale (API, cache incrémental)."""
    raw = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    cache_d = raw.get("siren", {}) if raw.get("v") == CACHE_V else {}
    today = time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - MAX_AGE_DAYS * 86400))
    sirens = sorted({r["siren"] for r in recs if r.get("siren")})
    todo = [s for s in sirens if s not in cache_d or cache_d[s]["d"] < cutoff]
    todo.sort(key=lambda s: cache_d.get(s, {}).get("d", ""))
    t0, ok, fails = time.time(), 0, 0

    def one(siren):
        j = _get({"q": siren, "per_page": 1})
        for e in j.get("results", []):
            if e.get("siren") == siren:
                return _holdings(e)
        return []

    with ThreadPoolExecutor(workers) as ex:
        futs, it = {}, iter(todo)

        def submit():
            if time.time() - t0 > budget_s or fails > 50:
                return
            s = next(it, None)
            if s:
                futs[ex.submit(one, s)] = s
        for _ in range(workers * 2):
            submit()
        while futs:
            done, _ = wait(futs, return_when=FIRST_COMPLETED)
            for f in done:
                s = futs.pop(f)
                try:
                    cache_d[s] = {"d": today, "h": f.result()}
                    ok += 1
                except Exception as e:
                    fails += 1
                    if fails <= 3:
                        print(f"  dirigeants {s} : {e}", file=sys.stderr)
                submit()
    if ok:
        cache.write_text(json.dumps({"v": CACHE_V, "siren": cache_d}, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    n = 0
    for r in recs:
        h = (cache_d.get(r.get("siren") or "") or {}).get("h") or []
        if not h or r.get("expl_src") == "fichier":
            continue
        group = next((canon(d) for d in h if canon(d)), None)
        if group:
            r["expl"], r["expl_src"] = group, "sirene"
            n += 1
        elif r.get("expl_src") == "sirene" and r.get("expl") == r.get("societe"):
            r["expl"] = h[0].strip()  # holding plutôt que la société de projet
            n += 1
    print(f"  dirigeants : {ok} SIREN interrogés en {int(time.time() - t0)} s ({fails} échecs), "
          f"{len(sirens) - sum(1 for s in sirens if s in cache_d)} restants ; {n} exploitants précisés", file=sys.stderr)
    return n

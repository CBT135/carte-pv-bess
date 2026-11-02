"""
Exploitants étrangers (Allemagne, Royaume-Uni, Pays-Bas, Luxembourg, Danemark…).

Quand la chaîne de présidents SIRENE aboutit à une société étrangère (GmbH, KG, B.V., Ltd, S.à r.l.
luxembourgeoise…), elle n'a pas de SIREN et la remontée s'arrête. On la retrouve alors dans le registre
mondial des identifiants d'entités juridiques (LEI) tenu par la GLEIF – données ouvertes, sans clé :
https://api.gleif.org – qui donne le pays et, pour beaucoup d'entités, la société mère directe et
la société mère ultime. Cache : data/gleif_cache.json (noms de sociétés uniquement).
"""
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.gleif.org/api/v1"
UA = {"User-Agent": "carte-pv-bess/1.0 (GitHub Actions)", "Accept": "application/vnd.api+json"}
PAYS = {"DE": "Allemagne", "GB": "Royaume-Uni", "NL": "Pays-Bas", "LU": "Luxembourg", "BE": "Belgique",
        "DK": "Danemark", "ES": "Espagne", "IT": "Italie", "CH": "Suisse", "AT": "Autriche", "SE": "Suède",
        "NO": "Norvège", "FI": "Finlande", "IE": "Irlande", "US": "États-Unis", "CA": "Canada", "PT": "Portugal",
        "JP": "Japon", "CN": "Chine", "AU": "Australie", "IL": "Israël", "SG": "Singapour", "KR": "Corée du Sud",
        "IN": "Inde", "AE": "Émirats arabes unis", "QA": "Qatar", "MA": "Maroc", "FR": "France", "MC": "Monaco",
        "GG": "Guernesey", "JE": "Jersey", "KY": "Îles Caïmans", "BM": "Bermudes", "PL": "Pologne"}

FOREIGN = re.compile(
    r"\b(GMBH|AG|KG(AA)?|UG|E\.?K\.?|LTD|LIMITED|LLP|PLC|B\.? ?V\.?|N\.? ?V\.?|A/S|APS|AB|ASA|OY|OYJ|S\.? ?L\.? ?U?\.?"
    r"|S\.? ?P\.? ?A\.?|S\.? ?R\.? ?L\.?|LLC|INC|L\.?P\.?|SCSP|SCA SICAV|BETEILIGUNGS\w*|VERWALTUNGS\w*)\b"
    r"|DROIT ETRANGER|LUXEMBOURG|\bRCS LUX", re.I)
FRENCH_FORM = re.compile(r"\b(SAS|SASU|SARL|EURL|SNC|SCI|SEM|SA)\b", re.I)


def _plain(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper()


def is_foreign(name):
    p = _plain(name)
    if not FOREIGN.search(p):
        return False
    # « SARL » française contenant « … SOLAR » ne doit pas passer : on exige une forme étrangère explicite
    if FRENCH_FORM.search(p) and not re.search(r"LUXEMBOURG|DROIT ETRANGER|S\.?A\.?R\.?L\.? ?\(RCS LUX|GMBH|KG|B\.? ?V\.?|LTD|LLP|PLC", p):
        return False
    return True


def clean(name):
    s = re.sub(r"\(.*?\)", " ", str(name))
    s = re.sub(r"-?\s*SOCI[EÉ]T[EÉ] DE DROIT [EÉ]TRANGER", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -,")


def key(name):
    p = _plain(clean(name))
    p = re.sub(r"\b(GMBH ?& ?CO\.? ?KG|GMBH|CO\.? ?KG|KG|AG|LTD|LIMITED|LLP|PLC|B\.? ?V\.?|N\.? ?V\.?|S\.? ?A\.? ?R\.? ?L\.?|"
               r"S\.? ?A\.?|A/S|APS|AB|OY|LLC|INC)\b", " ", p)
    return re.sub(r"[^A-Z0-9]", "", p)


def _get(path, params=None, tries=3):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1.5 * (k + 1))
        except Exception:
            time.sleep(1.5 * (k + 1))
    raise RuntimeError("GLEIF indisponible")


def _rec(d):
    if not d or not d.get("data"):
        return None
    x = d["data"][0] if isinstance(d["data"], list) else d["data"]
    a = x.get("attributes", {})
    ent = a.get("entity", {})
    return {"lei": x.get("id"), "nom": (ent.get("legalName") or {}).get("name"),
            "pays": (ent.get("legalAddress") or {}).get("country") or ent.get("jurisdiction", "")[:2]}


def lookup(name):
    """Entité GLEIF correspondant au nom (correspondance exacte du nom normalisé) + sociétés mères."""
    q = clean(name)
    found = None
    for params in ({"filter[entity.legalName]": q, "page[size]": 5},
                   {"filter[fulltext]": q, "page[size]": 10}):
        d = _get("/lei-records", params)
        time.sleep(0.3)
        for x in (d or {}).get("data", []):
            r = _rec({"data": x})
            if r and r["nom"] and key(r["nom"]) == key(q):
                found = r
                break
        if found:
            break
    if not found:
        return {"lei": None}
    out = dict(found)
    for rel in ("ultimate-parent", "direct-parent"):
        p = _rec(_get(f"/lei-records/{found['lei']}/{rel}"))
        time.sleep(0.3)
        if p and p["lei"] != found["lei"]:
            out["parent"], out["parent_pays"], out["parent_rel"] = p["nom"], p["pays"], rel
            break
    return out


def enrich(recs, cache: Path, canon, budget_s=400, max_age_days=90):
    raw = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    cache_d = raw.get("noms", {}) if raw.get("v") == 1 else {}
    today = time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - max_age_days * 86400))

    def last_foreign(r):
        for lab in reversed([r.get("expl") or ""] + (r.get("chaine") or "").split(" → ")):
            lab = lab.strip()
            if lab and is_foreign(lab):
                return lab
        return None

    targets = [(r, last_foreign(r)) for r in recs if r.get("expl_src") != "fichier"]
    targets = [(r, f) for r, f in targets if f]
    names = sorted({f for _, f in targets})
    todo = [n for n in names if n not in cache_d or cache_d[n]["d"] < cutoff]
    t0, ok = time.time(), 0
    for n in todo:
        if time.time() - t0 > budget_s:
            break
        try:
            cache_d[n] = {"d": today, **lookup(n)}
            ok += 1
        except Exception as e:
            print(f"  GLEIF {n} : {e}", file=sys.stderr)
            break
    if ok:
        cache.write_text(json.dumps({"v": 1, "noms": cache_d}, ensure_ascii=False, indent=0), encoding="utf-8")

    n_pays = n_grp = 0
    for r, f in targets:
        c = cache_d.get(f) or {}
        pays = c.get("pays") or ("LU" if "LUXEMBOURG" in _plain(f) else None)
        parent = c.get("parent")
        g = canon(parent or "") or canon(f) or (canon(r.get("expl") or ""))
        if g:
            if r.get("expl") != g:
                n_grp += 1
            r["expl"] = g
        elif parent:
            r["expl"] = parent
            n_grp += 1
        elif not canon(r.get("expl") or ""):
            r["expl"] = clean(f)
        ppays = c.get("parent_pays") if parent else pays
        if ppays and ppays != "FR":
            r["pays_expl"] = ppays
            n_pays += 1
        if parent:
            r["chaine"] = (r.get("chaine") or r.get("societe") or f) + f" → {parent}" + \
                (f" ({c.get('parent_pays')})" if c.get("parent_pays") else "")
    rest = sum(1 for n in names if n not in cache_d)
    print(f"  GLEIF : {len(names)} sociétés étrangères en sommet de chaîne, {ok} interrogées ({rest} restantes) ; "
          f"{n_pays} installations à exploitant étranger, {n_grp} rattachées à leur maison mère", file=sys.stderr)
    return n_pays

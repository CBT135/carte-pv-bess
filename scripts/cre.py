"""
Lauréats des appels d'offres de la CRE (open data « Liste des dossiers que la CRE propose de retenir »,
tous AO depuis 2017) rapprochés des installations du registre.

Le fichier ne donne ni commune ni coordonnées : le rapprochement se fait par le nom du projet
(souvent identique au nom de l'installation dans le registre, ex. « CRE4-1199 »), par les noms de
lieux contenus dans le projet ou dans la société candidate, et par la cohérence de la puissance.
La société candidate (société de projet) est ensuite cherchée dans SIRENE pour remonter à l'exploitant.
"""
import csv
import io
import json
import re
import sys
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from osm_match import tokens

URL = ("https://www.cre.fr/fileadmin/Documents/Open_data/Transition_energetique_et_appels_d_offres/"
       "Dossiers_a_retenir.csv")
UA = {"User-Agent": "carte-pv-bess/1.0 (GitHub Actions)"}
ZNI = {"Corse", "Guadeloupe", "Martinique", "Guyane", "La Réunion", "Mayotte"}
LEGAL = r"(?:SASU|SAS|SARL|EURL|SNC|SA|SCI|SEM|SEML|SCIC|SCA|SPV)"


def _up(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper().strip()


def compact(name):
    """Clé de comparaison d'une raison sociale : sans accents, sans forme juridique, sans ponctuation."""
    s = _up(name)
    s = re.sub(rf"^{LEGAL}\s+|\s+{LEGAL}$", "", s)
    return re.sub(r"[^A-Z0-9]", "", s)


def _num(v):
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def fetch(cache: Path):
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=120) as r:
            raw = r.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:  # fichier en Windows-1252 (ou mixte)
            text = raw.decode("cp1252", errors="replace")
        cache.write_text(text, encoding="utf-8")
        src = "CRE"
    except Exception as e:
        if not cache.exists():
            print(f"  CRE indisponible ({e})", file=sys.stderr)
            return [], "aucun"
        print(f"  CRE indisponible ({e}) : utilisation du cache", file=sys.stderr)
        text, src = cache.read_text(encoding="utf-8"), "cache"
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        fil = _up(r.get("FILIERE"))
        if "STOCKAGE" in fil or "BATTER" in fil:
            typ = "BESS"
        elif any(x in fil for x in ("PHOTOVOLTA", "SOLAIRE", "NEUTRE", "INNOVATION", "AUTOCONSO", "PV ")) \
                or fil.startswith("PV"):
            typ = "PV"
        else:
            continue
        mw = _num(r.get("PUISSANCE"))
        if not mw or not (r.get("NOM_PROJET") or r.get("CANDIDAT")):
            continue
        rows.append({"filiere": r.get("FILIERE", ""), "ao": r.get("AO", ""), "periode": r.get("PERIODE", ""),
                     "famille": r.get("CATEGORIE", ""), "projet": r.get("NOM_PROJET", ""),
                     "candidat": r.get("CANDIDAT", ""), "zni": r.get("TERRITOIRE_ZNI", ""),
                     "mw": mw, "type": typ})
    print(f"  CRE : {len(rows)} dossiers PV / stockage retenus ({src})", file=sys.stderr)
    return rows, src


def match(recs, rows, max_df=12):
    """Associe au plus un dossier CRE à chaque installation. Ajoute rec['ao'] et rec['candidat']."""
    by_name = defaultdict(list)
    df = Counter()
    rtok = []
    for i, r in enumerate(recs):
        t = tokens(r["nom"]) | tokens(r["commune"])
        rtok.append(t)
        df.update(t)
        by_name[compact(r["nom"])].append(i)
    index = defaultdict(list)
    for i, t in enumerate(rtok):
        for x in t:
            if len(x) >= 4 and df[x] <= max_df:
                index[x].append(i)

    pairs = []
    for j, c in enumerate(rows):
        zni = c["zni"] in ZNI or "ZNI" in _up(c["zni"])
        cands = {}
        # 1. nom de projet identique au nom de l'installation (ex. CRE4-1199)
        for i in by_name.get(compact(c["projet"]), []) if len(compact(c["projet"])) >= 5 else []:
            cands[i] = 6.0
        # 2. noms de lieux communs au projet / à la société candidate et à l'installation
        ct = tokens(c["projet"]) | tokens(c["candidat"])
        hits = Counter(i for x in ct if len(x) >= 4 and df[x] <= max_df for i in index.get(x, []))
        for i, h in hits.items():
            cands[i] = max(cands.get(i, 0), 2.0 * h)
        for i, s in cands.items():
            r = recs[i]
            if r["type"] != c["type"]:
                continue
            if c["zni"] and (r["region"] in ZNI) != zni:
                continue
            pr = min(r["mw"], c["mw"]) / max(r["mw"], c["mw"])
            if pr < 0.4 and s < 6:
                continue
            s += 3 * pr
            if s >= 4.6:
                pairs.append((s, i, j))
    per_i = defaultdict(list)
    for s, i, j in pairs:
        per_i[i].append((s, j))
    choice = []
    for i, lst in per_i.items():
        lst.sort(key=lambda x: -x[0])
        if len(lst) > 1 and lst[1][0] > lst[0][0] - 0.8:
            continue  # deux dossiers presque aussi plausibles : on s'abstient
        choice.append((lst[0][0], i, lst[0][1]))
    choice.sort(key=lambda x: -x[0])
    best_i, used_j = {}, set()
    for s, i, j in choice:  # un dossier ne sert qu'une fois (le plus plausible l'emporte)
        if j in used_j:
            continue
        used_j.add(j)
        best_i[i] = j
    n = 0
    for i, j in best_i.items():
        c, r = rows[j], recs[i]
        r["ao"] = " ".join(x for x in (c["ao"], c["periode"], c["famille"]) if x)
        r["candidat"] = c["candidat"]
        n += 1
    print(f"  CRE : {n} installations rattachées à un dossier lauréat", file=sys.stderr)
    return n

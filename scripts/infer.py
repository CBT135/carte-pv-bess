"""
Déduction de l'exploitant pour les installations encore non identifiées :
1. « site »  : autres tranches / points de livraison du même site (code RTE commun, ou même nom
   au numéro de tranche près, dans la même commune) dont l'exploitant est connu et unique ;
2. « motif » : codification de nom propre à un développeur (ex. « NWJ MET … », « LAVANSOL … »),
   apprise sur les installations identifiées (au moins 4 exemples, 85 % du même exploitant).
"""
import re, unicodedata
from collections import Counter, defaultdict

def _up(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper().strip()

GENERIC = set("""CENTRALE CENTRALES SOLAIRE SOLAIRES PHOTOVOLTAIQUE PHOTOVOLTAIQUES PARC FERME CHAMP PV CS DE DU DES LA LE LES L D
SAS SARL SA SNC SCI ET EN SUR AU AUX TRANCHE NORD SUD EST OUEST BIS TER PROJET SITE PROD CENTRAL OMBRIERE OMBRIERES TOITURE
STOCKAGE N0 NO ADP CTRL INSTALLATION ENERGIE ENERGIES""".split())

def site_key(r):
    """Clé « même site » : code RTE (JAMETV04 → JAMET) ou nom sans numéro de tranche, dans la même commune."""
    n = _up(r["nom"])
    m = re.match(r"^([A-Z.]{3,6})[A-Z0-9]?[VS]\d{2}\b", n)
    if m:
        return ("rte", m.group(1), r["insee"])
    toks = [t for t in re.split(r"[^A-Z0-9]+", n) if t]
    base = [t for t in toks if not re.fullmatch(r"\d+|[A-Z]?\d+[A-Z]?|I{1,3}|IV|V|[A-H]", t) and t not in
            ("TRANCHE", "NORD", "SUD", "EST", "OUEST", "BIS", "TER", "PDL", "EXTENSION", "EXT")]
    strong = [t for t in base if t not in GENERIC and len(t) >= 3]
    if not strong or n == "CONFIDENTIEL" or n in ("PRIVE",):
        return None
    return ("nom", " ".join(base), r["insee"])

def signature(nom):
    n = _up(nom)
    if not n or n in ("CONFIDENTIEL", "PRIVE"):
        return []
    first = re.split(r"\s+", n)[0]
    sigs = []
    if re.search(r"\d", first) and re.search(r"[A-Z]", first):
        sigs.append("T:" + re.sub(r"\d+", "#", first))          # RS#, C#, CEN#P#, MTSFR-MONZ#
    m = re.match(r"^([A-Z][A-Z'.]+(?:\s+[A-Z][A-Z'.]+)?)", n)
    if m:
        w = m.group(1).split()
        if not all(x in GENERIC for x in w):
            sigs.append("W:" + " ".join(w))                   # SOLAIRE PARC, LAVANSOL…
    return sigs

def infer(recs, canon, min_n=4, purity=0.85):
    lab = lambda r: (canon(r["expl"]) or r["expl"]) if r["expl"] else None
    # 1. autres tranches / points de livraison du même site
    groups = defaultdict(list)
    for r in recs:
        k = site_key(r)
        if k: groups[k].append(r)
    n1 = 0
    for k, rs in groups.items():
        labs = Counter(lab(r) for r in rs if r["expl"])
        if not labs or len(labs) > 1: continue
        e = next(iter(labs))
        for r in rs:
            if not r["expl"]:
                r["expl"], r["expl_src"] = e, "site"
                n1 += 1
    # 2. codification des noms propre à un développeur
    by_sig = defaultdict(Counter)
    for r in recs:
        if r["expl"] and r["expl_src"] not in ("site", "motif"):
            for s in signature(r["nom"]): by_sig[s][lab(r)] += 1
    rules = {}
    for s, c in by_sig.items():
        tot = sum(c.values()); e, n = c.most_common(1)[0]
        if n >= min_n and n / tot >= purity: rules[s] = (e, n, tot)
    n2 = 0
    for r in recs:
        if r["expl"]: continue
        for s in signature(r["nom"]):
            if s in rules:
                r["expl"], r["expl_src"] = rules[s][0], "motif"
                n2 += 1
                break
    return n1, n2, rules

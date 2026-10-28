#!/usr/bin/env python3
"""
Construit data/installations.json : installations PV et BESS > 1 MW en service en France,
à partir du Registre national des installations de production et de stockage d'électricité
(ODRÉ – RTE, Enedis, EDF SEI, ELD), géolocalisées au centroïde de leur commune.

Usage :
    python scripts/build_data.py                      # téléchargement ODRÉ + geo.api.gouv.fr
    python scripts/build_data.py --registre reg.csv   # à partir d'un CSV déjà téléchargé
    python scripts/build_data.py --seuil-kw 1000      # seuil de puissance (kW, strict)

Aucune dépendance hors bibliothèque standard Python 3.9+.
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import re
import sys
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import osm_match  # noqa: E402
import sirene_match  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ODRE_DATASET = "registre-national-installation-production-stockage-electricite-agrege"
ODRE_EXPORT = (
    "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    f"{ODRE_DATASET}/exports/csv?delimiter=%3B&use_labels=false"
)
DATAGOUV_API = "https://www.data.gouv.fr/api/1/datasets/5bfcc5a006e3e744e304ccf0/"
GEO_URLS = [
    "https://geo.api.gouv.fr/communes?fields=code,nom,centre&format=json",
    "https://geo.api.gouv.fr/communes?type=arrondissement-municipal&fields=code,nom,centre&format=json",
    "https://geo.api.gouv.fr/communes?type=commune-deleguee&fields=code,nom,centre&format=json",
    "https://geo.api.gouv.fr/communes?type=commune-associee&fields=code,nom,centre&format=json",
]
UA = {"User-Agent": "carte-pv-bess/1.0 (GitHub Actions)"}

# --------------------------------------------------------------------------- utilitaires

def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def up(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper().strip()


def fetch(url, timeout=300):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def num(v):
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def date_iso(v):
    s = str(v or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


class Row:
    """Accès tolérant aux colonnes (casse, accents, underscores ignorés)."""

    def __init__(self, raw):
        self.d = {norm(k): v for k, v in raw.items() if k is not None}

    def get(self, *names):
        for n in names:
            v = self.d.get(norm(n))
            if v not in (None, ""):
                return v
        return None


# --------------------------------------------------------------------------- sources

def load_registre(path=None):
    if path:
        raw = Path(path).read_bytes()
        source = f"fichier local {Path(path).name}"
    else:
        try:
            print("Téléchargement du registre ODRÉ…", file=sys.stderr)
            raw = fetch(ODRE_EXPORT)
            source = "ODRÉ – odre.opendatasoft.com (" + ODRE_DATASET + ")"
        except Exception as e:  # repli data.gouv.fr
            print(f"  ODRÉ indisponible ({e}), repli sur data.gouv.fr…", file=sys.stderr)
            meta = json.loads(fetch(DATAGOUV_API))
            csvs = [r for r in meta.get("resources", []) if (r.get("format") or "").lower() == "csv"]
            csvs.sort(key=lambda r: r.get("last_modified") or "", reverse=True)
            if not csvs:
                raise SystemExit("Aucune ressource CSV trouvée sur data.gouv.fr")
            raw = fetch(csvs[0]["url"])
            source = "data.gouv.fr – " + meta.get("title", "Registre national")
    text = raw.decode("utf-8-sig", errors="replace")
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") >= first.count(",") else ","
    rows = [Row(r) for r in csv.DictReader(io.StringIO(text), delimiter=delim)]
    print(f"  {len(rows)} lignes lues ({source})", file=sys.stderr)
    return rows, source


def load_communes(path=None):
    """Retourne {code INSEE: (lat, lon)}."""
    if path:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        items = []
        for u in GEO_URLS:
            try:
                items += json.loads(fetch(u, timeout=120))
            except Exception as e:
                print(f"  geo.api.gouv.fr : {u} indisponible ({e})", file=sys.stderr)
    out = {}
    for c in items:
        ctr = (c.get("centre") or {}).get("coordinates")
        if c.get("code") and ctr:
            out.setdefault(c["code"], (round(ctr[1], 5), round(ctr[0], 5)))
    print(f"  {len(out)} centroïdes de communes", file=sys.stderr)
    return out


# --------------------------------------------------------------------------- exploitants

# Motifs reconnus dans le nom d'installation (indicatif : marqué « déduit du nom »).
OPERATEURS = [
    (r"\bTOTAL ?ENERGIES\b|\bTOTAL QUADRAN\b|\bQUADRAN\b|\bTOTAL SOLAR\b", "TotalEnergies"),
    (r"\bEDF ?(RENOUVELABLES|EN|ENR|R)\b|\bEDF SOLAIRE\b|\bEDF POWER SOLUTIONS\b", "EDF Renouvelables"),
    (r"\bNW ?JOULES\b|\bNWJ\b|\bNW GROUPE\b", "NW Groupe"),
    (r"\bCNR\b|COMPAGNIE NATIONALE DU RHONE", "CNR"),
    (r"\bENGIE\b|\bGREEN ?YELLOW\b", "ENGIE"),
    (r"\bURBA ?\d+|\bURBASOLAR\b", "Urbasolar"),
    (r"\bNEOEN\b", "Neoen"),
    (r"\bVALECO\b", "Valeco"),
    (r"\bBORALEX\b", "Boralex"),
    (r"\bAKUO\b", "Akuo"),
    (r"\bVOLTALIA\b", "Voltalia"),
    (r"\bARKOLIA\b", "Arkolia"),
    (r"\bAMARENCO\b", "Amarenco"),
    (r"\bLUXEL\b", "Luxel"),
    (r"\bTECHNIQUE SOLAIRE\b|\bTS ?\d+\b", "Technique Solaire"),
    (r"\bPHOTOSOL\b", "Photosol"),
    (r"\bQ ?ENERGY\b", "Q ENERGY"),
    (r"\bREDEN\b", "Reden Solar"),
    (r"\bSONNEDIX\b", "Sonnedix"),
    (r"\bGENERALE DU SOLAIRE\b|\bGDSOL\b", "Générale du Solaire"),
    (r"\bIBERDROLA\b", "Iberdrola"),
    (r"\bBAYWA\b", "BayWa r.e."),
    (r"\bCOROSOL\b|\bCORSICA SOLE\b", "Corsica Sole"),
    (r"\bJPEE\b", "JPee"),
    (r"\bCVE\b", "CVE"),
    (r"\bELEMENTS\b", "Elements"),
    (r"\bLANGA\b", "Langa"),
    (r"\bIEL\b", "IEL"),
    (r"\bTENERGIE\b", "Tenergie"),
    (r"\bENOE\b", "Enoé"),
    (r"\bAPEX\b", "Apex Energies"),
    (r"\bHARMONY\b", "Harmony Energy"),
    (r"\bTAGENERGY\b|\bTAG ENERGY\b", "TagEnergy"),
    (r"\bENERCOOP\b", "Enercoop"),
    (r"\bTERRE ET LAC\b", "Terre et Lac"),
    (r"\bGAZEL\b", "GazelEnergie"),
    (r"\bSOLVEO\b", "Solveo"),
    (r"\bIREL\b|\bIRISOLARIS\b|\bIRISOLAR\b", "Irisolaris"),
    (r"\bVALOREM\b", "Valorem"),
    (r"\bABO ?(WIND|ENERGY)\b", "ABO Energy"),
    (r"\bWPD\b", "wpd"),
    (r"\bVSB\b", "VSB"),
    (r"\bOSTWIND\b", "Ostwind"),
    (r"\bSERGIES\b", "Sergies"),
    (r"\bCOMPAGNIE DU VENT\b", "Engie (Compagnie du Vent)"),
    (r"\bSUNTI?ENERGIE\b|\bSUNTI\b", "Sunti"),
    (r"\bRUBIS\b", "Rubis"),
    (r"\bALBIOMA\b", "Albioma"),
    (r"\bENERPARC\b", "Enerparc"),
    (r"\bLIGHTSOURCE\b", "Lightsource bp"),
    (r"\bENGIE GREEN\b|\bENGIE PV\b", "ENGIE"),
]
OPERATEURS = [(re.compile(p), lab) for p, lab in OPERATEURS]


def load_overrides():
    """data/exploitants.csv : cle;exploitant — cle = code EIC, nom exact, ou re:<regex>."""
    f = DATA / "exploitants.csv"
    exact, regex = {}, []
    if not f.exists():
        return exact, regex
    with f.open(encoding="utf-8-sig") as fh:
        for r in csv.reader(fh, delimiter=";"):
            if len(r) < 2 or not r[0].strip() or r[0].startswith("#") or r[0].strip().lower() == "cle":
                continue
            k, v = r[0].strip(), r[1].strip()
            if k.lower().startswith("re:"):
                regex.append((re.compile(k[3:], re.I), v))
            else:
                exact[up(k)] = v
    return exact, regex


def canon(op):
    """Ramène un libellé d'opérateur (ex. tag OSM) à un groupe connu, sinon None."""
    n = up(op)
    for rx, v in OPERATEURS:
        if rx.search(n):
            return v
    return None


def exploitant(eic, nom, overrides):
    exact, regex = overrides
    for k in (eic, nom):
        if k and up(k) in exact:
            return exact[up(k)], "fichier"
    for rx, v in regex:
        if nom and rx.search(nom):
            return v, "fichier"
    n = up(nom)
    for rx, v in OPERATEURS:
        if rx.search(n):
            return v, "nom"
    return None, None


# --------------------------------------------------------------------------- traitement

def classify(filiere, techno):
    f, t = up(filiere), up(techno)
    if "SOLAIRE" in f and "THERMO" not in t:
        return "PV"
    if "STOCKAGE" in f and "HYDRAU" not in f:
        return "BESS"
    if "BATTERIE" in t:
        return "BESS"
    if "PHOTOVOLTA" in t:
        return "PV"
    return None


def jitter(points):
    """Écarte en spirale les installations d'une même commune (≈ 500 m par cran)."""
    by = defaultdict(list)
    for p in points:
        by[(p["lat"], p["lon"])].append(p)
    golden = math.pi * (3 - math.sqrt(5))
    for (lat, lon), grp in by.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda p: -p["mw"])
        coslat = max(math.cos(math.radians(lat)), 0.2)
        for i, p in enumerate(grp):
            if i == 0:
                continue
            r = 0.0045 * math.sqrt(i)
            a = i * golden
            p["lat"] = round(lat + r * math.sin(a), 5)
            p["lon"] = round(lon + r * math.cos(a) / coslat, 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registre", help="CSV du registre déjà téléchargé")
    ap.add_argument("--communes", help="JSON geo.api.gouv.fr déjà téléchargé")
    ap.add_argument("--seuil-kw", type=float, default=1000.0)
    ap.add_argument("--out", default=str(DATA / "installations.json"))
    ap.add_argument("--tmp", default="/tmp/sirene", help="dossier temporaire pour le stock SIRENE")
    ap.add_argument("--no-sirene", action="store_true", help="ne pas interroger la base SIRENE")
    ap.add_argument("--no-osm", action="store_true", help="ne pas interroger OpenStreetMap")
    a = ap.parse_args()

    rows, source = load_registre(a.registre)
    centres = load_communes(a.communes)
    overrides = load_overrides()
    today = dt.date.today().isoformat()

    excl = Counter()
    best = {}
    for r in rows:
        typ = classify(r.get("filiere"), r.get("technologie"))
        if not typ:
            continue
        nb = num(r.get("nbInstallation", "nbInstallations"))
        nom = (r.get("nomInstallation") or "").strip()
        if (nb and nb > 1) or re.search(r"agr[eé]g", nom, re.I):
            excl["agrégats < 36 kW"] += 1
            continue
        kw = num(r.get("puisMaxInstallee")) or 0
        if typ == "BESS":  # stockage : la puissance de décharge peut être saisie côté raccordement / charge
            kw = max(kw, num(r.get("puisMaxRac")) or 0, num(r.get("puisMaxCharge")) or 0)
        elif not kw:
            kw = num(r.get("puisMaxRac")) or 0
        if kw <= a.seuil_kw:
            excl["≤ seuil"] += 1
            continue
        if date_iso(r.get("dateDeraccordement")):
            excl["déraccordées"] += 1
            continue
        regime = up(r.get("regime"))
        if regime and "SERVICE" not in regime:
            excl["hors service / retrait provisoire"] += 1
            continue
        dmes = date_iso(r.get("dateMiseEnService"))
        drac = date_iso(r.get("dateRaccordement"))
        if not ((dmes and dmes <= today) or (drac and drac <= today)):
            excl["pas encore mises en service"] += 1
            continue

        eic = (r.get("codeEICResourceObject") or "").strip()
        insee = (r.get("codeINSEECommune") or "").strip().zfill(5) if r.get("codeINSEECommune") else ""
        expl, expl_src = exploitant(eic, nom, overrides)
        rec = {
            "nom": nom or "Non renseigné",
            "type": typ,
            "techno": (r.get("technologie") or "").strip().capitalize(),
            "mw": round(kw / 1000, 3),
            "mw_charge": round((num(r.get("puisMaxCharge")) or 0) / 1000, 3) if typ == "BESS" else None,
            "mes": dmes or drac,
            "mes_src": "MES" if dmes else "raccordement",
            "expl": expl,
            "expl_src": expl_src,
            "commune": (r.get("commune") or "").strip(),
            "insee": insee,
            "dep": (r.get("codeDepartement") or "").strip(),
            "region": (r.get("region") or "").strip() or "Non renseignée",
            "grd": (r.get("gestionnaire", "codeGestionnaire") or "").strip(),
            "tension": (r.get("tensionRaccordement") or "").strip(),
            "poste": (r.get("posteSource") or "").strip(),
            "eic": eic,
            "_v": date_iso(r.get("dateDebutVersion")) or "",
        }
        key = eic or f"{nom}|{insee}|{typ}"
        if key in best and best[key]["_v"] >= rec["_v"]:
            excl["doublons (versions)"] += 1
            continue
        best[key] = rec

    pts, sans_coord = [], []
    for rec in best.values():
        rec.pop("_v", None)
        c = centres.get(rec["insee"])
        if not c:
            sans_coord.append(rec)
            continue
        rec["lat"], rec["lon"] = c
        rec["loc_src"] = "commune"
        rec["osm"] = None
        rec["siren"] = rec["societe"] = None
        pts.append(rec)

    osm_stats = {}
    if not a.no_osm:
        osm, osm_src = osm_match.fetch_osm(DATA / "osm_plants.json")
        n_loc, n_expl = osm_match.match(pts, osm, canon)
        osm_stats = {"source": osm_src, "objets": len(osm), "localisees": n_loc, "exploitants": n_expl}
        print(f"  OSM : {n_loc} installations rapprochées, {n_expl} exploitants récupérés", file=sys.stderr)
    sirene_stats = {}
    if not a.no_sirene:
        print("SIRENE : établissements de production d'électricité (NAF 35.11Z)…", file=sys.stderr)
        try:
            by_c, mode = sirene_match.load_stock(Path(a.tmp)), "stock Parquet"
        except Exception as e:
            print(f"  stock SIRENE indisponible ({e}) : repli sur l'API par commune", file=sys.stderr)
            by_c, _ = sirene_match.fetch_all({p["insee"] for p in pts}, DATA / "sirene_cache.json")
            mode = "API"
        sirene_match.match(pts, by_c, canon)
        n_h = sirene_match.enrich_holdings(pts, DATA / "sirene_dirigeants.json", canon)
        sirene_stats = {"mode": mode, "societes_rattachees": sum(1 for p in pts if p["siren"]),
                        "exploitants": sum(1 for p in pts if p["expl_src"] == "sirene"), "holdings": n_h}
        print(f"  SIRENE : {sirene_stats}", file=sys.stderr)
    jitter([p for p in pts if p["loc_src"] == "commune"])
    pts.sort(key=lambda p: -p["mw"])

    fields = ["nom", "type", "techno", "mw", "mw_charge", "mes", "mes_src", "expl", "expl_src",
              "commune", "insee", "dep", "region", "grd", "tension", "poste", "eic", "lat", "lon", "loc_src", "osm", "siren", "societe"]
    out = {
        "meta": {
            "source": source,
            "genere_le": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "seuil_kw": a.seuil_kw,
            "n": len(pts),
            "mw_pv": round(sum(p["mw"] for p in pts if p["type"] == "PV"), 1),
            "mw_bess": round(sum(p["mw"] for p in pts if p["type"] == "BESS"), 1),
            "exclusions": dict(excl),
            "sans_coordonnees": len(sans_coord),
            "osm": osm_stats,
            "sirene": sirene_stats,
            "exploitant_identifie_mw": round(sum(p["mw"] for p in pts if p["expl"]), 1),
            "localisation": "centroïde de la commune du point de livraison (le registre ne publie pas de coordonnées)",
        },
        "fields": fields,
        "rows": [[p.get(f) for f in fields] for p in pts],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if sans_coord:
        with (Path(a.out).parent / "sans_coordonnees.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["nom", "type", "mw", "insee", "commune", "region"])
            for p in sans_coord:
                w.writerow([p["nom"], p["type"], p["mw"], p["insee"], p["commune"], p["region"]])
    m = out["meta"]
    print(f"OK : {m['n']} installations ({m['mw_pv']} MW PV, {m['mw_bess']} MW BESS), "
          f"{len(sans_coord)} sans coordonnées, exclusions {dict(excl)}", file=sys.stderr)


if __name__ == "__main__":
    main()

# Carte des centrales PV & BESS > 1 MW en service en France

Carte web (GitHub Pages) de toutes les installations **photovoltaïques** et de **stockage par batteries** de plus de 1 MW raccordées et en service en France (métropole et DROM), tous gestionnaires de réseau confondus (RTE, Enedis, EDF SEI, ELD).

- Point orange = PV, point bleu-violet = BESS ; diamètre proportionnel à √P (plafonné, ajusté au zoom).
- Info-bulle au survol (clic sur mobile) : nom, puissance, date de mise en service, exploitant, commune, gestionnaire et tension de raccordement.
- Filtres : type, région, exploitant, plage de puissance, année de mise en service, recherche texte. Les filtres sont conservés dans l'URL (lien partageable).
- Export CSV de la sélection.

## Source des données

**Registre national des installations de production et de stockage d'électricité** publié par ODRÉ (Open Data Réseaux Énergies) — jeu `registre-national-installation-production-stockage-electricite-agrege`, mis à jour semestriellement. Repli automatique sur la copie data.gouv.fr si l'API ODRÉ ne répond pas.

Règles appliquées par `scripts/build_data.py` :

| Critère | Règle |
|---|---|
| Type | filière *Solaire* (hors thermodynamique) → PV ; filière *Stockage hors hydraulique* → BESS |
| Puissance | `puisMaxInstallee` > 1 000 kW (pour le stockage, max de la puissance installée / de raccordement / de charge) |
| En service | date de mise en service (ou à défaut de raccordement) passée, pas de date de déraccordement, régime « en service » |
| Agrégats | lignes agrégées (< 36 kW, `nbInstallation` > 1) exclues |
| Doublons | une ligne par code EIC (version la plus récente) |

### Limites à connaître

- **Localisation** : le registre ne publie pas de coordonnées. Chaque installation est placée au centroïde de sa commune (API geo.api.gouv.fr) ; plusieurs installations d'une même commune sont écartées en spirale (~500 m) pour rester survolables.
- **Exploitant** : le registre ne publie pas le propriétaire/exploitant. Il est renseigné :
  1. par `data/exploitants.csv` (prioritaire — code EIC, nom exact ou `re:` + expression régulière) ;
  2. sinon déduit du nom de l'installation quand il contient un développeur connu (ex. « URBA 123 » → Urbasolar), signalé « déduit du nom » dans l'info-bulle ;
  3. sinon « Non identifié ».
- Certaines installations ont un nom « Confidentiel ».

## Mise en route

1. Créer le dépôt GitHub et y pousser ces fichiers (branche `main`).
2. **Settings → Pages** : *Deploy from a branch*, `main`, dossier `/ (root)`.
3. **Settings → Actions → General → Workflow permissions** : *Read and write permissions*.
4. **Actions → Mise à jour des données → Run workflow** : génère `data/installations.json` (≈ 1 min), puis automatiquement chaque lundi.

En local : `python scripts/build_data.py` puis `python -m http.server` et ouvrir http://localhost:8000.

## Arborescence

```
index.html                     carte (Leaflet, sans build)
scripts/build_data.py          collecte + filtrage + géolocalisation (Python, bibliothèque standard)
data/installations.json        données générées
data/exploitants.csv           correspondances exploitant (à enrichir)
data/sans_coordonnees.csv      installations non géolocalisées (si besoin)
.github/workflows/update-data.yml
```

Licence des données : Licence Ouverte 2.0 (ODRÉ). Fond de carte © OpenStreetMap, © CARTO.

# FormationLap-data

Relais de données de l'app Formation Lap. Une tâche planifiée récupère les données F1 chez [jolpica-f1](https://github.com/jolpica/jolpica-f1) et les publie en fichiers JSON statiques sur Cloudflare Pages. Les apps lisent ces fichiers au lieu d'appeler jolpica : la charge sur jolpica reste la même quel que soit le nombre d'utilisateurs.

## Fonctionnement

- `relay/update.py` décide à chaque passage quels fichiers rafraîchir :
  - souvent dans les 6 heures qui suivent une séance ;
  - toutes les 2 heures pendant les 3 jours suivants, le temps des décisions des commissaires ;
  - une fois par jour le reste du temps.

  Les résultats des manches passées ne sont revérifiés qu'une fois par semaine, et la saison précédente toutes les deux semaines.
- Quotas jolpica : 4 requêtes par seconde et 500 par heure. Le script se limite à environ 3 requêtes par seconde, 100 par passage et 400 par heure.
- `.github/workflows/update.yml` lance le script toutes les 15 minutes. Il enregistre les changements dans le dépôt, puis publie `public/` sur Cloudflare Pages uniquement si les données ont changé.
- `state.json` garde l'heure du dernier téléchargement de chaque fichier.

## Fichiers publiés

Sous `https://formationlap-oax.pages.dev/ergast/f1/` (format Ergast, réponses jolpica sans modification) :

| Fichier | Contenu |
|---|---|
| `{saison}.json` | calendrier |
| `{saison}/last/results.json` | dernière course courue |
| `{saison}/{manche}/results.json` | résultat d'une course |
| `{saison}/{manche}/sprint.json` | résultat d'un sprint |
| `{saison}/{manche}/qualifying.json` | qualifications |
| `{saison}/{results,sprint,qualifying}/offset-{n}.json` | pages de 100 lignes de toute la saison |
| `{saison}/driverstandings.json` | classement pilotes |
| `{saison}/constructorstandings.json` | classement constructeurs |

Un fichier absent (manche pas encore courue) renvoie une erreur 404.

## Mise en place

Secrets du dépôt (Settings › Secrets and variables › Actions) :

- `CLOUDFLARE_API_TOKEN` : jeton avec la permission **Account › Cloudflare Pages › Edit**.
- `CLOUDFLARE_ACCOUNT_ID` : identifiant du compte Cloudflare.

Premier lancement : onglet Actions › « Mise à jour des données » › Run workflow.

En local, sans publication :

```bash
python3 -m unittest discover relay
python3 relay/update.py
```

## Licence des données

Les données viennent de jolpica-f1, sous licence [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) : usage non commercial, avec attribution et partage dans les mêmes conditions.

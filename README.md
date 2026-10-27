# Extracteur Sport

Rassemble dix ans d'activités sportives éparpillées entre plusieurs applis (Google Fit, Runtastic, Samsung Health,
Smashrun, Runalyze, Moves, TomTom, Withings, Zepp, Health Sync, montre Garmin) en **une activité FIT par sortie réelle** :
doublons regroupés entre applis, fuseaux horaires corrigés, totaux journaliers et trajets en véhicule écartés, morceaux de
détection automatique recollés. Les activités peuvent ensuite être envoyées vers Garmin Connect, Strava, intervals.icu,
Runalyze et Endurain (auto-hébergé), avec une petite interface web.

- `outils/` : l'outil en ligne de commande `sport.py` et son interface web (voir [outils/LISEZ-MOI.md](outils/LISEZ-MOI.md))
- `endurain/` : `docker-compose.yml` pour Endurain et l'interface web (copier `.env.example` en `.env`)
- `chemins.exemple.yml` : à copier en `chemins.yml` pour indiquer où sont vos exports et où ranger les résultats
  (sans ce fichier, tout est dans le dossier du projet)

Les identifiants des services sont rangés dans `~/.config/sport/`, jamais dans le projet.

Licence MIT : voir [LICENSE](LICENSE).

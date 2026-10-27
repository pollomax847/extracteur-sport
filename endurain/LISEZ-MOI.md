# Endurain : gestion des données sport

Endurain est une plateforme auto-hébergée (une alternative à Strava). Elle importe les fichiers FIT, TCX et GPX et se synchronise avec Strava et Garmin Connect.
Doc : https://docs.endurain.com

## Démarrer / arrêter
```bash
cd endurain
docker compose up -d      # démarrer
docker compose down       # arrêter (les données sont conservées)
docker compose logs -f endurain
```
Interface : http://localhost:8080. Identifiant de départ : admin / admin, à changer dès la première connexion.

Le même `docker compose` démarre aussi **sport-outils** sur http://localhost:8090. C'est l'interface web de `outils/sport.py` : conversion en FIT, tri, envoi vers Garmin Connect et sauvegarde de la montre. Voir `outils/LISEZ-MOI.md`.

## Importer l'historique
Les activités sont préparées par `outils/sport.py preparer` dans `activites-v2/`, puis envoyées à Endurain depuis l'interface http://localhost:8090 (carte « Endurain ») ou avec `outils/sport.py envoyer endurain`. Endurain écarte les doublons.

## Synchronisation
- **Garmin Connect** : Paramètres → Intégrations, avec votre identifiant Garmin. La synchronisation est automatique toutes les heures.
- **Strava** : vous devez d'abord créer une application API sur https://www.strava.com/settings/api (Authorization Callback Domain : `localhost`). Saisissez ensuite le Client ID et le Secret dans Paramètres → Intégrations.
- **Android** : RunnerUp et Gadgetbridge peuvent envoyer les activités directement à Endurain.

## Notes techniques
- La base PostgreSQL est stockée dans le volume Docker `endurain_endurain-pgdata`, car le SSD est en exFAT et PostgreSQL ne peut pas y gérer ses droits.
- Les fichiers d'activités sont stockés dans `data/backend/data` sur le SSD.
- Pour accéder à l'interface depuis un autre appareil du réseau, remplacez `127.0.0.1:8080` par `8080:8080` dans docker-compose.yml et `ENDURAIN_HOST` par l'IP du PC dans `.env`.
- Le fichier `.env` contient des secrets. Ne le partagez pas.

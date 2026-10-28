# Relais

## Organisation du projet
| Dossier | Contenu |
|---|---|
| `sources-brutes/` | **tous vos exports d'origine**, archivés : `sources-brutes.zip` (99 353 fichiers, vérifiés un par un par leur empreinte, voir `liste.csv`) et `zips/` (les gros exports déjà en ZIP : Runtastic 2022, Google Takeout, Withings, Moves…). L'outil les lit directement, sans les décompresser |
| `activites-v2/` | **le résultat** : un FIT par sortie réelle, rangé par catégorie puis par année (`marche/`, `course/`, `course-tapis/`, `randonnee/`, `velo/`, `velo-interieur/`, `natation-piscine/`, `natation-eau-libre/`, `musculation/`…), avec `sommeil/sommeil.csv`, `index.csv` (provenance et remarques de chaque activité) et `ecartees.csv` (chaque séance écartée et pourquoi) |
| `sante/` | pas, poids et sommeil, une valeur par jour (CSV) ; `montre.csv` : la journée vue par la montre (pas, FC de repos, min, moyenne et max, stress, minutes d'intensité) |
| `montre-garmin/` | sauvegardes de la montre branchée en USB |
| `vers-montre/` | parcours et entraînements en attente du prochain branchement (`relais.py vers-montre`) |
| `analyse/` | `ANALYSE.md` : ce que contient chaque source et les règles de conversion |
| `endurain/` | Endurain et l'interface `relais` (`docker compose`) |
| `outils/` | le code ; `historique-v1/` garde la trace de ce que l'ancienne version a envoyé à Garmin |

Pour ajouter un nouvel export : déposez son dossier ou son ZIP dans `sources-brutes/`, puis lancez « Reconvertir ».

Ces emplacements sont ceux par défaut. Pour les changer, par exemple pour lire vos exports sur un autre disque, copiez
`chemins.exemple.yml` en `chemins.yml` à la racine du projet et indiquez-y vos dossiers : `donnees` (exports lus en
entier), `sources` (autres dossiers à lire), `activites`, `sante`, `montre` et `corbeille`. Avec l'interface Docker, un
dossier hors du projet doit aussi être monté dans `endurain/docker-compose.yml`.

## Interface : http://localhost:8090
- **Bibliothèque** : nombre d'activités, part avec GPS, période, répartition par catégorie.
- **Envoyer vers** : une carte par service, avec sa progression, sa configuration et un bouton d'envoi. Chaque service tient son propre journal (`outils/.envoyes-<service>-v2.txt` : fichier, statut, départ, durée) : un envoi reprend là où il s'était arrêté. Une activité dont le fichier a changé de nom après une reconversion (meilleur exemplaire, heure corrigée) est reconnue par son départ et sa durée et n'est pas renvoyée ; une activité que Health Sync a lue dans Garmin ou Strava n'y est pas renvoyée non plus.
- **Données** : reconversion, santé, sauvegarde de la montre, et « Tout faire ».
- **Comptes** : Garmin (e-mail, mot de passe, code MFA) et Endurain.
- **Journal** : la sortie de la commande en cours, avec une barre de progression et un bouton Arrêter.

Après une modification du code : `cd endurain && docker compose restart sport`.

## Commandes
```bash
outils/relais.py preparer                 # conversion précise → activites-v2/ (+ contrôle de chaque FIT)
outils/relais.py sante                    # pas, poids, sommeil → sante/, Endurain ; pesées → Garmin
outils/relais.py envoyer intervals        # tout ce qui reste vers intervals.icu (garmin, strava, runalyze, endurain, smashrun)
outils/relais.py envoyer garmin 50        # seulement 50 ; --reessayer retente aussi les refusées
outils/relais.py lots runalyze            # activités pas encore envoyées → a-importer/runalyze/lot-N, à importer sur le site
outils/relais.py lots runalyze --importes # une fois l'import fait : notées envoyées, lots rangés dans a-supprimer/
outils/relais.py montre                   # sauvegarde de la montre (lancé tout seul au branchement)
outils/relais.py auto                     # montre + conversion + santé + envois choisis
outils/relais.py refaire garmin           # retire de Garmin ce que l'ancienne version y a envoyé
outils/relais.py retirer endurain         # retire ce que l'outil a envoyé et qui n'est plus dans la bibliothèque (garmin, endurain, smashrun ; --essai pour voir)
```
Une seule commande longue tourne à la fois, que ce soit dans le terminal ou l'interface. Le verrou est `outils/.verrou`.

**Limites d'envoi des services** (vérifiées le 27/10/2026) :
- Strava autorise 200 requêtes par 15 min et 2 000 par jour.
- intervals.icu autorise 5 000 requêtes par jour avec une clé personnelle.
- Garmin ne publie pas de limite.
- Runalyze n'annonce aucune limite, mais Cloudflare bloque l'API après quelques centaines d'envois. Pour un gros
  historique, utilisez `lots` et l'import du site : 1 000 fichiers au plus à la fois, doublons détectés par l'heure.

Pour les nouvelles activités, Tapiriik (lien en haut de l'interface) peut synchroniser les services entre eux.

## Destinations
| Service | Ce qu'il faut | Remarques |
|---|---|---|
| **Garmin Connect** | connexion dans l'interface (une fois) | mode import. **Les services reliés à Garmin (Strava…) reçoivent aussi les activités** : coupez la liaison pendant l'envoi de l'historique |
| **Strava** | une application sur https://www.strava.com/settings/api (« Authorization Callback Domain » : `localhost`), puis son Client ID et son Secret dans l'interface, et le bouton d'autorisation | limite d'envois de Strava : l'outil s'arrête proprement et reprend plus tard |
| **intervals.icu** | clé d'API (intervals.icu → Settings → Developer Settings) | gratuit ; refuse lui-même les doublons |
| **Runalyze** | jeton « Personal API » (Réglages, avec date d'expiration) | Runalyze ne publie pas le chemin d'envoi : il est vérifié au premier fichier, et l'outil s'arrête si Runalyze le refuse |
| **Endurain** | clé d'API (déjà configurée) | Endurain écarte les doublons |
| **Smashrun** | bouton « Autoriser Smashrun » dans l'interface : connexion à Smashrun, le jeton est récupéré tout seul (12 semaines, recliquer ensuite) | course à pied avec distance seulement ; avant d'envoyer, l'outil lit les courses déjà sur le compte (venues de Garmin, Strava…) et ne les renvoie pas ; sans trace, la course part en JSON ; 250 envois par heure |

Decathlon n'accepte pas l'envoi de fichiers : reliez-le à Garmin.

Dans l'interface, chaque service a un seul bouton : tant que les champs sont vides, il ouvre la page du service où
l'on obtient la clé (ou autorise directement, pour Smashrun) ; une fois les champs remplis, il enregistre. « Envoyer »
envoie tout ce qui reste, au rythme permis par le service ; s'il demande d'attendre, l'envoi s'arrête et un nouveau clic
reprend.

Les clés sont vérifiées auprès du service avant d'être enregistrées dans `~/.config/sport/sport.env`, lisible par vous seul.
Le mode automatique n'envoie que vers les services listés dans `SPORT_AUTO_DESTINATIONS=garmin,intervals` (sport.env) : aucun par défaut.

## Pas, poids et sommeil
Les sources sont Samsung, Withings, Mi Fit, Runtastic, Google Fit, Moves et Health Sync (poids et sommeil seulement : ses pas mélangent plusieurs applis sans les nommer). On garde une valeur par jour :
- **pas** : le plus grand total ;
- **poids** : la pesée la plus complète. Les recopies MyFitnessPal et Google Fit et les erreurs de saisie (plus de 8 kg d'écart) sont écartées ;
- **sommeil** : la nuit la plus détaillée, datée du jour du réveil.

Garmin n'accepte que les pesées. Le sommeil n'existe pas en FIT importable : il est dans `sante/` et dans `activites-v2/sommeil/`, et il part vers Endurain.

## Montre Garmin (Instinct E-Sports)
Branchée en USB, elle apparaît sur `/media/<utilisateur>/GARMIN`. Le service `relais-montre` (systemd utilisateur) sauvegarde ses fichiers dans `montre-garmin/` à chaque branchement, envoie ses activités à Endurain, puis lance `relais.py sante` (pas et fréquence cardiaque de la montre compris).

`relais.py sante` lit aussi ses fichiers `Monitor/` : les pas du jour rejoignent `sante/pas.csv` (et Endurain), et le reste va dans `sante/montre.csv`. Le 25/10/2026, la montre ne gardait que le fichier du jour : chaque branchement ne sauvegarde que les journées encore présentes. Son sommeil n'est pas lu, car l'Instinct laisse les serveurs Garmin calculer les phases ; il vient de Health Sync.

Parcours et entraînements vers la montre, sans l'appli du téléphone :
```bash
outils/relais.py vers-montre boucle.gpx              # itinéraire (AllTrails, Komoot…, avec ou sans heures), TCX ou activité FIT à refaire
outils/relais.py vers-montre course.fit --nom "Tour du lac"   # nom affiché sur la montre (16 caractères)
outils/relais.py vers-montre garmin                  # liste des parcours et entraînements de Garmin Connect
outils/relais.py vers-montre garmin Morières 1613752333      # les prépare (nom ou numéro), sans rien changer sur Garmin
```
Les fichiers attendent dans `vers-montre/` et sont copiés dans `Garmin/NewFiles` de la montre au branchement suivant (tout de suite si elle est branchée). Ils passent ensuite dans `vers-montre/deposes/`. La montre les importe quand on la **débranche**.

Données satellites (EPO), pour que le GPS accroche plus vite, comme avec Garmin Express : à chaque branchement, `relais.py montre` remplace `Garmin/RemoteSW/EPO.BIN` par les orbites prévues des 9 prochains jours (serveur MediaTek), si celles de la montre ont plus de 2 jours. `relais.py satellites` le force. L'outil n'écrit que si la montre a déjà ce fichier, au même format ; une montre qui utilise `CPE.BIN` (puces Sony) est laissée telle quelle, faute de source publique.

L'interface web (port 8090) montre la santé : la dernière valeur de chaque mesure, les pas, le sommeil et le poids sur 1 an, 3 ans ou tout l'historique, et le tableau des mêmes valeurs.

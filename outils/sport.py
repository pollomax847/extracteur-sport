#!/usr/bin/env python3
"""Outil unique pour les données sport : conversion précise, montre Garmin, santé, envoi vers les services.

  sport.py preparer     conversion précise de toutes les sources (sources-brutes/, ZIP compris) : une activité
                        par sortie réelle, en FIT, rangée par catégorie dans activites-v2/ (+ contrôle)
  sport.py sante        pas, poids et sommeil de toutes les sources → sante/, Endurain (et les pesées → Garmin)
  sport.py envoyer <garmin|strava|intervals|runalyze|endurain> [N] [--reessayer]
                        envoie N activités de activites-v2/ (100 par défaut), de la plus ancienne à la plus récente
  sport.py montre       sauvegarde la montre branchée et envoie ses nouvelles activités à Endurain
  sport.py auto         tout en une fois : montre, conversion, santé, envois choisis (SPORT_AUTO_DESTINATIONS)
  sport.py refaire garmin   retire de Garmin ce que l'ancienne version y a envoyé (avant d'envoyer activites-v2)
  sport.py etat         montre branchée ? Endurain en marche ?

Configuration (identifiants Endurain, clés des services) : ~/.config/sport/sport.env. Interface : http://localhost:8090
"""
import csv
import fcntl
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent


def lire_chemins():
    """Emplacements des données, lus dans chemins.yml à la racine du projet (modèle : chemins.exemple.yml).
    Sans ce fichier, tout est dans le projet. Chemins absolus, ou relatifs au projet ; « ~ » accepté."""
    fichier = PROJET / "chemins.yml"
    if not fichier.exists():
        return {}
    try:
        import yaml
    except ImportError:
        sys.exit(f"{fichier.name} demande PyYAML : pip install pyyaml")
    return yaml.safe_load(fichier.read_text()) or {}


def chemin(valeur):
    return PROJET / Path(str(valeur)).expanduser()  # un chemin absolu remplace PROJET


CHEMINS = lire_chemins()
ARCHIVE = chemin(CHEMINS.get("montre", "montre-garmin"))
ENVOYES = ARCHIVE / ".envoyes.txt"
CONFIG = Path.home() / ".config" / "sport" / "sport.env"
DOSSIER_IMPORT = PROJET / "endurain" / "data" / "backend" / "data" / "activity_files" / "bulk_import" / "1"
JETONS_GARMIN = Path.home() / ".config" / "sport" / "garmin-jetons"
JETONS_STRAVA = Path.home() / ".config" / "sport" / "strava.json"
VENV = Path.home() / ".local" / "share" / "sport" / "venv"
VERROU = PROJET / "outils" / ".verrou"
COMMANDES_EXCLUSIVES = {"envoyer", "sante", "auto", "refaire", "preparer"}
V2 = chemin(CHEMINS.get("activites", "activites-v2"))  # conversion précise : une activité par sortie réelle (sport.py preparer)
CORBEILLE = chemin(CHEMINS.get("corbeille", "a-supprimer"))  # anciennes versions de activites-v2 (jamais effacées par l'outil)
SANTE = chemin(CHEMINS.get("sante", "sante"))  # pas, poids et sommeil retenus, en CSV lisibles
ETAT_SANTE = PROJET / "outils" / ".sante-endurain.json"  # ce qui a déjà été envoyé à Endurain
POIDS_GARMIN = PROJET / "outils" / ".poids-garmin.txt"  # pesées créées sur Garmin par l'outil (jour, kg)
AUTO_GARMIN = 300  # activités envoyées par destination et par passage automatique (SPORT_AUTO_NOMBRE dans sport.env)

# Toutes les sources lues par « inventaire » et « historique » : « donnees » et « sources » de chemins.yml.
# SPORT_SOURCES=libellé=chemin;… dans sport.env en ajoute d'autres.
SOURCES = {"projet": chemin(CHEMINS.get("donnees", ".")),
           **{nom: chemin(dossier) for nom, dossier in (CHEMINS.get("sources") or {}).items()}}
# Google Drive/sport n'est plus lu par défaut : tout son contenu est déjà copié dans sport/ (inventaire du 25/10/2026 :
# aucune activité unique) et ce disque NTFS est très lent quand il est occupé. Pour le relire :
#   sport.py convertir "<disque>/Google Drive/sport"
# Dans le projet : outils, documentation et la copie de travail d'Endurain ne sont pas des données
EXCLUS_PROJET = {"endurain", "outils", "scripts", "documentation", "sante", "a-supprimer", "activites-v2", "analyse",
                 SANTE.name, CORBEILLE.name, V2.name}

# Dossiers de la montre qui contiennent vos données. Les autres (Debug, EDM, REMOTESW, TEXT…) sont internes à la montre.
DOSSIERS_MONTRE = ["Activity", "Monitor", "Sleep", "Summary", "Records", "Totals",
                   "Courses", "Workouts", "Location", "Goals", "Settings", "Sports"]


def config():
    conf = {"ENDURAIN_URL": "http://localhost:8080"}
    if CONFIG.exists():
        for ligne in CONFIG.read_text().splitlines():
            if "=" in ligne and not ligne.lstrip().startswith("#"):
                cle, valeur = ligne.split("=", 1)
                conf[cle.strip()] = valeur.strip()
    conf.update({k: v for k, v in os.environ.items() if k.startswith("ENDURAIN_")})
    return conf


def notifier(message):
    print(message)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-i", "watch", "Montre Garmin", message], check=False)


def trouver_montre():
    """Point de montage de la montre (stockage USB contenant un dossier Garmin/), ou None."""
    with open("/proc/mounts") as f:
        # /proc/mounts écrit les espaces en \040
        points = [re.sub(r"\\(\d{3})", lambda m: chr(int(m[1], 8)), ligne.split()[1]) for ligne in f]
    for point in points:
        if not point.startswith(("/media/", "/run/media/", "/mnt/")):
            continue
        try:
            if (Path(point) / "Garmin" / "Activity").is_dir():
                return Path(point)
        except OSError:
            continue
    return None


def endurain_actif(conf):
    try:
        with urllib.request.urlopen(conf["ENDURAIN_URL"] + "/api/v1/about", timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def envoyer_activite(conf, fichier):
    """Envoie un .fit à Endurain. L'import se fait en arrière-plan et Endurain ignore les doublons."""
    limite = uuid.uuid4().hex
    corps = (f"--{limite}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fichier.name}\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n").encode() + fichier.read_bytes() + f"\r\n--{limite}--\r\n".encode()
    req = urllib.request.Request(
        conf["ENDURAIN_URL"] + "/api/v1/activities/upload", data=corps, method="POST",
        headers={"X-API-Key": conf["ENDURAIN_API_KEY"],
                 "Content-Type": f"multipart/form-data; boundary={limite}",
                 # même fichier = même clé : un nouvel essai après une coupure ne crée pas de doublon
                 "Idempotency-Key": f"garmin-{fichier.name}-{fichier.stat().st_size}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def cmd_montre():
    montre = trouver_montre()
    if not montre:
        sys.exit("Aucune montre Garmin branchée (pas de dossier Garmin/ sur un disque USB monté).")

    copies = 0
    for dossier in DOSSIERS_MONTRE:
        source = montre / "Garmin" / dossier
        if not source.is_dir():
            continue
        for fichier in source.rglob("*"):
            if not fichier.is_file():
                continue
            cible = ARCHIVE / dossier / fichier.relative_to(source)
            # Les fichiers du jour (Monitor, Sleep) grossissent au fil de la journée : on recopie s'ils ont changé
            if cible.exists() and cible.stat().st_size == fichier.stat().st_size:
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fichier, cible)
            copies += 1
    print(f"Sauvegarde : {copies} fichier(s) copiés dans {ARCHIVE}")

    conf = config()
    if not conf.get("ENDURAIN_API_KEY"):
        notifier(f"Montre sauvegardée ({copies} fichiers). Pas de clé d'API dans {CONFIG} : rien n'est envoyé à Endurain.")
        return
    if not endurain_actif(conf):
        notifier(f"Montre sauvegardée ({copies} fichiers). Endurain ne répond pas : relancez « sport.py montre » une fois Endurain démarré.")
        return

    deja = set(ENVOYES.read_text().split()) if ENVOYES.exists() else set()
    envoyes = []
    for fichier in sorted((ARCHIVE / "Activity").glob("*.[fF][iI][tT]")):
        if fichier.name in deja:
            continue
        try:
            envoyer_activite(conf, fichier)
        except urllib.error.HTTPError as e:
            print(f"  {fichier.name} : refusé par Endurain ({e.code} {e.read().decode()[:200]})")
            continue
        envoyes.append(fichier.name)
        print(f"  {fichier.name} : envoyé")
    with ENVOYES.open("a") as f:
        f.writelines(nom + "\n" for nom in envoyes)
    notifier(f"Montre sauvegardée ({copies} fichiers), {len(envoyes)} nouvelle(s) activité(s) envoyée(s) à Endurain.")


def cmd_etat():
    montre = trouver_montre()
    if montre:
        libre = shutil.disk_usage(montre)
        activites = sorted((montre / "Garmin" / "Activity").glob("*.[fF][iI][tT]"))
        print(f"Montre       : branchée sur {montre}, mémoire {libre.used * 100 // libre.total} % utilisée")
        print(f"               {len(activites)} activité(s) dessus, la plus récente : {activites[-1].name if activites else 'aucune'}")
    else:
        print("Montre       : non branchée")

    conf = config()
    print(f"Endurain     : {'en marche' if endurain_actif(conf) else 'arrêté'} ({conf['ENDURAIN_URL']})")
    print(f"Clé d'API    : {'configurée' if conf.get('ENDURAIN_API_KEY') else 'absente'} ({CONFIG})")

    if ARCHIVE.exists():
        detail = ", ".join(f"{d.name} {sum(1 for f in d.rglob('*') if f.is_file())}"
                           for d in sorted(ARCHIVE.iterdir()) if d.is_dir())
        nb_envoyes = len(ENVOYES.read_text().split()) if ENVOYES.exists() else 0
        print(f"Archive      : {detail} ; {nb_envoyes} activité(s) déjà envoyée(s)")
    else:
        print("Archive      : vide (lancez « sport.py montre »)")


def sources():
    toutes = dict(SOURCES)
    for paire in filter(None, config().get("SPORT_SOURCES", "").split(";")):
        libelle, chemin = paire.split("=", 1)
        toutes[libelle.strip()] = Path(chemin.strip()).expanduser()
    return toutes


def utiliser_venv():
    """garminconnect et garmin-fit-sdk sont dans un environnement à part : on relance la commande avec son Python."""
    try:
        import garmin_fit_sdk  # noqa: F401
        import garminconnect  # noqa: F401
    except ImportError:
        python_venv = VENV / "bin" / "python"
        if python_venv.exists() and Path(sys.executable) != python_venv:
            os.execv(python_venv, [str(python_venv), __file__, *sys.argv[1:]])
        sys.exit(f"Bibliothèques absentes : python3 -m venv {VENV} && {VENV}/bin/pip install garminconnect garmin-fit-sdk pyyaml")


def connexion_garmin():
    """Session Garmin Connect. Le mot de passe n'est demandé qu'à la première connexion, ensuite les jetons suffisent."""
    from garminconnect import Garmin

    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)  # les erreurs sont résumées par sport.py
    JETONS_GARMIN.mkdir(parents=True, exist_ok=True)
    JETONS_GARMIN.chmod(0o700)
    try:
        api = Garmin()
        api.login(str(JETONS_GARMIN))
        return api
    except Exception:
        pass  # pas encore de jetons, ou jetons expirés
    if not sys.stdin.isatty():
        raise RuntimeError("connectez-vous une fois à Garmin dans l'interface web ou le terminal")
    email = input("E-mail Garmin Connect : ")
    api = Garmin(email, getpass.getpass("Mot de passe Garmin Connect : "),
                 prompt_mfa=lambda: input("Code de vérification Garmin (MFA) : "))
    api.login(str(JETONS_GARMIN))
    return api


DESTINATIONS = ("garmin", "strava", "intervals", "runalyze", "endurain")
PAUSES = {"garmin": 5, "strava": 10, "intervals": 1, "runalyze": 5, "endurain": 3}  # s entre deux envois


class GarminV2:
    """Garmin Connect, pour les FIT de activites-v2/ (déjà compacts, avec des points réguliers sans trace)."""
    nom, titre = "garmin", "Garmin Connect"

    def __init__(self):
        self.api = connexion_garmin()

    def verifier(self):
        return self.api.get_full_name() if hasattr(self.api, "get_full_name") else "connecté"

    def envoyer(self, fichier, activite):
        import destinations
        from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError

        try:
            # « import » comme sur le site Garmin. Les services reliés à Garmin (Strava…) la reçoivent quand même.
            resultat = self.api.import_activity(str(fichier))
        except GarminConnectTooManyRequestsError as e:
            raise destinations.Limite("Garmin limite le nombre d'envois") from e
        except GarminConnectConnectionError as e:
            return "deja" if "uplicate" in str(e) or "409" in str(e) else f"refuse : {str(e)[:120]}"
        echecs = (resultat or {}).get("detailedImportResult", {}).get("failures") or []
        if not echecs:
            return "envoye"
        return "deja" if "uplicate" in str(echecs) else f"refuse : {str(echecs)[:120]}"


def destination(nom):
    import destinations

    conf = config()
    if nom == "garmin":
        return GarminV2()
    if nom == "strava":
        return destinations.Strava(conf, JETONS_STRAVA)
    if nom == "intervals":
        return destinations.Intervals(conf)
    if nom == "runalyze":
        return destinations.Runalyze(conf)
    if nom == "endurain":
        return destinations.Endurain(conf)
    raise ValueError(f"destination inconnue : {nom} (possibles : {', '.join(DESTINATIONS)})")


def journal_envois(nom):
    return PROJET / "outils" / f".envoyes-{nom}-v2.txt"


def lignes_envois(nom):
    """Lignes du journal : [fichier, statut, début UTC, durée min]. Début et durée manquent dans les lignes écrites avant
    le 26/10/2026 que completer_journal() n'a pas pu compléter."""
    journal = journal_envois(nom)
    if not journal.exists():
        return []
    return [(c + ["", ""])[:4] for c in (l.split("\t") for l in journal.read_text().splitlines()) if len(c) >= 2]


def etat_envois(nom):
    """{fichier de activites-v2 : dernier statut}."""
    return {l[0]: l[1] for l in lignes_envois(nom)}


def completer_journal(nom):
    """Ajoute début et durée aux lignes écrites avant le 26/10/2026, d'après activites-v2/index.csv et ses anciennes
    versions (a-supprimer/) : une activité déjà envoyée est ainsi reconnue même si son fichier change de nom."""
    lignes = lignes_envois(nom)
    if all(l[2] for l in lignes):
        return
    connus = {}
    for index in [V2 / "index.csv", *sorted(CORBEILLE.glob("*/activites-v2/index.csv"), reverse=True)]:
        if index.exists():
            with index.open() as f:
                for a in csv.DictReader(f):
                    connus.setdefault(a["fichier"], (a["debut_utc"], a["duree_min"]))
    for l in lignes:
        if not l[2] and l[0] in connus:
            l[2], l[3] = connus[l[0]]
    provisoire = journal_envois(nom).with_suffix(".tmp")
    provisoire.write_text("".join("\t".join(l) + "\n" for l in lignes))
    provisoire.replace(journal_envois(nom))


ECART_DEPART, RECOUVREMENT = 120, 0.6  # mêmes règles que regrouper.py pour reconnaître la même sortie
# appli d'origine (colonne « enregistreur ») des activités lues par Health Sync dans un service : y sont déjà
ORIGINE_SERVICE = {"garmin": "Garmin", "strava": "Strava"}


def deja_envoyee(a, envoyees):
    """Fichier d'une activité déjà envoyée sous un autre nom (la même sortie, dont le meilleur exemplaire ou l'heure
    a changé depuis l'envoi), ou None. envoyees : [(début, durée s ou None, fichier)]."""
    debut, duree = datetime.fromisoformat(a["debut_utc"]), float(a["duree_min"]) * 60
    for debut_e, duree_e, fichier in envoyees:
        ecart = abs((debut_e - debut).total_seconds())
        if duree_e is None:  # ligne ancienne non complétée : le départ seul
            if ecart <= ECART_DEPART:
                return fichier
            continue
        if ecart <= ECART_DEPART and abs(duree - duree_e) <= 0.1 * max(duree, duree_e) + 60:
            return fichier
        commun = (min(debut + timedelta(seconds=duree), debut_e + timedelta(seconds=duree_e)) - max(debut, debut_e)).total_seconds()
        if commun > 0 and commun / max(min(duree, duree_e), 1) >= RECOUVREMENT:
            return fichier
    return None


def envoyer_v2(nom, limite, reessayer=False):
    """Envoie au plus « limite » activités de activites-v2/ vers une destination, de la plus ancienne à la plus récente.

    Une activité déjà envoyée sous un autre nom (même sortie) ou venue de ce service (Health Sync) n'est pas renvoyée :
    elle est notée « deja » dans le journal."""
    import destinations

    with (V2 / "index.csv").open() as f:
        activites = sorted(csv.DictReader(f), key=lambda a: a["debut_utc"])
    completer_journal(nom)
    etat = etat_envois(nom)
    envoyees = []
    for fichier, statut, debut, duree in lignes_envois(nom):
        if not statut.startswith("refuse"):
            m = re.search(r"(\d{4}-\d\d-\d\d)_(\d\d)(\d\d)(\d\d)", fichier)  # nom = départ UTC
            debut = debut or (m and f"{m[1]}T{m[2]}:{m[3]}:{m[4]}+00:00")
            if debut:
                envoyees.append((datetime.fromisoformat(debut), float(duree) * 60 if duree else None, fichier))
    a_faire, doublons = [], []
    for a in activites:
        if a["fichier"] in etat and not (reessayer and etat[a["fichier"]].startswith("refuse")):
            continue
        if a["source"] == "Health Sync" and a["enregistreur"] == ORIGINE_SERVICE.get(nom):
            doublons.append((a, f"deja : vient de {a['enregistreur']} (lue par Health Sync)"))
        elif a["fichier"] not in etat and (ancien := deja_envoyee(a, envoyees)):
            doublons.append((a, f"deja : envoyée sous {ancien}"))
        else:
            a_faire.append(a)
    if doublons:
        with journal_envois(nom).open("a") as journal:
            for a, statut in doublons:
                journal.write(f"{a['fichier']}\t{statut}\t{a['debut_utc']}\t{a['duree_min']}\n")
                print(f"  {a['fichier']} : {statut}")
        etat = etat_envois(nom)
    a_faire = a_faire[:limite]
    print(f"{nom} : {len(activites)} activités, {len(etat)} déjà traitées, {len(a_faire)} à envoyer maintenant")
    if not a_faire:
        return Counter()
    try:
        cible = destination(nom)
        print(f"  {cible.titre} : {cible.verifier()}")
    except destinations.NonConfiguree as e:
        print(f"  {nom} : {e}")
        return Counter()
    compte = Counter()
    with journal_envois(nom).open("a") as journal:
        for n, a in enumerate(a_faire, 1):
            try:
                statut = cible.envoyer(V2 / a["fichier"], a)
            except destinations.Limite as e:
                print(f"  {e} : arrêt après {n - 1} envoi(s), relancer plus tard")
                break
            except destinations.NonConfiguree as e:
                print(f"  {e}")
                break
            statut = re.sub(r"\s+", " ", statut)  # une ligne par envoi, colonnes séparées par des tabulations
            journal.write(f"{a['fichier']}\t{statut}\t{a['debut_utc']}\t{a['duree_min']}\n")
            journal.flush()
            compte[statut.split(" :")[0]] += 1
            if statut.startswith("refuse"):
                print(f"  {a['fichier']} : {statut}")
            if n % 10 == 0:
                print(f"  {n}/{len(a_faire)} ({a['debut_utc'][:10]})", flush=True)
            time.sleep(PAUSES.get(nom, 2))
    print(f"  {nom} : " + (", ".join(f"{v} {k}" for k, v in compte.items()) or "rien envoyé"))
    return compte


def cmd_envoyer():
    """sport.py envoyer <destination> [N] [--reessayer] : N activités de activites-v2/ (100 par défaut)."""
    args = sys.argv[2:]
    if not args or args[0] not in DESTINATIONS:
        sys.exit(f"Usage : sport.py envoyer <{'|'.join(DESTINATIONS)}> [nombre, 100 par défaut] [--reessayer]")
    utiliser_venv()
    nombres = [a for a in args[1:] if a.isdigit()]
    envoyer_v2(args[0], int(nombres[0]) if nombres else 100, "--reessayer" in args)


class Endurain:
    """Session sur l'API d'Endurain avec un compte utilisateur (la clé d'API ne sert qu'à envoyer des activités).

    Identifiants : ENDURAIN_USER / ENDURAIN_PASSWORD dans sport.env (enregistrés depuis l'interface web),
    sinon le mot de passe est demandé dans le terminal.
    """

    def __init__(self, conf):
        self.url = conf["ENDURAIN_URL"] + "/api/v1"
        self.utilisateur = conf.get("ENDURAIN_USER", "admin")
        self.mot_de_passe = conf.get("ENDURAIN_PASSWORD")
        if not self.mot_de_passe:
            if not sys.stdin.isatty():
                raise RuntimeError("identifiants Endurain absents : enregistrez-les dans l'interface web")
            self.mot_de_passe = getpass.getpass(f"Mot de passe Endurain pour {self.utilisateur} : ")
        self.jeton = None

    def _connexion(self):
        corps = urllib.parse.urlencode({"username": self.utilisateur, "password": self.mot_de_passe}).encode()
        req = urllib.request.Request(self.url + "/auth/login", data=corps, method="POST",
                                     headers={"X-Client-Type": "mobile", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.jeton = json.loads(resp.read())["access_token"]
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                raise RuntimeError(f"Endurain refuse l'utilisateur « {self.utilisateur} » ou son mot de passe") from e
            raise

    def appel(self, methode, chemin, donnees=None):
        for essai in (1, 2):  # le jeton dure 15 min : on se reconnecte une fois s'il a expiré
            if not self.jeton:
                self._connexion()
            req = urllib.request.Request(
                self.url + chemin, method=methode, data=json.dumps(donnees).encode() if donnees is not None else None,
                headers={"X-Client-Type": "mobile", "Authorization": f"Bearer {self.jeton}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read() or "null")
            except urllib.error.HTTPError as e:
                if e.code == 401 and essai == 1:
                    self.jeton = None
                    continue
                raise


def lancer_import_endurain(session):
    """Démarre l'import du dossier bulk_import d'Endurain (équivalent du bouton Paramètres → Import)."""
    if not any(f.is_file() for f in DOSSIER_IMPORT.rglob("*")):
        print("Endurain : rien de nouveau dans le dossier d'import")
        return
    try:
        session.appel("POST", "/activities/bulk-import/folder?source=generic")
        print("Endurain : import du dossier lancé (il continue en arrière-plan)")
    except urllib.error.HTTPError as e:
        print("Endurain : un import est déjà en cours" if e.code == 409 else f"Endurain : import refusé ({e.code})")


def sante_endurain(session, pas, poids, sommeil):
    """Envoie à Endurain les jours nouveaux ou modifiés (Endurain garde une valeur par jour et la remplace)."""
    import sante

    etat = json.loads(ETAT_SANTE.read_text()) if ETAT_SANTE.exists() else {}
    a_envoyer = ([("/health/steps", f"pas:{j}", sante.json_pas(j, n)) for j, (n, _) in pas.items()]
                 + [("/health/weight", f"poids:{j}", sante.json_poids(p)) for j, p in poids.items()]
                 + [("/health/sleep", f"sommeil:{j}", sante.json_sommeil(s)) for j, s in sommeil.items()])
    compte = Counter()
    for n, (chemin, cle, corps) in enumerate(a_envoyer, 1):
        signature = json.dumps(corps, sort_keys=True)
        if etat.get(cle) == signature:
            continue
        try:
            session.appel("POST", chemin, corps)
            etat[cle] = signature
            compte[cle.split(":")[0]] += 1
        except urllib.error.HTTPError as e:
            compte["refusés"] += 1
            print(f"  refusé par Endurain : {cle} ({e.code} {e.read().decode()[:150]})")
        if n % 500 == 0:
            ETAT_SANTE.write_text(json.dumps(etat))
    ETAT_SANTE.write_text(json.dumps(etat))
    print("Endurain : " + (", ".join(f"{v} {k}" for k, v in compte.items()) or "déjà à jour"))


def poids_garmin(api, poids):
    """Synchronise les pesées retenues avec Garmin Connect (Garmin n'accepte ni les pas ni le sommeil).

    Le journal POIDS_GARMIN note chaque pesée que l'outil a créée (« jour<TAB>kg »). Une pesée créée par l'outil
    et qui n'est plus retenue (recopie MyFitnessPal, erreur de saisie…) est retirée de Garmin ; les pesées
    saisies ailleurs (balance, appli Garmin) ne sont jamais touchées, et un jour qui en a déjà une est sauté.
    """
    envoyees = {}
    if POIDS_GARMIN.exists():
        for ligne in POIDS_GARMIN.read_text().splitlines():
            jour, _, kg = ligne.partition("\t")
            envoyees[jour] = float(kg) if kg else None

    retirees = 0
    for jour, kg in list(envoyees.items()):
        retenue = poids.get(date.fromisoformat(jour))
        if retenue and (kg is None or abs(retenue.kg - kg) <= 0.05):
            continue
        for pesee in (api.get_daily_weigh_ins(jour) or {}).get("dateWeightList", []):
            # seulement une saisie « MANUAL » à la valeur que l'outil avait envoyée
            if pesee.get("sourceType") == "MANUAL" and kg is not None and abs(pesee.get("weight", 0) / 1000 - kg) <= 0.05:
                api.delete_weigh_in(pesee["samplePk"], jour)
                retirees += 1
        del envoyees[jour]
        time.sleep(0.5)
    if retirees:
        print(f"Garmin : {retirees} pesée(s) créées par l'outil et plus retenues, retirées")

    jours = sorted(j for j in poids if j.isoformat() not in envoyees)
    deja = set()  # jours où Garmin a une pesée venant d'ailleurs
    for annee in sorted({j.year for j in jours}):
        try:
            reponse = api.get_weigh_ins(f"{annee}-01-01", f"{annee}-12-31") or {}
        except Exception:
            continue
        deja |= {r.get("summaryDate") for r in reponse.get("dailyWeightSummaries", [])}
    ajoutees = 0
    for jour in jours:
        if jour.isoformat() in deja:
            continue
        p = poids[jour]
        api.add_weigh_in_with_timestamps(weight=round(p.kg, 2), unitKey="kg",
                                         dateTimestamp=p.locale().replace(tzinfo=None).isoformat(timespec="seconds"),
                                         gmtTimestamp=p.heure.replace(tzinfo=None).isoformat(timespec="seconds"))
        envoyees[jour.isoformat()] = round(p.kg, 2)
        ajoutees += 1
        time.sleep(1)
    POIDS_GARMIN.write_text("".join(f"{j}\t{kg if kg is not None else ''}\n" for j, kg in sorted(envoyees.items())))
    print(f"Garmin : {ajoutees} pesée(s) ajoutée(s), {len(envoyees)} au total venant de l'outil")


def faire_sante(api_garmin=None):
    import sante

    print("Lecture des pas, du poids et du sommeil de toutes les sources…")
    pas, poids, sommeil, lus = sante.collecter(list(sources().values()), EXCLUS_PROJET | {"convertis", SANTE.name})
    print(f"  {lus['pas']} valeurs de pas, {lus['poids']} pesées, {lus['sommeil']} nuits lues ; {lus['écartés']} vides ou aberrantes écartées")
    print(f"  retenu après dédoublonnage : {len(pas)} jours de pas, {len(poids)} jours de poids, {len(sommeil)} nuits")
    sante.ecrire_csv(SANTE, pas, poids, sommeil)
    print(f"  détail : {SANTE}/pas.csv, poids.csv, sommeil.csv")
    copier_sommeil()
    conf = config()
    if endurain_actif(conf):
        try:
            sante_endurain(Endurain(conf), pas, poids, sommeil)
        except (RuntimeError, urllib.error.URLError) as e:
            print(f"Endurain : non envoyé ({e})")
    else:
        print("Endurain : ne répond pas, rien n'est envoyé")
    if api_garmin is not None:
        poids_garmin(api_garmin, poids)


def cmd_sante():
    utiliser_venv()
    try:
        api = connexion_garmin()
    except Exception as e:
        print(f"Garmin : pas de connexion ({e}), les pesées ne sont pas envoyées")
        api = None
    faire_sante(api)


def cmd_auto():
    """Tout en une fois : montre, conversion précise (v2) de toutes les sources, santé, puis envoi vers les
    destinations choisies (SPORT_AUTO_DESTINATIONS=garmin,intervals… dans sport.env ; aucune par défaut)."""
    utiliser_venv()
    conf = config()
    etapes = 0

    def etape(titre):
        nonlocal etapes
        etapes += 1
        print(f"\n=== {etapes}. {titre} ===", flush=True)

    if trouver_montre():
        etape("Montre Garmin")
        try:
            cmd_montre()
        except SystemExit as e:
            print(e)

    etape("Conversion précise de toutes les sources (activites-v2)")
    cmd_preparer()

    api = None
    try:
        api = connexion_garmin()
    except Exception as e:
        print(f"Garmin : pas de connexion ({e}), les pesées ne sont pas envoyées")
    etape("Pas, poids et sommeil")
    faire_sante(api)

    choisies = [d.strip() for d in conf.get("SPORT_AUTO_DESTINATIONS", "").split(",") if d.strip() in DESTINATIONS]
    for nom in choisies:
        etape(f"Envoi vers {nom}")
        envoyer_v2(nom, int(conf.get("SPORT_AUTO_NOMBRE", AUTO_GARMIN)), reessayer=True)
    if not choisies:
        print("\nAucune destination automatique (SPORT_AUTO_DESTINATIONS dans sport.env) : rien n'est envoyé.")
    print("\nMode automatique terminé.")


def cmd_refaire():
    """sport.py refaire garmin : retire de Garmin Connect les activités envoyées par l'ancienne version
    (outils/historique-v1/envoyes-garmin.csv : départ et distance de chacune), pour les remplacer par activites-v2.

    Une activité n'est retirée que si Garmin en a exactement une qui démarre à moins d'une minute et dont la
    distance correspond (2 % ou 20 m) : les autres sont signalées et laissées. Journal : historique-v1/.retirees-garmin.txt"""
    if sys.argv[2:3] != ["garmin"]:
        sys.exit("Usage : sport.py refaire garmin")
    utiliser_venv()
    from datetime import datetime, timedelta, timezone

    liste = PROJET / "outils" / "historique-v1" / "envoyes-garmin.csv"
    journal = liste.parent / ".retirees-garmin.txt"
    deja = set(journal.read_text().split()) if journal.exists() else set()
    with liste.open() as f:
        a_retirer = [l for l in csv.DictReader(f) if l["empreinte"] not in deja and l["debut_utc"]]
    print(f"{len(a_retirer)} activité(s) de l'ancienne version à retirer de Garmin")
    if not a_retirer:
        return
    api = connexion_garmin()
    jours = sorted({datetime.fromisoformat(l["debut_utc"]).date() for l in a_retirer})
    sur_garmin, debut = [], jours[0] - timedelta(days=1)
    while debut <= jours[-1]:  # par tranches de 30 jours
        fin = debut + timedelta(days=30)
        sur_garmin += api.get_activities_by_date(debut.isoformat(), fin.isoformat()) or []
        debut = fin + timedelta(days=1)
    compte = Counter()
    with journal.open("a") as j:
        for l in a_retirer:
            depart = datetime.fromisoformat(l["debut_utc"])
            distance = float(l["distance_m"] or 0)
            candidates = [a for a in sur_garmin
                          if abs((datetime.fromisoformat(a["startTimeGMT"]).replace(tzinfo=timezone.utc) - depart).total_seconds()) <= 60
                          and abs((a.get("distance") or 0) - distance) <= max(20, 0.02 * distance)]
            if len(candidates) != 1:
                compte["introuvables ou ambiguës, laissées"] += 1
                continue
            api.delete_activity(candidates[0]["activityId"])
            sur_garmin.remove(candidates[0])
            j.write(l["empreinte"] + "\n")
            j.flush()
            compte["retirées de Garmin"] += 1
            time.sleep(1)
    print(", ".join(f"{n} {s}" for s, n in compte.items()))


def ecrire_config(**valeurs):
    """Ajoute ou remplace des clés dans sport.env (lisible par vous seul)."""
    lignes = CONFIG.read_text().splitlines() if CONFIG.exists() else []
    lignes = [l for l in lignes if l.split("=", 1)[0].strip() not in valeurs]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text("\n".join(lignes + [f"{k}={v}" for k, v in valeurs.items()]) + "\n")
    CONFIG.chmod(0o600)


def copier_sommeil():
    """Le sommeil n'existe pas en FIT importable : activites-v2/sommeil/ en garde le CSV (une nuit par ligne)."""
    if (SANTE / "sommeil.csv").exists() and V2.exists():
        (V2 / "sommeil").mkdir(exist_ok=True)
        shutil.copyfile(SANTE / "sommeil.csv", V2 / "sommeil" / "sommeil.csv")
        print(f"  sommeil : {V2 / 'sommeil' / 'sommeil.csv'}")


def cmd_preparer():
    """sport.py preparer : conversion précise (v2) de toutes les sources vers activites-v2/, puis audit.

    Voir analyse/ANALYSE.md : un lecteur par source (heure, fuseau, durée, sport), regroupement de la même sortie
    vue par plusieurs applis, meilleur exemplaire, filtres, un FIT par activité. L'ancienne version de
    activites-v2/ est déplacée dans a-supprimer/ (jamais effacée)."""
    utiliser_venv()
    import statistics

    import audit
    import regrouper
    import seances

    print("Lecture de toutes les sources…", flush=True)
    racines = list(sources().values())
    seances.DOSSIER_MONTRE = ARCHIVE.name
    toutes = seances.lire_sources(racines, EXCLUS_PROJET)
    print("Regroupement, choix du meilleur exemplaire, filtres…", flush=True)
    gardees, ecartees = regrouper.preparer(toutes)
    if V2.exists():
        ancienne = CORBEILLE / time.strftime("%Y-%m-%d_%H%M") / V2.name
        ancienne.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(V2, ancienne)
        print(f"  ancienne version déplacée dans {ancienne}")
    print("Écriture des FIT…", flush=True)
    regrouper.ecrire(gardees, ecartees, V2)
    copier_sommeil()
    print("Contrôle des fichiers produits…", flush=True)
    index, problemes, vitesses = audit.auditer_v2(V2)
    print(f"  {len(index)} activités, {len(problemes)} problème(s)")
    for p in problemes[:10]:
        print(f"    {p[0]} : {p[1]} {p[2]}")
    for sport, v in sorted(vitesses.items()):
        print(f"  {sport:9} vitesse médiane {statistics.median(v):.1f} km/h ({len(v)} activités avec distance)")
    print(f"Détail : {V2 / 'index.csv'} (retenues) et {V2 / 'ecartees.csv'} (écartées, avec la raison)")


def main():
    commandes = {
        "etat": cmd_etat,
        "montre": cmd_montre,
        "envoyer": cmd_envoyer,
        "sante": cmd_sante,
        "auto": cmd_auto,
        "refaire": cmd_refaire,
        "preparer": cmd_preparer,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commandes:
        sys.exit(__doc__)
    if sys.argv[1] in COMMANDES_EXCLUSIVES:
        # Deux tris ou deux envois en même temps se marcheraient dessus (terminal, interface web, service montre)
        verrou = open(VERROU, "w")  # gardé ouvert jusqu'à la fin du processus
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Une autre commande sport.py est déjà en cours (terminal ou interface web) : attendez qu'elle finisse.")
    commandes[sys.argv[1]]()


if __name__ == "__main__":
    main()

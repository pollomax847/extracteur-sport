"""Destinations d'envoi des activités de activites-v2/ (utilisé par sport.py et web.py).

Chaque destination envoie un FIT et répond « envoye », « deja » (le service l'a déjà) ou « refuse » ; elle lève
Limite quand le service demande d'attendre. Le journal outils/.envoyes-<destination>-v2.txt mémorise le résultat
de chaque fichier : relancer reprend là où l'on s'était arrêté.

- garmin    : Garmin Connect (garminconnect, jetons dans ~/.config/sport/garmin-jetons/)
- strava    : Strava (API officielle ; application Strava + autorisation OAuth, jetons dans ~/.config/sport/strava.json)
- intervals : intervals.icu (clé d'API personnelle INTERVALS_API_KEY ; refuse lui-même les doublons)
- runalyze  : Runalyze (jeton « Personal API » RUNALYZE_TOKEN ; chemin d'envoi non publié, vérifié au premier envoi)
- endurain  : Endurain (clé d'API ENDURAIN_API_KEY, déjà utilisée pour la montre)
- smashrun  : Smashrun (jeton personnel SMASHRUN_TOKEN, 12 semaines ; course à pied seulement)
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NAVIGATEUR = "Mozilla/5.0 (X11; Linux x86_64) sport.py"  # intervals.icu filtre les clients sans user-agent


class Limite(Exception):
    """Le service limite le nombre d'envois : arrêter et reprendre plus tard."""


class NonConfiguree(Exception):
    """Identifiants manquants : à renseigner dans l'interface ou sport.env."""


def _multipart(fichier, champs=None, nom_champ="file"):
    limite = uuid.uuid4().hex
    morceaux = []
    for cle, valeur in (champs or {}).items():
        morceaux.append(f"--{limite}\r\nContent-Disposition: form-data; name=\"{cle}\"\r\n\r\n{valeur}\r\n".encode())
    morceaux.append(f"--{limite}\r\nContent-Disposition: form-data; name=\"{nom_champ}\"; filename=\"{fichier.name}\"\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n".encode() + fichier.read_bytes() + b"\r\n")
    morceaux.append(f"--{limite}--\r\n".encode())
    return b"".join(morceaux), f"multipart/form-data; boundary={limite}"


def _requete(url, methode="GET", donnees=None, entetes=None, delai=60):
    req = urllib.request.Request(url, data=donnees, method=methode, headers={"User-Agent": NAVIGATEUR, **(entetes or {})})
    try:
        with urllib.request.urlopen(req, timeout=delai) as resp:
            corps = resp.read()
            return resp.status, (json.loads(corps) if corps and corps[:1] in b"[{" else corps)
    except urllib.error.HTTPError as e:
        corps = e.read()
        if e.code == 429:
            raise Limite(f"{url.split('/')[2]} : trop d'envois, réessayer plus tard") from e
        try:
            return e.code, json.loads(corps)
        except ValueError:
            return e.code, corps.decode("utf-8", "replace")[:300]


# ----------------------------------------------------------------------------- intervals.icu

class Intervals:
    nom, titre = "intervals", "intervals.icu"

    def __init__(self, conf):
        self.cle = conf.get("INTERVALS_API_KEY")
        if not self.cle:
            raise NonConfiguree("clé d'API intervals.icu absente (Paramètres → Developer Settings sur intervals.icu)")
        import base64

        self.auth = "Basic " + base64.b64encode(f"API_KEY:{self.cle}".encode()).decode()

    def verifier(self):
        code, reponse = _requete("https://intervals.icu/api/v1/athlete/0", entetes={"Authorization": self.auth})
        if code != 200:
            raise NonConfiguree(f"intervals.icu refuse la clé ({code})")
        return reponse.get("name") or reponse.get("id")

    def envoyer(self, fichier, activite):
        corps, type_ = _multipart(fichier)
        url = "https://intervals.icu/api/v1/athlete/0/activities?" + urllib.parse.urlencode({"external_id": activite["fichier"]})
        code, reponse = _requete(url, "POST", corps, {"Authorization": self.auth, "Content-Type": type_})
        if code == 201:
            return "envoye"
        if code == 200:  # rien de créé : fichier déjà présent (empreinte du contenu)
            return "deja"
        return f"refuse : {code} {str(reponse)[:120]}"


# ----------------------------------------------------------------------------- Strava

class Strava:
    """API officielle. Il faut créer une application sur https://www.strava.com/settings/api
    (« Authorization Callback Domain » : localhost), puis l'autoriser une fois depuis l'interface (bouton Strava)."""
    nom, titre = "strava", "Strava"
    TYPES_STRAVA = {"running": "Run", "walking": "Walk", "hiking": "Hike", "cycling": "Ride", "swimming": "Swim",
                    "training": "WeightTraining"}

    def __init__(self, conf, jetons_fichier):
        self.client_id, self.secret = conf.get("STRAVA_CLIENT_ID"), conf.get("STRAVA_CLIENT_SECRET")
        self.fichier = jetons_fichier
        if not (self.client_id and self.secret):
            raise NonConfiguree("application Strava absente (Client ID et Secret de strava.com/settings/api)")
        if not self.fichier.exists():
            raise NonConfiguree("Strava pas encore autorisé : bouton « Autoriser Strava » dans l'interface")
        self.jetons = json.loads(self.fichier.read_text())

    @staticmethod
    def lien_autorisation(client_id, retour):
        return "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": retour, "response_type": "code",
            "approval_prompt": "auto", "scope": "read,activity:write"})

    @staticmethod
    def echanger_code(client_id, secret, code, fichier):
        corps = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret, "code": code,
                                        "grant_type": "authorization_code"}).encode()
        statut, reponse = _requete("https://www.strava.com/oauth/token", "POST", corps,
                                   {"Content-Type": "application/x-www-form-urlencoded"})
        if statut != 200:
            raise NonConfiguree(f"Strava refuse l'autorisation ({statut} {str(reponse)[:120]})")
        fichier.write_text(json.dumps(reponse))
        fichier.chmod(0o600)
        return (reponse.get("athlete") or {}).get("firstname", "")

    def _jeton(self):
        if self.jetons.get("expires_at", 0) - 300 < time.time():  # jeton de 6 h : renouvelé avant expiration
            corps = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.secret,
                                            "grant_type": "refresh_token",
                                            "refresh_token": self.jetons["refresh_token"]}).encode()
            statut, reponse = _requete("https://www.strava.com/oauth/token", "POST", corps,
                                       {"Content-Type": "application/x-www-form-urlencoded"})
            if statut != 200:
                raise NonConfiguree(f"Strava : renouvellement refusé ({statut}), réautoriser depuis l'interface")
            self.jetons.update(reponse)
            self.fichier.write_text(json.dumps(self.jetons))
        return self.jetons["access_token"]

    def verifier(self):
        statut, reponse = _requete("https://www.strava.com/api/v3/athlete", entetes={"Authorization": f"Bearer {self._jeton()}"})
        if statut != 200:
            raise NonConfiguree(f"Strava refuse l'accès ({statut})")
        return reponse.get("firstname")

    def envoyer(self, fichier, activite):
        champs = {"data_type": "fit", "external_id": activite["fichier"]}
        type_strava = self.TYPES_STRAVA.get(activite["sport"])
        if type_strava:
            champs["sport_type"] = type_strava
        corps, type_ = _multipart(fichier, champs)
        statut, reponse = _requete("https://www.strava.com/api/v3/uploads", "POST", corps,
                                   {"Authorization": f"Bearer {self._jeton()}", "Content-Type": type_})
        if statut not in (200, 201):
            return f"refuse : {statut} {str(reponse)[:120]}"
        # Strava traite le fichier en arrière-plan : on attend le verdict (doublon, erreur ou activité créée)
        for _ in range(20):
            if reponse.get("activity_id"):
                return "envoye"
            erreur = reponse.get("error") or ""
            if "duplicate" in erreur.lower():
                return "deja"
            if erreur:
                return f"refuse : {erreur[:120]}"
            time.sleep(2)
            statut, reponse = _requete(f"https://www.strava.com/api/v3/uploads/{reponse['id']}",
                                       entetes={"Authorization": f"Bearer {self._jeton()}"})
        return "envoye"  # toujours en traitement chez Strava : il finira seul


# ----------------------------------------------------------------------------- Runalyze

class Runalyze:
    """Jeton « Personal API » (runalyze.com, Réglages → Personal API, avec date d'expiration).
    Runalyze ne publie pas le chemin d'envoi : il est vérifié au premier envoi, et la destination est désactivée
    proprement si le serveur ne l'accepte pas."""
    nom, titre = "runalyze", "Runalyze"
    URL = "https://runalyze.com/api/v1"

    def __init__(self, conf):
        self.jeton = conf.get("RUNALYZE_TOKEN")
        if not self.jeton:
            raise NonConfiguree("jeton Runalyze absent (Réglages → Personal API sur runalyze.com)")

    def verifier(self):
        statut, _ = _requete(f"{self.URL}/ping", entetes={"token": self.jeton})
        if statut != 200:
            raise NonConfiguree(f"Runalyze refuse le jeton ({statut})")
        return "jeton valide"

    def envoyer(self, fichier, activite):
        corps, type_ = _multipart(fichier)
        statut, reponse = _requete(f"{self.URL}/activities/uploads", "POST", corps, {"token": self.jeton, "Content-Type": type_})
        if statut in (200, 201, 202):
            self.reussis = getattr(self, "reussis", 0) + 1
            texte = str(reponse).lower()
            return "deja" if "duplicate" in texte or "already" in texte else "envoye"
        if statut in (404, 405):
            raise NonConfiguree("Runalyze n'accepte pas l'envoi de fichiers avec ce jeton (chemin non ouvert)")
        if statut == 403:
            # un compte gratuit envoie bien : un 403 après des envois réussis est un blocage temporaire
            # (vu le 26/10/2026 après 514 envois en 80 min, levé quelques heures plus tard)
            if getattr(self, "reussis", 0):
                raise Limite(f"Runalyze bloque temporairement les envois (403 {str(reponse)[:120]})")
            raise NonConfiguree(f"Runalyze refuse l'envoi avec ce jeton (403 {str(reponse)[:120]})")
        return f"refuse : {statut} {str(reponse)[:120]}"


# ----------------------------------------------------------------------------- Smashrun

class Smashrun:
    """Jeton personnel : api.smashrun.com/v1/explorer, taper « client » en haut, « Connect », copier le jeton. Il expire
    au bout de 12 semaines (pas de renouvellement pour ces jetons). 250 requêtes par heure ; course à pied seulement
    (sport.py ne lui propose que les courses). Une course avec trace part en FIT (champ « file », vérifié le
    27/10/2026) ; sans trace, Smashrun refuse le FIT (« no data to import ») : elle part en JSON (départ, durée,
    distance), à l'heure de Paris faute de mieux. Sans distance, Smashrun ne l'accepte pas."""
    nom, titre = "smashrun", "Smashrun"
    URL = "https://api.smashrun.com/v1"

    @staticmethod
    def lien_autorisation(retour):
        """Connexion Smashrun avec le client public « client » (flux implicite) : le jeton revient dans le fragment de
        l'adresse de retour (#access_token=…), lu par la page de retour de l'interface."""
        return "https://secure.smashrun.com/oauth2/authenticate?" + urllib.parse.urlencode({
            "client_id": "client", "scope": "read_activity write_activity", "response_type": "token",
            "state": uuid.uuid4().hex, "redirect_uri": retour}, quote_via=urllib.parse.quote)

    def __init__(self, conf):
        self.jeton = conf.get("SMASHRUN_TOKEN")
        if not self.jeton:
            raise NonConfiguree("jeton Smashrun absent (api.smashrun.com/v1/explorer : « client », puis « Connect »)")

    def _entetes(self, **autres):
        return {"Authorization": f"Bearer {self.jeton}", **autres}

    def verifier(self):
        statut, reponse = _requete(f"{self.URL}/my/userinfo", entetes=self._entetes())
        if statut != 200:
            raise NonConfiguree(f"Smashrun refuse le jeton ({statut}) : il expire au bout de 12 semaines, en recopier un")
        return (reponse.get("userName") if isinstance(reponse, dict) else None) or "jeton valide"

    def presentes(self):
        """[(départ UTC, durée s)] des courses déjà sur le compte, quelle que soit l'appli qui les y a mises (Garmin,
        Strava… : 567 courses le 27/10/2026, dont 209 que sport.py aurait renvoyées en double)."""
        statut, liste = _requete(f"{self.URL}/my/activities/search", entetes=self._entetes())
        if statut != 200 or not isinstance(liste, list):
            raise NonConfiguree(f"Smashrun : liste des courses illisible ({statut})")
        return [(datetime.fromisoformat(a["startDateTimeLocal"]).astimezone(timezone.utc), float(a.get("duration") or 0))
                for a in liste if a.get("startDateTimeLocal")]

    def envoyer(self, fichier, activite):
        if activite.get("trace") == "True":
            corps, type_ = _multipart(fichier)
        else:
            debut = datetime.fromisoformat(activite["debut_utc"]).astimezone(ZoneInfo("Europe/Paris"))
            corps, type_ = json.dumps({
                "externalId": activite["fichier"], "activityType": "running",
                "startDateTimeLocal": debut.isoformat(timespec="seconds"),
                "distance": float(activite["distance_km"]), "duration": round(float(activite["duree_min"]) * 60),
            }).encode(), "application/json"
        statut, reponse = _requete(f"{self.URL}/my/activities/", "POST", corps, self._entetes(**{"Content-Type": type_}))
        if statut in (200, 201, 202):
            return "envoye"
        if statut == 401:
            raise NonConfiguree("Smashrun : jeton expiré (12 semaines), en recopier un depuis api.smashrun.com/v1/explorer")
        return f"refuse : {statut} {str(reponse)[:120]}"


# ----------------------------------------------------------------------------- Endurain

class Endurain:
    nom, titre = "endurain", "Endurain"

    def __init__(self, conf):
        self.url, self.cle = conf["ENDURAIN_URL"], conf.get("ENDURAIN_API_KEY")
        if not self.cle:
            raise NonConfiguree("clé d'API Endurain absente")

    def verifier(self):
        statut, _ = _requete(f"{self.url}/api/v1/about")
        if statut != 200:
            raise NonConfiguree("Endurain ne répond pas")
        return "en marche"

    def envoyer(self, fichier, activite):
        corps, type_ = _multipart(fichier)
        statut, reponse = _requete(f"{self.url}/api/v1/activities/upload", "POST", corps,
                                   {"X-API-Key": self.cle, "Content-Type": type_,
                                    "Idempotency-Key": f"v2-{activite['fichier']}"})
        if statut in (200, 201, 202):
            return "envoye"  # Endurain écarte lui-même les doublons à l'import
        return f"refuse : {statut} {str(reponse)[:120]}"

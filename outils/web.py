#!/usr/bin/env python3
"""Interface web de relais.py (conteneur « relais », à côté d'Endurain).

Chaque bouton lance une commande relais.py et affiche sa sortie en direct. La connexion à Garmin Connect
(mot de passe puis code MFA) se fait ici : les jetons sont enregistrés comme avec le terminal, et
« envoyer garmin » les réutilise. Les identifiants Endurain (pour la santé et l'import automatique)
sont enregistrés dans sport.env.

Mode automatique : « relais.py auto » est lancé chaque jour à SPORT_AUTO_HEURE (« 04:00 » par défaut,
« non » pour le désactiver), si aucune autre commande ne tourne. Port : SPORT_WEB_PORT (8090 par défaut).
"""
import csv
import json
import urllib.parse
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import relais

RELAIS_PY = Path(__file__).resolve().parent / "relais.py"
PAGE = Path(__file__).resolve().parent / "web.html"
PORT = int(os.environ.get("SPORT_WEB_PORT", "8090"))
ENDURAIN_PUBLIC = os.environ.get("ENDURAIN_PUBLIC_URL", "http://localhost:8080")
HEURE_AUTO = os.environ.get("SPORT_AUTO_HEURE", "04:00")
PUBLIC = os.environ.get("SPORT_PUBLIC_URL", "http://localhost:8090")  # adresse de retour des autorisations
# Smashrun rend le jeton dans le fragment de l'adresse (#access_token=…), que seul le navigateur voit : cette page le lit
# et l'enregistre comme le formulaire (vérifié auprès de Smashrun). Le « + » du jeton est gardé tel quel.
RETOUR_SMASHRUN = """<!doctype html><meta charset="utf-8"><title>Smashrun</title><p>Connexion à Smashrun…</p>
<script>
const champs = Object.fromEntries(location.hash.slice(1).split("&").map((p) => p.split("=").map(decodeURIComponent)));
const fin = (r) => location.replace("/?smashrun=" + encodeURIComponent(r));
if (!champs.access_token) fin(champs.error || new URLSearchParams(location.search).get("error") || "autorisation refusée");
else fetch("/api/destination", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nom: "smashrun", champs: { SMASHRUN_TOKEN: champs.access_token } }) })
  .then((r) => r.json().then((d) => fin(r.ok ? "ok" : d.erreur || "refusé")))
  .catch((e) => fin(String(e)));
</script>"""


class Tache:
    """La commande en cours (une seule à la fois) et sa sortie."""

    def __init__(self):
        self.verrou = threading.Lock()
        self.proc = None
        self.commande = ""
        self.lignes = []
        self.code = None

    def en_cours(self):
        return self.proc is not None and self.proc.poll() is None

    def lancer(self, args):
        with self.verrou:
            if self.en_cours():
                return False
            self.commande, self.lignes, self.code = " ".join(args), [], None
            self.proc = subprocess.Popen([sys.executable, "-u", str(RELAIS_PY), *args], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._lire, args=(self.proc,), daemon=True).start()
        return True

    def _lire(self, proc):
        for ligne in proc.stdout:
            self.lignes.append(ligne.rstrip("\n"))
        self.code = proc.wait()

    def arreter(self):
        if self.en_cours():
            self.proc.terminate()


TACHE = Tache()
CONNEXION_GARMIN = {}  # session en attente du code MFA


def prochain_auto(maintenant=None):
    if HEURE_AUTO.lower() in ("", "non", "off"):
        return None
    maintenant = maintenant or datetime.now()
    heure, minute = map(int, HEURE_AUTO.split(":"))
    prochain = maintenant.replace(hour=heure, minute=minute, second=0, microsecond=0)
    return prochain if prochain > maintenant else prochain + timedelta(days=1)


def planificateur():
    """Lance « auto » une fois par jour à l'heure prévue (si une commande tourne déjà, on attend le lendemain)."""
    while (prochain := prochain_auto()) is not None:
        time.sleep(max(1, (prochain - datetime.now()).total_seconds()))
        TACHE.lancer(["auto"])
        time.sleep(61)


def enregistrer_identifiants_endurain(utilisateur, mot_de_passe):
    """Vérifie les identifiants auprès d'Endurain puis les écrit dans sport.env (lisible par vous seul)."""
    conf = relais.config()
    conf.update(ENDURAIN_USER=utilisateur, ENDURAIN_PASSWORD=mot_de_passe)
    relais.Endurain(conf)._connexion()  # lève une erreur si Endurain refuse
    lignes = [l for l in relais.CONFIG.read_text().splitlines()
              if not l.startswith(("ENDURAIN_USER=", "ENDURAIN_PASSWORD="))] if relais.CONFIG.exists() else []
    relais.CONFIG.write_text("\n".join(lignes + [f"ENDURAIN_USER={utilisateur}", f"ENDURAIN_PASSWORD={mot_de_passe}"]) + "\n")
    relais.CONFIG.chmod(0o600)
    return {"ok": True}


def etat():
    montre = relais.trouver_montre()
    conf = relais.config()
    bibliotheque = []
    if relais.V2.exists() and (relais.V2 / "index.csv").exists():
        with (relais.V2 / "index.csv").open() as f:
            bibliotheque = list(csv.DictReader(f))
    destinations = {}
    for nom in relais.DESTINATIONS:
        envois = relais.etat_envois(nom)
        proposees = [l for l in bibliotheque if relais.proposee(nom, l)]  # Smashrun : courses avec distance
        # statuts des seules activités proposées (le journal garde aussi d'anciens noms et des activités non proposées)
        statuts = Counter(envois[l["fichier"]].split(" :")[0] for l in proposees if l["fichier"] in envois)
        destinations[nom] = {"configuree": destination_configuree(nom, conf), "envoye": statuts.get("envoye", 0),
                             "deja": statuts.get("deja", 0), "refuse": statuts.get("refuse", 0), "total": len(proposees),
                             # activités de la bibliothèque actuelle jamais traitées (le journal garde aussi d'anciens noms)
                             "restantes": sum(l["fichier"] not in envois for l in proposees)}
    index = relais.V2 / "index.csv"
    return {
        "bibliotheque": {"total": len(bibliotheque),
                         "categories": dict(Counter(l["categorie"] for l in bibliotheque).most_common()),
                         "gps": sum(l["gps"] == "True" for l in bibliotheque),
                         "annees": [min(l["debut_utc"][:4] for l in bibliotheque), max(l["debut_utc"][:4] for l in bibliotheque)] if bibliotheque else None,
                         "convertie_le": datetime.fromtimestamp(index.stat().st_mtime).strftime("%d/%m/%Y à %H:%M") if index.exists() else None},
        "destinations": destinations,
        "montre": str(montre) if montre else None,
        "endurain": relais.endurain_actif(conf),
        "endurain_url": ENDURAIN_PUBLIC,
        "cle_api": bool(conf.get("ENDURAIN_API_KEY")),
        "endurain_identifiants": bool(conf.get("ENDURAIN_PASSWORD")),
        "prochain_auto": prochain.strftime("%d/%m à %H:%M") if (prochain := prochain_auto()) else None,
        "garmin_connecte": relais.JETONS_GARMIN.is_dir() and any(relais.JETONS_GARMIN.iterdir()),
        "commande": TACHE.commande if TACHE.en_cours() else None,
    }


def sante():
    """Valeurs retenues par « relais.py sante » (sante/*.csv), une ligne [date, valeur…] par jour, pour la section Santé."""
    def lire(nom, colonnes):
        fichier = relais.SANTE / nom
        if not fichier.exists():
            return []
        with fichier.open() as f:
            return [[l["date"], *(float(l[c]) if l.get(c) else None for c in colonnes)] for l in csv.DictReader(f)]

    return {"pas": lire("pas.csv", ["pas"]), "poids": lire("poids.csv", ["kg"]), "sommeil": lire("sommeil.csv", ["total_h"]),
            "montre": lire("montre.csv", ["pas", "fc_repos", "stress_moy"])}


def destination_configuree(nom, conf):
    if nom == "garmin":
        return relais.JETONS_GARMIN.is_dir() and any(relais.JETONS_GARMIN.iterdir())
    if nom == "strava":
        return bool(conf.get("STRAVA_CLIENT_ID")) and relais.JETONS_STRAVA.exists()
    return bool(conf.get({"intervals": "INTERVALS_API_KEY", "runalyze": "RUNALYZE_TOKEN",
                          "endurain": "ENDURAIN_API_KEY", "smashrun": "SMASHRUN_TOKEN"}[nom]))


def configurer_destination(nom, champs):
    """Enregistre les identifiants d'une destination après les avoir vérifiés auprès du service."""
    import destinations

    cles = {"intervals": ["INTERVALS_API_KEY"], "runalyze": ["RUNALYZE_TOKEN"], "smashrun": ["SMASHRUN_TOKEN"],
            "strava": ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]}.get(nom)
    if not cles:
        raise ValueError(f"rien à configurer pour {nom}")
    valeurs = {c: (champs.get(c) or "").strip() for c in cles}
    if not all(valeurs.values()):
        raise ValueError("tous les champs sont nécessaires")
    if nom == "strava":  # vérifié à l'autorisation (bouton suivant)
        relais.ecrire_config(**valeurs)
        return {"ok": True, "autoriser": "/strava/autoriser"}
    conf = relais.config()
    conf.update(valeurs)
    cible = {"intervals": destinations.Intervals, "runalyze": destinations.Runalyze,
             "smashrun": destinations.Smashrun}[nom](conf)
    qui = cible.verifier()  # lève une erreur si le service refuse
    relais.ecrire_config(**valeurs)
    return {"ok": True, "compte": str(qui)}


def connexion_garmin(email, mot_de_passe):
    from garminconnect import Garmin

    relais.JETONS_GARMIN.mkdir(parents=True, exist_ok=True)
    relais.JETONS_GARMIN.chmod(0o700)
    api = Garmin(email, mot_de_passe, return_on_mfa=True)
    statut, etat_client = api.login(str(relais.JETONS_GARMIN))
    if statut == "needs_mfa":
        CONNEXION_GARMIN.update(api=api, etat=etat_client)
        return {"mfa": True}
    return {"ok": True}


def code_mfa(code):
    if not CONNEXION_GARMIN:
        raise ValueError("aucune connexion en attente : recommencez avec l'e-mail et le mot de passe")
    api = CONNEXION_GARMIN.pop("api")
    api.resume_login(CONNEXION_GARMIN.pop("etat"), code)
    api.client.dump(str(relais.JETONS_GARMIN))
    return {"ok": True}


def arguments(demande):
    """Traduit un bouton de l'interface en arguments de relais.py (rien d'autre n'est accepté)."""
    commande = demande.get("commande")
    if commande in ("montre", "etat", "sante", "auto", "preparer", "satellites"):
        return [commande]
    if commande == "convertir":
        chemin = (demande.get("chemin") or "").strip()
        if not chemin:
            raise ValueError("indiquez le dossier à convertir")
        return ["convertir", chemin]
    if commande == "envoyer":
        cible = demande.get("destination") or "garmin"
        if cible not in relais.DESTINATIONS:
            raise ValueError(f"destination inconnue : {cible}")
        args = ["envoyer", cible] + ([str(int(demande["nombre"]))] if demande.get("nombre") else [])  # tout par défaut
        return args + (["--reessayer"] if demande.get("reessayer") else [])
    raise ValueError(f"commande inconnue : {commande}")


class Gestionnaire(BaseHTTPRequestHandler):
    def _repondre(self, code, contenu, type_="application/json"):
        corps = contenu if isinstance(contenu, bytes) else json.dumps(contenu).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{type_}; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _rediriger(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._repondre(200, PAGE.read_bytes(), "text/html")
        elif self.path == "/api/etat":
            self._repondre(200, etat())
        elif self.path == "/api/sante":
            self._repondre(200, sante())
        elif self.path == "/strava/autoriser":
            import destinations

            client = relais.config().get("STRAVA_CLIENT_ID")
            if not client:
                return self._repondre(400, {"erreur": "Client ID Strava absent"})
            self._rediriger(destinations.Strava.lien_autorisation(client, f"{PUBLIC}/strava/retour"))
        elif self.path.startswith("/strava/retour"):
            import destinations

            requete = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            conf = relais.config()
            try:
                if "code" not in requete:
                    raise ValueError(requete.get("error", ["autorisation refusée"])[0])
                destinations.Strava.echanger_code(conf["STRAVA_CLIENT_ID"], conf["STRAVA_CLIENT_SECRET"],
                                                  requete["code"][0], relais.JETONS_STRAVA)
                self._rediriger("/?strava=ok")
            except Exception as e:
                self._rediriger("/?strava=" + urllib.parse.quote(str(e)[:200]))
        elif self.path == "/smashrun/autoriser":
            import destinations

            self._rediriger(destinations.Smashrun.lien_autorisation(f"{PUBLIC}/smashrun/retour"))
        elif self.path.startswith("/smashrun/retour"):
            self._repondre(200, RETOUR_SMASHRUN.encode(), "text/html")
        elif self.path.startswith("/api/journal"):
            depuis = int(self.path.partition("depuis=")[2] or 0)
            self._repondre(200, {"lignes": TACHE.lignes[depuis:], "total": len(TACHE.lignes),
                                 "en_cours": TACHE.en_cours(), "commande": TACHE.commande, "code": TACHE.code})
        else:
            self._repondre(404, {"erreur": "introuvable"})

    def do_POST(self):
        try:
            demande = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            if self.path == "/api/lancer":
                if not TACHE.lancer(arguments(demande)):
                    return self._repondre(409, {"erreur": "une commande est déjà en cours"})
                self._repondre(200, {"ok": True})
            elif self.path == "/api/arreter":
                TACHE.arreter()
                self._repondre(200, {"ok": True})
            elif self.path == "/api/garmin/connexion":
                self._repondre(200, connexion_garmin(demande["email"], demande["mot_de_passe"]))
            elif self.path == "/api/garmin/mfa":
                self._repondre(200, code_mfa(demande["code"]))
            elif self.path == "/api/destination":
                self._repondre(200, configurer_destination(demande["nom"], demande.get("champs") or {}))
            elif self.path == "/api/endurain/identifiants":
                self._repondre(200, enregistrer_identifiants_endurain(demande["utilisateur"], demande["mot_de_passe"]))
            else:
                self._repondre(404, {"erreur": "introuvable"})
        except Exception as e:  # message affiché dans l'interface
            self._repondre(400, {"erreur": str(e)})

    def log_message(self, *_):
        pass  # pas de journal d'accès : l'interface interroge le serveur chaque seconde


if __name__ == "__main__":
    print(f"Interface relais.py sur le port {PORT} ; mode automatique : {prochain_auto() or 'désactivé'}")
    threading.Thread(target=planificateur, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Gestionnaire).serve_forever()

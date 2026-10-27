"""Audit de la bibliothèque activites-uniques/ : mesures de chaque activité, contrôles de vraisemblance
et comparaisons entre sources (utilisé par « sport.py audit »).

Produit dans analyse/ :
- activites.csv : une ligne par activité (sport, départ, durée, distance, points, GPS, cardio, appli d'origine…) ;
- anomalies.csv : une ligne par problème détecté, avec la règle et le détail ;
- rapport.md    : synthèse chiffrée par source et par règle.
"""
import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import fitdecode

# Vitesse moyenne plausible (km/h) par sport, et durée maximale (h)
VITESSES = {"running": (4, 25), "walking": (1.5, 8), "hiking": (1, 8), "cycling": (6, 60), "swimming": (0.5, 6)}
DUREE_MAX_H = 12


@dataclass
class Mesure:
    fichier: str
    famille: str  # dossier d'origine dans la bibliothèque (sport, smachrun, google-fit…)
    format: str
    appli: str  # créateur déclaré dans le fichier
    sport: str
    sport_nom_fichier: str  # sport lisible dans le nom du fichier (Google Fit, Runtastic…), s'il y en a un
    debut: datetime | None
    duree_s: float | None  # durée du résumé (tours / session)
    duree_points_s: float | None  # entre le premier et le dernier point
    distance_m: float | None
    points: int
    gps: bool
    cardio: bool

    def vitesse(self):
        duree = self.duree_s or self.duree_points_s
        return self.distance_m / duree * 3.6 if self.distance_m and duree else None


SPORTS_NOMS = {  # sport écrit dans les noms de fichiers → nom normalisé
    "marche à pied": "walking", "walking": "walking", "course à pied": "running", "running": "running",
    "vélo": "cycling", "cycling": "cycling", "biking": "cycling", "randonnée": "hiking", "hiking": "hiking",
    "natation": "swimming", "natation en piscine": "swimming", "swimming": "swimming",
    "course sur tapis roulant": "running", "musculation": "training", "autre": "other",
}


def _sport_nom(nom):
    bas = nom.lower()
    for libelle, sport in sorted(SPORTS_NOMS.items(), key=lambda x: -len(x[0])):
        if libelle in bas:
            return sport
    return ""


def _date(texte):
    try:
        d = datetime.fromisoformat(texte.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _xml(contenu, ext):
    tete = contenu[:3000].decode("utf-8", "replace")
    appli = (re.search(r'creator="([^"]+)"', tete) or re.search(r"<Name>([^<]+)</Name>", tete) or [None, ""])[1]
    if ext == ".tcx":
        sport = (re.search(rb'<Activity Sport="([^"]+)"', contenu) or [None, b""])[1].decode()
        tours = re.findall(rb"<TotalTimeSeconds>([\d.]+)</TotalTimeSeconds>", contenu)
        dist_tours = re.findall(rb"<Lap[^>]*>.*?<DistanceMeters>([\d.]+)</DistanceMeters>", contenu, re.S)
        points = contenu.count(b"<Trackpoint>")
        heures = re.findall(rb"<Time>([^<]+)</Time>", contenu)
        debut = re.search(rb"<Id>([^<]+)</Id>", contenu)
        debut = _date(debut[1].decode()) if debut else (_date(heures[0].decode()) if heures else None)
        distances = re.findall(rb"<DistanceMeters>([\d.]+)</DistanceMeters>", contenu)
        distance = sum(map(float, dist_tours)) or (max(map(float, distances)) if distances else None)
        gps = b"<LatitudeDegrees>" in contenu
        cardio = bool(re.search(rb"<HeartRateBpm>\s*<Value>[1-9]", contenu))
        duree = sum(map(float, tours)) or None
    else:
        trkseg = contenu.find(b"<trkseg")
        corps = contenu[trkseg:] if trkseg >= 0 else b""
        sport = (re.search(rb"<type>([^<]+)</type>", contenu) or [None, b""])[1].decode()
        points = corps.count(b"<trkpt")
        heures = re.findall(rb"<time>([^<]+)</time>", corps)
        debut = _date(heures[0].decode()) if heures else None
        distance, gps, duree = None, points > 0, None
        cardio = bool(re.search(rb"<(?:\w+:)?hr>[1-9]", corps))
    duree_points = None
    if len(heures) >= 2:
        a, b = _date(heures[0].decode()), _date(heures[-1].decode())
        duree_points = (b - a).total_seconds() if a and b else None
    return appli, sport, debut, duree, duree_points, distance, points, gps, cardio


def _fit(contenu):
    appli, sport, debut, duree, distance, points, gps, cardio = "", "", None, None, None, 0, False, False
    premier = dernier = None
    with fitdecode.FitReader(io.BytesIO(contenu), check_crc=fitdecode.CrcCheck.DISABLED) as lecteur:
        for m in lecteur:
            if not isinstance(m, fitdecode.FitDataMessage):
                continue
            if m.name == "file_id" and not appli:
                appli = f"{m.get_value('manufacturer', fallback='')} {m.get_value('garmin_product', fallback='') or m.get_value('product', fallback='')}".strip()
            elif m.name == "session" and not sport:
                sport = str(m.get_value("sport", fallback="") or "")
                debut = m.get_value("start_time", fallback=None)
                duree = m.get_value("total_timer_time", fallback=None) or m.get_value("total_elapsed_time", fallback=None)
                distance = m.get_value("total_distance", fallback=None)
            elif m.name == "record":
                points += 1
                t = m.get_value("timestamp", fallback=None)
                if isinstance(t, datetime):
                    premier = premier or t
                    dernier = t
                gps = gps or m.get_value("position_lat", fallback=None) is not None
                cardio = cardio or bool(m.get_value("heart_rate", fallback=None))
    duree_points = (dernier - premier).total_seconds() if premier and dernier else None
    return appli, sport, debut or premier, duree, duree_points, distance, points, gps, cardio


def normaliser(sport):
    s = (sport or "").strip().lower()
    return {"biking": "cycling", "other": "other", "generic": "other", "": "inconnu",
            "fitness_equipment": "training", "training": "training", "car": "car", "transport": "transport"}.get(s, s)


def mesurer(bibliotheque, rel):
    chemin = bibliotheque / rel
    contenu = chemin.read_bytes()
    ext = chemin.suffix.lower()
    appli, sport, debut, duree, duree_points, distance, points, gps, cardio = (
        _fit(contenu) if ext == ".fit" else _xml(contenu, ext))
    parties = Path(rel).parts
    return Mesure(rel, parties[1] if len(parties) > 2 else parties[0], ext, appli, normaliser(sport),
                  _sport_nom(Path(rel).name), debut, duree, duree_points, distance, points, gps, cardio)


def controler(mesures):
    """Contrôles de vraisemblance, un par une règle nommée."""
    anomalies = []

    def signaler(m, regle, detail=""):
        anomalies.append({"regle": regle, "fichier": m.fichier, "famille": m.famille, "sport": m.sport,
                          "debut_utc": m.debut.isoformat() if m.debut else "", "detail": detail})

    for m in mesures:
        duree = m.duree_s or m.duree_points_s
        if not m.debut:
            signaler(m, "sans date de départ")
        elif not 2010 <= m.debut.year <= 2026:
            signaler(m, "date hors période", m.debut.isoformat())
        if duree and duree > DUREE_MAX_H * 3600:
            signaler(m, f"durée > {DUREE_MAX_H} h", f"{duree / 3600:.1f} h")
        if m.points < 10:
            signaler(m, "sans vraie trace (< 10 points)", f"{m.points} point(s)")
        if m.duree_s and m.duree_points_s and m.points >= 10 and abs(m.duree_s - m.duree_points_s) > max(600, 0.5 * m.duree_s):
            signaler(m, "durée du résumé ≠ durée des points",
                     f"résumé {m.duree_s / 60:.0f} min, points {m.duree_points_s / 60:.0f} min")
        v = m.vitesse()
        if v is not None and m.sport in VITESSES:
            mini, maxi = VITESSES[m.sport]
            if not mini <= v <= maxi:
                signaler(m, "vitesse invraisemblable pour le sport", f"{m.sport} à {v:.1f} km/h")
        if m.sport in ("other", "inconnu") and m.sport_nom_fichier and m.sport_nom_fichier not in ("other",):
            signaler(m, "sport perdu (le nom du fichier le donne)", f"fichier : {m.sport_nom_fichier}")
        elif m.sport_nom_fichier and m.sport_nom_fichier not in ("other", m.sport) and m.sport not in ("other", "inconnu"):
            signaler(m, "sport différent du nom du fichier", f"fichier : {m.sport_nom_fichier}, contenu : {m.sport}")
        if m.gps and m.distance_m == 0:
            signaler(m, "GPS mais distance nulle")

    # Doublons restants : même durée et même distance (à 3 % près), départ décalé d'un nombre entier d'heures
    # (heure locale écrite comme UTC dans un des fichiers) ou à moins de 10 min (deux applis, départs différents)
    datees = sorted((m for m in mesures if m.debut and (m.duree_s or m.duree_points_s)), key=lambda m: m.debut)
    for i, a in enumerate(datees):
        for b in datees[i + 1:]:
            ecart = (b.debut - a.debut).total_seconds()
            if ecart > 3 * 3600 + 600:
                break
            da, db = a.duree_s or a.duree_points_s, b.duree_s or b.duree_points_s
            proche = lambda x, y: x and y and abs(x - y) <= 0.03 * max(x, y) + 30
            if not proche(da, db) or not (proche(a.distance_m, b.distance_m) or not (a.distance_m or b.distance_m)):
                continue
            heures = round(ecart / 3600)
            if heures >= 1 and abs(ecart - heures * 3600) <= 300:
                signaler(b, "doublon décalé d'un nombre entier d'heures (fuseau)",
                         f"{heures} h après {a.fichier} ({a.famille})")
            elif 120 < ecart <= 600:
                signaler(b, "doublon probable (départ < 10 min, même durée/distance)", f"{ecart / 60:.0f} min après {a.fichier}")
    return anomalies


def rapport(mesures, anomalies, dossier):
    dossier.mkdir(parents=True, exist_ok=True)
    with (dossier / "activites.csv").open("w", newline="") as f:
        champs = list(asdict(mesures[0]).keys()) + ["vitesse_kmh"]
        w = csv.DictWriter(f, champs)
        w.writeheader()
        for m in mesures:
            ligne = asdict(m)
            ligne["debut"] = m.debut.isoformat() if m.debut else ""
            ligne["vitesse_kmh"] = round(m.vitesse(), 2) if m.vitesse() else ""
            w.writerow(ligne)
    with (dossier / "anomalies.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, ["regle", "famille", "sport", "debut_utc", "fichier", "detail"])
        w.writeheader()
        w.writerows(anomalies)

    lignes = ["# Audit des activités", "", f"{len(mesures)} activités dans activites-uniques/.", ""]
    lignes += ["## Par famille de fichiers", "", "| Famille | Activités | Sports | Avec GPS | Avec cardio | Sans trace |",
               "|---|---|---|---|---|---|"]
    par_famille = defaultdict(list)
    for m in mesures:
        par_famille[m.famille].append(m)
    for famille, ms in sorted(par_famille.items(), key=lambda x: -len(x[1])):
        sports = ", ".join(f"{s} {n}" for s, n in Counter(m.sport for m in ms).most_common(4))
        lignes.append(f"| {famille} | {len(ms)} | {sports} | {sum(m.gps for m in ms)} | {sum(m.cardio for m in ms)} | "
                      f"{sum(m.points < 10 for m in ms)} |")
    lignes += ["", "## Anomalies par règle", "", "| Règle | Nombre | Familles les plus touchées |", "|---|---|---|"]
    par_regle = defaultdict(list)
    for a in anomalies:
        par_regle[a["regle"]].append(a)
    for regle, liste in sorted(par_regle.items(), key=lambda x: -len(x[1])):
        familles = ", ".join(f"{f} {n}" for f, n in Counter(a["famille"] for a in liste).most_common(3))
        lignes.append(f"| {regle} | {len(liste)} | {familles} |")
    lignes += ["", "## Vitesses médianes par sport (km/h)", ""]
    for sport in sorted({m.sport for m in mesures}):
        v = [m.vitesse() for m in mesures if m.sport == sport and m.vitesse()]
        if v:
            lignes.append(f"- {sport} : {median(v):.1f} km/h sur {len(v)} activités")
    (dossier / "rapport.md").write_text("\n".join(lignes) + "\n")
    return par_regle


def auditer_v2(dossier):
    """Contrôle de la bibliothèque v2 : chaque FIT est valide (CRC, outil officiel Garmin), conforme à l'index
    (sport, durée, distance), a assez de points pour Strava, et les vitesses sont plausibles."""
    from garmin_fit_sdk import Decoder, Stream

    with (dossier / "index.csv").open() as f:
        index = list(csv.DictReader(f))
    problemes, vitesses = [], defaultdict(list)
    for l in index:
        contenu = (dossier / l["fichier"]).read_bytes()
        decodeur = Decoder(Stream.from_byte_array(bytearray(contenu)))
        if not decodeur.is_fit() or not decodeur.check_integrity():
            problemes.append(("FIT invalide", l["fichier"], ""))
            continue
        # le contrôle d'intégrité lit le fichier jusqu'au bout : nouveau décodeur pour lire les messages
        messages, erreurs = Decoder(Stream.from_byte_array(bytearray(contenu))).read()
        if erreurs:
            problemes.append(("erreur de lecture FIT", l["fichier"], str(erreurs[0])[:80]))
        session = (messages.get("session_mesgs") or [{}])[0]
        points = len(messages.get("record_mesgs") or [])
        sport = {"generic": "other"}.get(str(session.get("sport")), str(session.get("sport")))
        if sport != l["sport"]:
            problemes.append(("sport du FIT ≠ index", l["fichier"], f"{sport} / {l['sport']}"))
        duree = session.get("total_timer_time") or 0
        if abs(duree / 60 - float(l["duree_min"])) > 1:
            problemes.append(("durée du FIT ≠ index", l["fichier"], f"{duree / 60:.1f} / {l['duree_min']} min"))
        if l["distance_km"] and abs((session.get("total_distance") or 0) / 1000 - float(l["distance_km"])) > 0.05:
            problemes.append(("distance du FIT ≠ index", l["fichier"], f"{(session.get('total_distance') or 0) / 1000:.3f} / {l['distance_km']} km"))
        if points < 10:
            problemes.append(("moins de 10 points (Strava : 0:00)", l["fichier"], str(points)))
        if l["vitesse_kmh"]:
            vitesses[l["sport"]].append(float(l["vitesse_kmh"]))
    return index, problemes, vitesses

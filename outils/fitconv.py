"""Écriture de fichiers FIT, le format natif de Garmin (utilisé par sport.py).

- lire_trace() + ecrire_fit() : convertit un TCX/GPX en FIT (≈ 20 fois plus léger, garde GPS, altitude,
  distance, cardio et cadence) ;
- seance_fit() : crée une activité FIT à partir d'un simple résumé (date, durée, distance, calories…).
Nécessite garmin-fit-sdk, installé dans l'environnement de sport.py.
"""
import io
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from garmin_fit_sdk import Encoder, Profile

MESG = Profile["mesg_num"]
SEMICERCLES = 2**31 / 180

# Noms de sport rencontrés (TCX, GPX, Runtastic, Runalyze…) → (sport, sub_sport) du profil FIT
SPORTS = {
    "running": ("running", "generic"), "run": ("running", "generic"), "course à pied": ("running", "generic"),
    "treadmill": ("running", "treadmill"), "trail": ("running", "trail"),
    "biking": ("cycling", "generic"), "cycling": ("cycling", "generic"), "vélo": ("cycling", "generic"),
    "mountain_biking": ("cycling", "mountain"), "indoor_cycling": ("cycling", "indoor_cycling"),
    "walking": ("walking", "generic"), "marche": ("walking", "generic"), "marche à pied": ("walking", "generic"),
    "hiking": ("hiking", "generic"), "randonnée": ("hiking", "generic"),
    "swimming": ("swimming", "generic"), "natation": ("swimming", "generic"),
    "musculation": ("training", "strength_training"), "strength_training": ("training", "strength_training"),
    "skiing": ("alpine_skiing", "generic"), "cross_country_skiing": ("cross_country_skiing", "generic"),
    "rowing": ("rowing", "generic"), "elliptical": ("fitness_equipment", "elliptical"),
}


def sport_fit(nom):
    return SPORTS.get((nom or "").strip().lower(), ("generic", "generic"))


@dataclass
class Point:
    temps: datetime
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    dist: float | None = None
    fc: int | None = None
    cad: int | None = None


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _date(texte):
    d = datetime.fromisoformat(texte.strip())
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def reparer_encodage(contenu):
    """Certains exports (Smashrun) annoncent de l'UTF-8 mais écrivent les accents en Latin-1 : on les réencode."""
    try:
        contenu.decode("utf-8")
        return contenu
    except UnicodeDecodeError:
        return contenu.decode("cp1252", errors="replace").encode("utf-8")


def lire_trace(contenu, ext):
    """Renvoie (nom du sport, points) d'un TCX ou d'un GPX. Lecture en flux : les très gros fichiers passent."""
    contenu = reparer_encodage(contenu)
    sport, points, courant = None, [], None
    for evenement, elem in ET.iterparse(io.BytesIO(contenu), events=("start", "end")):
        nom = _local(elem.tag)
        if evenement == "start":
            if nom in ("Trackpoint", "trkpt"):
                courant = {"lat": elem.get("lat"), "lon": elem.get("lon")}
            elif nom == "Activity" and sport is None:
                sport = elem.get("Sport")
            continue
        texte = (elem.text or "").strip()
        if courant is not None and texte:
            cle = {"Time": "temps", "time": "temps", "LatitudeDegrees": "lat", "LongitudeDegrees": "lon",
                   "AltitudeMeters": "alt", "ele": "alt", "DistanceMeters": "dist", "Value": "fc", "hr": "fc",
                   "Cadence": "cad", "RunCadence": "cad", "cad": "cad"}.get(nom)
            if cle:
                courant[cle] = texte
        elif nom == "type" and sport is None and texte:
            sport = texte  # GPX : <trk><type>
        if nom in ("Trackpoint", "trkpt"):
            if courant.get("temps"):
                points.append(Point(
                    _date(courant["temps"]),
                    *(float(courant[k]) if courant.get(k) else None for k in ("lat", "lon", "alt", "dist")),
                    *(int(float(courant[k])) if courant.get(k) else None for k in ("fc", "cad"))))
            courant = None
        if nom in ("Trackpoint", "trkpt", "Lap", "trkseg"):
            elem.clear()  # libère la mémoire au fil de la lecture
    return sport, points


def _distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def ecrire_fit(nom_sport, points, calories=None, titre=None, sport=None, sous_sport=None, distance=None,
               duree=None, fc_moy=None, fc_max=None):
    """Encode une activité FIT (une seule session, un seul tour) et renvoie ses octets."""
    points = sorted(points, key=lambda p: p.temps)
    if not points:
        raise ValueError("aucun point daté")
    # Distance cumulée recalculée depuis le GPS quand le fichier ne la donne pas
    if all(p.dist is None for p in points) and any(p.lat is not None for p in points):
        total, precedent = 0.0, None
        for p in points:
            if p.lat is not None:
                if precedent:
                    total += _distance(precedent, p)
                precedent = p
            p.dist = total

    if sport is None:
        sport, sous_sport = sport_fit(nom_sport)
    sport = {"other": "generic"}.get(sport, sport)
    sous_sport = sous_sport or "generic"
    debut, fin = points[0].temps, points[-1].temps
    # durée et distance du résumé de la source quand elles sont connues (plus justes que les points)
    duree = duree or max((fin - debut).total_seconds(), 1)
    distances = [p.dist for p in points if p.dist is not None]
    if distance is not None:
        distances = [distance]
    cardio = [p.fc for p in points if p.fc]

    enc = Encoder()
    enc.write_mesg({"mesg_num": MESG["FILE_ID"], "type": "activity", "manufacturer": "development",
                    "product": 1, "serial_number": 1, "time_created": debut})
    enc.write_mesg({"mesg_num": MESG["EVENT"], "timestamp": debut, "event": "timer", "event_type": "start"})
    for p in points:
        rec = {"mesg_num": MESG["RECORD"], "timestamp": p.temps}
        if p.lat is not None and p.lon is not None:
            rec["position_lat"], rec["position_long"] = round(p.lat * SEMICERCLES), round(p.lon * SEMICERCLES)
        for champ, valeur in (("altitude", p.alt), ("distance", p.dist), ("heart_rate", p.fc), ("cadence", p.cad)):
            if valeur is not None:
                rec[champ] = valeur
        enc.write_mesg(rec)
    enc.write_mesg({"mesg_num": MESG["EVENT"], "timestamp": fin, "event": "timer", "event_type": "stop_all"})

    resume = {"start_time": debut, "total_elapsed_time": duree, "total_timer_time": duree,
              "sport": sport, "sub_sport": sous_sport}
    if distances:
        resume["total_distance"] = max(distances)
    if cardio:
        resume["avg_heart_rate"], resume["max_heart_rate"] = round(sum(cardio) / len(cardio)), max(cardio)
    if fc_moy:
        resume["avg_heart_rate"] = int(fc_moy)
    if fc_max:
        resume["max_heart_rate"] = int(fc_max)
    if calories:
        resume["total_calories"] = int(calories)
    enc.write_mesg({"mesg_num": MESG["LAP"], "timestamp": fin, "event": "lap", "event_type": "stop", **resume})
    session = {"mesg_num": MESG["SESSION"], "timestamp": fin, "event": "session", "event_type": "stop",
               "first_lap_index": 0, "num_laps": 1, "trigger": "activity_end", **resume}
    if titre:
        session["sport_profile_name"] = titre[:15]
    enc.write_mesg(session)
    enc.write_mesg({"mesg_num": MESG["ACTIVITY"], "timestamp": fin, "total_timer_time": duree, "num_sessions": 1,
                    "type": "manual", "event": "activity", "event_type": "stop"})
    return enc.close()


PAS_SEANCE = 5  # s entre deux points d'une séance sans trace


def seance_fit(nom_sport, debut, duree_s, distance_m=None, calories=None, fc_moy=None, fc_max=None, titre=None,
               sport=None, sous_sport=None):
    """Activité FIT sans trace GPS, à partir d'un résumé de séance.

    Un point toutes les PAS_SEANCE secondes sur toute la durée, distance répartie régulièrement : sinon Strava,
    qui recalcule le temps à partir des points, affiche 0:00 (Garmin, lui, lit le résumé)."""
    instants = [*range(0, int(duree_s), PAS_SEANCE), duree_s]  # le dernier point tombe pile à la fin
    points = [Point(debut + timedelta(seconds=t), dist=distance_m * t / duree_s if distance_m else None, fc=fc_moy)
              for t in instants]
    if fc_max and fc_max != fc_moy:
        points[len(points) // 2].fc = fc_max  # le maximum apparaît une fois, la moyenne reste juste à un point près
    return ecrire_fit(nom_sport, points, calories, titre, sport, sous_sport, distance_m, duree_s, fc_moy, fc_max)


def resume_trace(contenu, ext):
    """(sport, début, durée s, distance m, calories, fc moy, fc max) d'un TCX/GPX, d'après ses tours ou ses points."""
    contenu = reparer_encodage(contenu)
    sport, points = lire_trace(contenu, ext)
    debut = duree = distance = calories = fc_moy = fc_max = None
    if ext == ".tcx":
        racine = ET.fromstring(contenu)
        tours = [e for e in racine.iter() if _local(e.tag) == "Lap"]
        valeur = lambda tour, nom: next((float(e.text) for e in tour if _local(e.tag) == nom and (e.text or "").strip()), 0.0)
        if tours:
            debut = _date(tours[0].get("StartTime"))
            duree = sum(valeur(t, "TotalTimeSeconds") for t in tours) or None
            distance = sum(valeur(t, "DistanceMeters") for t in tours) or None
            calories = sum(valeur(t, "Calories") for t in tours) or None
            fc = [float(v.text) for t in tours for e in t if _local(e.tag) == "AverageHeartRateBpm"
                  for v in e if _local(v.tag) == "Value" and float(v.text or 0) > 0]
            fc_moy = round(sum(fc) / len(fc)) if fc else None
            fcm = [float(v.text) for t in tours for e in t if _local(e.tag) == "MaximumHeartRateBpm"
                   for v in e if _local(v.tag) == "Value" and float(v.text or 0) > 0]
            fc_max = round(max(fcm)) if fcm else None
    if points:
        points.sort(key=lambda p: p.temps)
        debut = debut or points[0].temps
        duree = duree or (points[-1].temps - points[0].temps).total_seconds() or None
        distances = [p.dist for p in points if p.dist is not None]
        distance = distance or (max(distances) if distances else None)
    return sport, debut, duree, distance, calories, fc_moy, fc_max

"""Conversion précise (v2) : une séance normalisée par enregistrement, quelle que soit la source.

Chaque lecteur connaît sa source (voir analyse/ANALYSE.md) : où est l'heure exacte et dans quel fuseau,
où est la vraie durée, où est le vrai sport, et si la source a mesuré elle-même ou recopié une autre appli.
Les séances de toutes les sources sont ensuite regroupées (la même sortie vue par plusieurs applis),
le meilleur exemplaire est retenu, complété par les autres, filtré, puis écrit en FIT.
"""
import csv
import hashlib
import io
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import fichiers
import fitconv

PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc

# Seuils de la politique choisie le 25/10/2026. Les séances plus courtes (surtout les morceaux de la détection
# automatique de pas) ne sont pas perdues : celles qui se suivent sont recollées (voir regrouper.recoller)
DUREE_MIN = {"walking": 600, "hiking": 600}  # marche : 10 min ; autres sports : 5 min
DUREE_MIN_DEFAUT = 300
DUREE_MAX = 16 * 3600  # au-delà : total journalier déguisé en activité
VITESSE = {  # km/h plausibles en moyenne
    "walking": (1.5, 8.0), "hiking": (1.0, 7.0), "running": (5.0, 22.0), "cycling": (6.0, 50.0), "swimming": (0.5, 6.0),
}
DOSSIER_MONTRE = "montre-garmin"  # sauvegarde de la montre Garmin (sport.py le règle d'après chemins.yml)
VEHICULE = 28.0  # km/h moyens : au-delà, une marche ou une course est un trajet en véhicule (16 à 28 : vélo)
POINTS_TRACE = 10  # en dessous : pas de vraie trace

# Priorité des enregistreurs quand la même séance existe plusieurs fois (plus haut = préféré)
PRIORITE = {
    "Garmin": 100, "Zepp": 90, "TomTom": 90, "Runtastic": 80, "Samsung Health": 75, "MapMyRun": 70,
    "Moves": 60, "Withings": 55, "Google Fit": 50, "Runalyze": 30, "Smashrun": 20, "inconnu": 10,
}


@dataclass
class Seance:
    source: str  # famille de fichiers lue (« Google Fit », « Smashrun »…)
    enregistreur: str  # appli qui a réellement mesuré (clé de PRIORITE)
    original: bool  # True : enregistrée par cette source ; False : recopiée d'une autre appli
    debut: datetime  # UTC
    duree: float  # s
    distance: float | None  # m
    sport: str  # running, walking, hiking, cycling, swimming, training, other
    sous_sport: str = "generic"
    sport_sur: bool = True  # sport déclaré explicitement par la source
    calories: float | None = None
    fc_moy: int | None = None
    fc_max: int | None = None
    nb_points: int = 0  # points datés de la trace
    gps: bool = False
    cardio: bool = False
    titre: str | None = None
    chemin: str = ""
    heure_douteuse: bool = False  # fuseau inconnu : peut être décalée d'un nombre entier d'heures
    qualite: list = field(default_factory=list)  # remarques (interpolée, GPS clairsemé…)
    _points: object = field(default=None, repr=False)  # fonction qui charge les points (lecture paresseuse)

    @property
    def fin(self):
        return self.debut + timedelta(seconds=self.duree)

    def vitesse(self):
        return self.distance / self.duree * 3.6 if self.distance and self.duree else None

    def points(self):
        return self._points() if self._points else []


# ----------------------------------------------------------------------------- outils communs

def _nfc(texte):
    return unicodedata.normalize("NFC", texte)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _iso(texte):
    d = datetime.fromisoformat(texte.strip().replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def _ms(ms):
    return datetime.fromtimestamp(ms / 1000, UTC)


def _distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def _longueur(points):
    gps = [p for p in points if p.lat is not None]
    return sum(_distance(a, b) for a, b in zip(gps, gps[1:]))


def _distance_google_fit(contenu, debut, fin):
    """Distance d'une séance Google Fit. Le fichier peut contenir plusieurs <Activity>/<Lap> qui se recouvrent, chacun
    avec sa propre distance cumulée (qui repart de 0) : on prend le tour qui recouvre le mieux la séance, et l'on
    interpole sa distance cumulée aux bornes. 0 ou rien : distance inconnue."""
    meilleur, recouvrement = None, 0
    for tour in ET.fromstring(contenu).iter():
        if _local(tour.tag) != "Lap":
            continue
        serie = []
        for tp in tour.iter():
            if _local(tp.tag) == "Trackpoint":
                t = next((e.text for e in tp if _local(e.tag) == "Time"), None)
                d = next((e.text for e in tp if _local(e.tag) == "DistanceMeters"), None)
                if t and d:
                    serie.append((_iso(t), float(d)))
        if len(serie) < 2:
            continue
        serie.sort()
        commun = (min(fin, serie[-1][0]) - max(debut, serie[0][0])).total_seconds()
        if commun > recouvrement:
            meilleur, recouvrement = serie, commun
    if not meilleur:
        return None
    distance = _interpoler(meilleur, fin) - _interpoler(meilleur, debut)
    return distance if distance > 0 else None


def _interpoler(serie, instant):
    """Valeur d'une série [(temps, valeur)] triée à un instant donné (linéaire, bornée aux extrémités)."""
    if instant <= serie[0][0]:
        return serie[0][1]
    for (t1, v1), (t2, v2) in zip(serie, serie[1:]):
        if t1 <= instant <= t2:
            return v1 if t2 == t1 else v1 + (v2 - v1) * (instant - t1).total_seconds() / (t2 - t1).total_seconds()
    return serie[-1][1]


def minutes_reconstruites(points):
    """Minutes d'une trace reconstruite en ligne droite (fenêtres de 10 min dont le chemin égale la ligne droite à
    0,5 % près, à vitesse constante à 2 % près). Un vrai GPS a toujours un peu de bruit qui allonge le chemin : une ligne parfaite vient d'un
    logiciel qui a « bouché » un trou (trajet en train ou en voiture sans signal, journée Moves exportée…)."""
    gps = [p for p in points if p.lat is not None]
    marquees, i = set(), 0
    while i < len(gps) - 1:
        j = i
        while j < len(gps) - 1 and (gps[j].temps - gps[i].temps).total_seconds() < 600:
            j += 1
        fenetre = gps[i:j + 1]
        if (fenetre[-1].temps - fenetre[0].temps).total_seconds() >= 300:
            pas = [_distance(a, b) / max((b.temps - a.temps).total_seconds(), 1) for a, b in zip(fenetre, fenetre[1:])]
            chemin = sum(_distance(a, b) for a, b in zip(fenetre, fenetre[1:]))
            moyenne = sum(pas) / len(pas)
            ecart = (sum((x - moyenne) ** 2 for x in pas) / len(pas)) ** 0.5
            # ligne droite ET vitesse rigoureusement constante : une vraie trace lissée (TomTom) sur une route droite
            # garde des variations d'allure ; une interpolation non
            if chemin > 100 and _distance(fenetre[0], fenetre[-1]) / chemin > 0.995 and moyenne > 0 and ecart / moyenne < 0.02:
                minute = fenetre[0].temps.replace(second=0, microsecond=0)
                while minute <= fenetre[-1].temps:
                    marquees.add(minute)
                    minute += timedelta(minutes=1)
        i = max(j, i + 1)
    return marquees


def part_reconstruite(points):
    gps = [p for p in points if p.lat is not None]
    if len(gps) < 20:
        return 0.0
    duree = (gps[-1].temps - gps[0].temps).total_seconds() / 60
    return min(len(minutes_reconstruites(gps)) / duree, 1.0) if duree >= 5 else 0.0


def _decrire(s, points):
    """Renseigne nb_points, gps, cardio à partir des points."""
    s.nb_points = len(points)
    s.gps = sum(p.lat is not None for p in points) >= POINTS_TRACE
    s.cardio = any(p.fc for p in points)
    return s


def _sport_libelle(texte):
    """Libellés rencontrés (anglais, français, sous-types Google Fit) → (sport, sous_sport, sûr)."""
    t = _nfc(texte or "").strip().lower()
    table = [
        ("running.treadmill", "running", "treadmill"), ("course sur tapis", "running", "treadmill"),
        ("treadmill", "running", "treadmill"), ("trail", "running", "trail"), ("running", "running", "generic"),
        ("course à pied", "running", "generic"), ("course", "running", "generic"), ("run", "running", "generic"),
        ("randonnée", "hiking", "generic"), ("hiking", "hiking", "generic"),
        ("marche", "walking", "generic"), ("walking", "walking", "generic"), ("walk", "walking", "generic"),
        ("vélo", "cycling", "generic"), ("biking", "cycling", "generic"), ("cycling", "cycling", "generic"),
        ("ride", "cycling", "generic"), ("natation en piscine", "swimming", "lap_swimming"),
        ("piscine", "swimming", "lap_swimming"), ("swimming.pool", "swimming", "lap_swimming"),
        ("eau libre", "swimming", "open_water"), ("open water", "swimming", "open_water"),
        ("swimming.open_water", "swimming", "open_water"),
        ("natation", "swimming", "generic"), ("swimming", "swimming", "generic"),
        ("musculation", "training", "strength_training"), ("strength", "training", "strength_training"),
    ]
    for libelle, sport, sous in table:
        if libelle in t:
            return sport, sous, True
    return "other", "generic", False


# ----------------------------------------------------------------------------- lecteurs

GOOGLE_FIT_NOM = re.compile(r"(\d{4}-\d\d-\d\dT\d\d_\d\d_\d\d(?:\.\d+)?)([+-]\d\d)_(\d\d)_PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?_(.*)\.tcx$")


def lire_google_fit(fichier):
    """Takeout Google Fit, Fit/Activités ou « Activité fait ». Le nom du fichier fait foi : heure locale avec fuseau,
    durée exacte, type. Le contenu peut couvrir une fenêtre plus large, avec plusieurs <Activity> : on ne garde
    que les points de la séance, et le sport vient de <Notes> (Walking, Running.treadmill…)."""
    m = GOOGLE_FIT_NOM.match(_nfc(fichier.name))
    if not m:
        return
    debut = datetime.fromisoformat(m[1].replace("_", ":") + m[2] + ":" + m[3]).astimezone(UTC)
    duree = float(m[4] or 0) * 3600 + float(m[5] or 0) * 60 + float(m[6] or 0)
    contenu = fitconv.reparer_encodage(fichier.read_bytes())
    notes = re.findall(rb"<Notes>([^<]*)</Notes>", contenu)
    calories = re.findall(rb"<Calories>([\d.]+)</Calories>", contenu)
    sport, sous, sur = _sport_libelle(notes[0].decode() if notes else "")
    if not sur:
        sport, sous, sur = _sport_libelle(m[7].split(",")[0])  # « Marche à pied,Autre » : séance mixte
    elif sous == "generic":
        nom_sport, nom_sous, _ = _sport_libelle(m[7].split(",")[0])
        if nom_sport == sport:
            sous = nom_sous  # « Natation en piscine » : Notes dit seulement « Swimming »
    fin = debut + timedelta(seconds=duree)

    def points(contenu=None):
        _, tous = fitconv.lire_trace(contenu or fitconv.reparer_encodage(fichier.read_bytes()), ".tcx")
        return sorted((p for p in tous if debut - timedelta(seconds=5) <= p.temps <= fin + timedelta(seconds=5)),
                      key=lambda p: p.temps)

    pts = points(contenu)
    distance = _distance_google_fit(contenu, debut, fin) or _longueur(pts) or None
    s = Seance("Google Fit", "Google Fit", True, debut, duree, distance or None, sport, sous, sur,
               calories=float(calories[0]) if len(calories) == 1 else None, chemin=str(fichier), _points=points)
    yield _decrire(s, [p for p in pts if p.lat is not None or p.fc])


def lire_samsung(fichier):
    """com.samsung.shealth.exercise.<date>.csv : heures UTC (vérifié). Distance et durée du résumé font foi ;
    le GPS (location_data) sert au tracé s'il est assez dense, le cardio vient de live_data."""
    P = "com.samsung.health.exercise."
    dossier_json = fichier.parent / "jsons" / "com.samsung.shealth.exercise"
    origines = {"com.sec.android.app.shealth": ("Samsung Health", True), "com.mapmyrun.android2": ("MapMyRun", True),
                "nl.appyhapps.healthsync": ("Google Fit", False), "com.syncmytracks": ("inconnu", False),
                "com.fitnesssyncer.fitnesssyncer_for_samsung_health": ("inconnu", False)}
    sports = {"1001": ("walking", "generic"), "1002": ("running", "generic"), "11007": ("cycling", "generic"),
              "13001": ("hiking", "generic"), "14001": ("swimming", "generic")}

    def json_exercice(nom):
        chemin = dossier_json / nom[:1] / nom if nom else None
        try:
            return json.loads(chemin.read_text()) if chemin and chemin.exists() else []
        except (json.JSONDecodeError, OSError):
            return []

    with fichier.open(encoding="utf-8-sig") as f:
        next(f)
        for l in csv.DictReader(f):
            duree = float(l.get(P + "duration") or 0) / 1000
            if not l.get(P + "start_time") or duree <= 0:
                continue
            debut = datetime.fromisoformat(l[P + "start_time"]).replace(tzinfo=UTC)
            enregistreur, original = origines.get(l.get(P + "pkg_name"), ("inconnu", False))
            sport, sous = sports.get(l.get(P + "exercise_type"), ("other", "generic"))
            distance = float(l.get(P + "distance") or 0) or None
            nom_gps, nom_mesures = l.get(P + "location_data"), l.get(P + "live_data")

            def points(nom_gps=nom_gps, nom_mesures=nom_mesures, duree=duree):
                gps = [fitconv.Point(_ms(m["start_time"]), m["latitude"], m["longitude"], m.get("altitude"))
                       for m in json_exercice(nom_gps) if "latitude" in m and "start_time" in m]
                if len(gps) < max(POINTS_TRACE, duree / 60):  # moins d'un point par minute : tracé inutilisable
                    gps = []
                mesures = [fitconv.Point(_ms(m["start_time"]), fc=int(m["heart_rate"]) if m.get("heart_rate") else None,
                                         cad=int(m["cadence"]) if m.get("cadence") else None)
                           for m in json_exercice(nom_mesures) if "start_time" in m and (m.get("heart_rate") or m.get("cadence"))]
                return sorted(gps + mesures, key=lambda p: p.temps)

            fc = float(l.get(P + "mean_heart_rate") or 0)
            fcm = float(l.get(P + "max_heart_rate") or 0)
            s = Seance("Samsung", enregistreur, original, debut, duree, distance, sport, sous, sport != "other",
                       calories=float(l.get(P + "calorie") or 0) or None, fc_moy=int(fc) if fc else None,
                       fc_max=int(fcm) if fcm else None, chemin=f"{fichier} ! {l.get(P + 'datauuid')}", _points=points)
            yield _decrire(s, points()) if (nom_gps or nom_mesures) else s


SPORTS_RUNTASTIC = {
    "1": ("running", "generic"), "2": ("walking", "generic"), "3": ("cycling", "generic"), "4": ("cycling", "mountain"),
    "7": ("hiking", "generic"), "8": ("cross_country_skiing", "generic"), "9": ("alpine_skiing", "generic"),
    "14": ("running", "treadmill"), "15": ("cycling", "indoor_cycling"), "16": ("fitness_equipment", "elliptical"),
    "17": ("rowing", "generic"), "18": ("swimming", "generic"), "19": ("walking", "generic"),
    "22": ("cycling", "road"), "34": ("training", "strength_training"),
}


def lire_runtastic(fichier):
    """Sport-sessions/*.json (dossier ou ZIP de l'export). Heures en ms UTC. Une séance créée plus d'un jour après
    sa fin est un import (souvent depuis Google Fit, Samsung ou Garmin) ; avec un fuseau à 0, son heure peut être
    l'heure locale écrite comme UTC (vérifié contre les traces GPS : 101 décalées sur 248, quel que soit le fuseau
    noté) : son heure est « à vérifier ». Trace : GPS-data/<même nom>.gpx ; cardio : Heart-rate-data/<même nom>.json."""
    try:
        d = json.loads(fichier.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if "sport_type_id" not in d or not d.get("start_time") or not d.get("duration"):
        return
    debut, duree = _ms(d["start_time"]), d["duration"] / 1000
    importee = bool(d.get("created_at") and d.get("end_time") and d["created_at"] - d["end_time"] > 86400000)
    sport, sous = SPORTS_RUNTASTIC.get(str(d["sport_type_id"]), ("other", "generic"))
    if sport == "swimming" and d.get("indoor"):
        sous = "lap_swimming"
    gpx = fichier.parent / "GPS-data" / (fichier.stem + ".gpx")
    cardio = fichier.parent / "Heart-rate-data" / fichier.name
    a_gps, a_cardio = gpx.exists(), cardio.exists()

    def points():
        pts = fitconv.lire_trace(gpx.read_bytes(), ".gpx")[1] if a_gps else []
        if a_cardio:
            try:
                for m in json.loads(cardio.read_text()):
                    t = datetime.strptime(m["timestamp"], "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)
                    pts.append(fitconv.Point(t, dist=m.get("distance"), fc=m.get("heart_rate") or None))
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
        return sorted(pts, key=lambda p: p.temps)

    # une séance importée (même avec un garmin_id : copie venue de Garmin Connect) n'a pas été mesurée par Runtastic
    s = Seance("Runtastic", "Runtastic" if not importee else "inconnu", not importee, debut, duree, d.get("distance") or None, sport, sous, sport != "other",
               calories=d.get("calories") or None, fc_moy=d.get("pulse_avg") or None, fc_max=d.get("pulse_max") or None,
               chemin=str(fichier), heure_douteuse=importee, _points=points)
    s.nb_points, s.gps, s.cardio = (POINTS_TRACE if a_gps or a_cardio else 0), a_gps, a_cardio
    yield s


def lire_trace_generique(fichier, source, enregistreur, original, decalage_local=False, sport_force=None,
                         heure_douteuse=False):
    """TCX / GPX / FIT quelconque : le résumé (tours, session) fait foi pour la durée et la distance.
    decalage_local : l'heure écrite est l'heure de Paris présentée comme UTC (Smashrun)."""
    contenu = fitconv.reparer_encodage(fichier.read_bytes()) if fichier.suffix.lower() != ".fit" else fichier.read_bytes()
    ext = fichier.suffix.lower()
    if ext == ".fit":
        from garmin_fit_sdk import Decoder, Stream

        messages, _ = Decoder(Stream.from_byte_array(bytearray(contenu))).read()
        sessions = messages.get("session_mesgs") or []
        records = messages.get("record_mesgs") or []
        if not sessions:
            return
        se = sessions[0]
        debut, duree = se.get("start_time"), se.get("total_timer_time") or se.get("total_elapsed_time")
        if not debut or not duree:
            return
        semi = 180 / 2**31
        pts = [fitconv.Point(r["timestamp"], r["position_lat"] * semi if r.get("position_lat") is not None else None,
                             r["position_long"] * semi if r.get("position_long") is not None else None,
                             r.get("enhanced_altitude") or r.get("altitude"), r.get("distance"), r.get("heart_rate"),
                             r.get("cadence")) for r in records if r.get("timestamp")]
        sport, sous, sur = _sport_libelle(str(sport_force or se.get("sport") or ""))
        sous = str(se.get("sub_sport") or sous) if sur else sous
        s = Seance(source, enregistreur, original, debut.astimezone(UTC), duree, se.get("total_distance") or None,
                   sport, sous if sous in ("treadmill", "trail", "indoor_cycling", "mountain", "road", "strength_training",
                                       "lap_swimming", "open_water") else "generic",
                   sur, calories=se.get("total_calories"), fc_moy=se.get("avg_heart_rate"), fc_max=se.get("max_heart_rate"),
                   chemin=str(fichier), _points=lambda pts=pts: pts)
        yield _decrire(s, pts)
        return

    sport_nom, debut, duree, distance, calories, fc_moy, fc_max = fitconv.resume_trace(contenu, ext)
    if not debut or not duree:
        return
    decalage = timedelta(0)
    if decalage_local:  # heure de Paris écrite comme UTC
        locale = debut.replace(tzinfo=None)
        decalage = locale.replace(tzinfo=PARIS).utcoffset()
        debut = locale.replace(tzinfo=PARIS).astimezone(UTC)
    sport, sous, sur = _sport_libelle(sport_force or sport_nom or "")

    def points(ext=ext, decalage=decalage):
        pts = fitconv.lire_trace(fitconv.reparer_encodage(fichier.read_bytes()), ext)[1]
        for p in pts:
            p.temps -= decalage
        return sorted(pts, key=lambda p: p.temps)

    pts = fitconv.lire_trace(contenu, ext)[1]
    for p in pts:
        p.temps -= decalage
    pts.sort(key=lambda p: p.temps)
    if not distance and ext == ".gpx":
        distance = _longueur(pts) or None  # un GPX n'écrit pas la distance : longueur du tracé
    if not sur and ext == ".gpx":
        nom_trace = re.search(rb"<trk>\s*<name>([^<]+)</name>", contenu)  # TomTom : <trk><name>Running</name>
        if nom_trace:
            sport, sous, sur = _sport_libelle(nom_trace[1].decode("utf-8", "replace"))
    del contenu
    s = Seance(source, enregistreur, original, debut, duree, distance, sport, sous, sur, calories=calories,
               fc_moy=fc_moy, fc_max=fc_max, chemin=str(fichier), heure_douteuse=heure_douteuse, _points=points)
    s = _decrire(s, pts)
    reconstruite = part_reconstruite(pts)
    if reconstruite > 0.5:
        s.gps = False  # pas un vrai tracé : on se fie au résumé
        s.qualite.append(f"trace reconstruite en ligne droite à {reconstruite:.0%}")
    elif reconstruite > 0.1:
        s.qualite.append(f"trace en partie reconstruite ({reconstruite:.0%})")
    if source == "Smashrun" and len(pts) >= 120:
        vitesses = [round(_distance(a, b) / max((b.temps - a.temps).total_seconds(), 1), 2)
                    for a, b in zip(pts[::60], pts[60::60]) if a.lat is not None and b.lat is not None]
        if vitesses and sum(x == y for x, y in zip(vitesses, vitesses[1:])) / max(len(vitesses) - 1, 1) > 0.6:
            s.qualite.append("trace interpolée par Smashrun")
    yield s


def lire_moves(fichier):
    """Moves, gpx/daily/activities_AAAAMMJJ.gpx : la journée est découpée en sorties distinctes par sport
    (le transport est écarté). Heures avec leur fuseau : exactes."""
    racine = ET.fromstring(fichier.read_bytes())
    ns = "{http://www.topografix.com/GPX/1/1}"
    for seg in racine.iter(ns + "trkseg"):
        pts = []
        for pt in seg.iter(ns + "trkpt"):
            t = pt.findtext(ns + "time")
            if t:
                pts.append((fitconv.Point(_iso(t), float(pt.get("lat")), float(pt.get("lon"))), pt.findtext(ns + "type", "")))
        if len(pts) < 2:
            continue
        types = [t for _, t in pts]
        sport = max(set(types), key=types.count)
        if sport not in ("walking", "running", "cycling"):
            continue
        points = [p for p, _ in pts]
        s = Seance("Moves", "Moves", True, points[0].temps, (points[-1].temps - points[0].temps).total_seconds(),
                   _longueur(points), sport, chemin=f"{fichier} #{len(points)}", _points=lambda points=points: points)
        yield _decrire(s, points)


def lire_withings(fichier):
    """activities.csv de l'export Withings : heures locales avec fuseau, résumé seulement."""
    with fichier.open(encoding="utf-8-sig") as f:
        for l in csv.DictReader(f):
            debut, fin = _iso(l["Début"]), _iso(l["Fin"])
            donnees = json.loads(l.get("Données") or "{}")
            sport, sous, sur = _sport_libelle(l.get("Type d'activité", ""))
            yield Seance("Withings", "Withings", True, debut, (fin - debut).total_seconds(), donnees.get("distance") or None,
                         sport, sous, sur, calories=donnees.get("calories") or None,
                         fc_moy=donnees.get("hr_average") or None, fc_max=donnees.get("hr_max") or None, chemin=str(fichier))


def lire_runalyze_json(fichier, deja):
    """Résumé Runalyze (activities/*.json) : seulement s'il n'a pas de fichier de trace voisin."""
    if any((fichier.parent / (fichier.stem + e)).exists() for e in (".tcx", ".zip", ".gpx", ".fit")):
        return
    d = json.loads(fichier.read_text())
    if not isinstance(d, dict) or "climbScore" not in d or not d.get("time") or not d.get("duration"):
        return  # autre format de JSON rangé dans un dossier « activities »
    sport, sous, sur = _sport_libelle(d.get("sport") or "")
    yield Seance("Runalyze", "Runalyze", False, datetime.fromtimestamp(d["time"], UTC), d["duration"],
                 d["distance"] * 1000 if d.get("distance") else None, sport, sous, sur, calories=d.get("kcal") or None,
                 fc_moy=d.get("hrAvg"), fc_max=d.get("hrMax"), titre=d.get("title") or None, chemin=str(fichier))


HEALTH_SYNC_NOM = re.compile(r"\d{4}\.\d\d\.\d\d \d\d\.\d\d-[A-Z_]+\.(fit|tcx|gpx|kml)", re.I)
HEALTH_SYNC_APPLIS = {"garmin": "Garmin", "google_fit": "Google Fit", "strava": "Strava"}


def lire_health_sync(fichier):
    """Health Sync (export Health Connect) : « 2026.04.05 09.37-RUNNING » en .fit, .tcx, .gpx et .kml, plus un CSV de
    résumé par appli. Vérifié le 26/10/2026 contre la montre (course du 25/10) et les traces GPS :
    - heure : le FIT est en UTC, le nom en heure locale ; justes toutes les deux ;
    - distance : pour les séances venues de Garmin ou de Strava, Health Connect cumule plusieurs séries de distance
      (non croissantes) : 25,5 km pour 9,1 km de trace GPS régulière. La longueur de la trace fait foi, et sans trace
      la distance est inconnue ;
    - durée : une séance Google Fit recopiée plusieurs fois a un temps « actif » multiple exact du temps écoulé
      (4 136 s pour 1 034 s) : le temps écoulé fait foi, et la distance, comptée plusieurs fois elle aussi, est inconnue ;
    - le GPX peut venir d'une autre appli que le FIT (31/05/2026 : FIT Google Fit, GPX Garmin) : il est alors lu à part."""
    s = next(lire_trace_generique(fichier, "Health Sync", "inconnu", False), None)
    if s is None:
        return
    pts = s.points()
    if fichier.suffix.lower() == ".fit":
        from garmin_fit_sdk import Decoder, Stream

        messages, _ = Decoder(Stream.from_byte_array(bytearray(fichier.read_bytes()))).read()
        ecoule = (messages.get("session_mesgs") or [{}])[0].get("total_elapsed_time") or s.duree
        if s.duree > 1.5 * ecoule:
            s.duree, s.distance = ecoule, None
            s.qualite.append("temps actif compté plusieurs fois par Health Connect : temps écoulé retenu, distance inconnue")
    s.enregistreur = _appli_health_sync(fichier, s)
    gpx = fichier.parent / (fichier.stem + ".gpx")
    if fichier.suffix.lower() != ".gpx" and gpx.exists():
        g = next(lire_trace_generique(gpx, "Health Sync", "inconnu", False), None)
        if g and abs((g.debut - s.debut).total_seconds()) <= 60 and abs(g.duree - s.duree) <= 0.05 * s.duree + 60:
            if g.gps and not s.gps:  # même enregistrement : le GPX apporte la trace absente du FIT
                pts, s.chemin = g.points(), f"{s.chemin} + {gpx.name}"
        elif g:
            g.enregistreur = _appli_health_sync(gpx, g)
            g = _distance_health_sync(g, g.points())
            if g.gps and not s.gps and abs((g.debut - s.debut).total_seconds()) <= 120:
                # même nom, donc même séance pour Health Sync, vue par deux applis (31/05/2026 : course Garmin de
                # 76 min, Google Fit n'en a vu que 30) : on garde celle qui a la trace GPS
                g.qualite.append(f"aussi vue par {s.enregistreur} ({s.duree / 60:.0f} min, sans trace)")
                yield g
                return
            yield g
    yield _distance_health_sync(s, pts)


def _distance_health_sync(s, pts):
    """Distance d'une séance Health Sync (voir lire_health_sync) ; les distances cumulées des points suivent."""
    gps = [p for p in pts if p.lat is not None]
    distances = [p.dist for p in pts if p.dist is not None]
    if len(gps) >= POINTS_TRACE:
        longueur = _longueur(gps)
        if s.distance and abs(s.distance - longueur) > 0.05 * longueur:
            s.qualite.append(f"distance de la trace GPS ({s.distance / 1000:.1f} km d'après Health Connect)")
        s.distance = longueur
    elif s.distance and (s.enregistreur in ("Garmin", "Strava") or distances != sorted(distances)):
        s.qualite.append(f"distance inconnue ({s.distance / 1000:.1f} km d'après Health Connect, sans trace GPS)")
        s.distance = None
    if not s.distance or len(gps) >= POINTS_TRACE:
        for p in pts:
            p.dist = None  # recalculée depuis le GPS à l'écriture, ou inconnue
    s._points = lambda pts=pts: pts
    return _decrire(s, pts)


def _appli_health_sync(fichier, s):
    """Appli d'origine d'une séance Health Sync, d'après les CSV de résumé voisins (« RUNNING 2026.04.05 09.37 Garmin.csv ») :
    celui dont l'heure locale tombe sur le départ à un fuseau près et dont la durée est la plus proche. Le CSV
    « Health Sync » est le résumé de l'appli elle-même et ne dit pas d'où vient la séance."""
    jour_heure, type_ = _nfc(fichier.stem).rsplit("-", 1)
    prefixe = f"{type_} {jour_heure}"
    candidats = []
    for voisin in fichier.parent.iterdir():
        if not (_nfc(voisin.name).startswith(prefixe) and voisin.name.endswith(".csv")):
            continue
        with voisin.open(encoding="utf-8-sig") as f:
            for l in csv.DictReader(f):
                ecart = (datetime.strptime(l["Date"], "%Y.%m.%d %H:%M:%S") - s.debut.replace(tzinfo=None)).total_seconds()
                residu = abs(ecart - round(ecart / 900) * 900)  # fuseaux : multiples d'un quart d'heure
                appli = l["Application source"].strip().lower()
                candidats.append((residu > 30, appli not in HEALTH_SYNC_APPLIS, residu,
                                  abs(float(l["Temps écoulé"] or 0) - s.duree), appli))
    if not candidats or min(candidats)[0]:
        return "inconnu"
    return HEALTH_SYNC_APPLIS.get(min(candidats)[4], "inconnu")


# ----------------------------------------------------------------------------- aiguillage

def reconnaitre(fichier):
    """Choisit le lecteur d'un fichier (Path ou membre de ZIP). Renvoie un itérable de Séances, ou None."""
    nom, ext, chemin = _nfc(fichier.name), fichier.suffix.lower(), _nfc(str(fichier))
    parent = _nfc(fichier.parent.name)
    if nom.endswith(".icloud"):
        return None
    if HEALTH_SYNC_NOM.fullmatch(nom):
        # une séance = FIT, TCX, GPX et KML du même nom : lue une fois, par le FIT (à défaut le TCX, puis le GPX)
        for principal in (".fit", ".tcx", ".gpx"):
            if (fichier.parent / (fichier.stem + principal)).exists():
                return lire_health_sync(fichier) if ext == principal else None
        return None
    if ext == ".tcx" and GOOGLE_FIT_NOM.match(nom):  # nom propre aux exports Google Fit, où qu'ils soient rangés
        return lire_google_fit(fichier)
    if re.fullmatch(r"com\.samsung\.shealth\.exercise\.\d+\.csv", nom):
        return lire_samsung(fichier)
    if ext == ".json" and parent == "Sport-sessions":
        return lire_runtastic(fichier)
    if parent == "GPS-data" or "Runtastic_GPX_TCX" in chemin or re.search(r"_UTC_[0-9a-f-]{20,}\.gpx$", nom):
        return None  # traces et conversions des séances Runtastic, lues avec leur JSON
    if nom == "activities.csv" and "Type d'activité" in fichiers.texte(fichier, 300):
        return lire_withings(fichier)
    if re.fullmatch(r"activities_\d{8}\.gpx", nom):
        return lire_moves(fichier) if "/daily/" in chemin or "/moves/" in chemin or "/gpx-files/" in chemin else None
    if ext not in (".tcx", ".gpx", ".fit"):
        if ext == ".json" and parent == "activities":
            return lire_runalyze_json(fichier, None)
        return None
    if "smashrun" in nom.lower() or "smachrun" in chemin.lower():
        # heure juste dans 73 % des cas (vérifié contre les traces GPS), décalée de +1/+2 h pour certaines séances
        # reçues d'autres applis : « à vérifier », corrigée par un exemplaire sûr s'il existe
        return lire_trace_generique(fichier, "Smashrun", "Smashrun", False, heure_douteuse=True)
    if "tomtom" in nom.lower():  # « 2014-07-25 269872872 Running Tomtom.gpx » : le sport est dans le nom
        sport_nom = re.search(r"\d+ (\w+) Tomtom", nom)
        # les fichiers « (copie) » sont des journées reconstruites (points Moves reliés en ligne droite), recopiées
        # aussi dans Smashrun et Runtastic : ce sont des copies, pas des enregistrements de la montre
        copie = "copie" in nom.lower()
        # le même contenu peut circuler renommé sans « (copie) » (sport/Sport/, 163 fichiers le 26/10/2026) :
        # lire_sources() rapproche les empreintes
        EMPREINTES_TOMTOM[str(fichier)] = (hashlib.sha1(fichier.read_bytes()).hexdigest(), copie)
        return lire_trace_generique(fichier, "TomTom", "inconnu" if copie else "TomTom", not copie,
                                    sport_force=sport_nom[1] if sport_nom and sport_nom[1] != "Other" else None)
    if f"/{DOSSIER_MONTRE}/" in chemin and "/Activity/" in chemin:
        return lire_trace_generique(fichier, "Garmin", "Garmin", True)
    if re.fullmatch(r"(RUNNING|WALKING|TREADMILL|CYCLING)-\d+\.fit", nom):
        return lire_trace_generique(fichier, "Zepp", "Zepp", True)
    if parent == "activities" or "/activities/" in chemin:
        if ext == ".fit":
            return None  # FIT Runalyze (dans les ZIP) : heure décalée, le TCX ou le JSON du même nom fait foi
        return lire_trace_generique(fichier, "Runalyze", "Runalyze", False, heure_douteuse=True)
    return None  # le reste (anciennes conversions, copies, agrégats) est connu par une source plus fiable


EMPREINTES_TOMTOM = {}  # chemin → (SHA-1 du contenu, nommé « (copie) »), rempli par reconnaitre()


def lire_sources(racines, exclus=(), journal=print):
    """Toutes les séances de toutes les racines (dossiers et contenu des ZIP)."""
    seances, comptes, erreurs = [], {}, []
    EMPREINTES_TOMTOM.clear()
    for racine in racines:
        if not racine.is_dir():
            continue
        for fichier in fichiers.parcourir(racine, exclus):
            try:
                lecteur = reconnaitre(fichier)
                if lecteur is None:
                    continue
                for s in lecteur:
                    seances.append(s)
                    comptes[s.source] = comptes.get(s.source, 0) + 1
            except (ET.ParseError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError,
                    UnicodeDecodeError, zipfile.BadZipFile, OSError) as e:
                erreurs.append((str(fichier), f"{type(e).__name__}: {e}"[:160]))
    copies = {empreinte for empreinte, copie in EMPREINTES_TOMTOM.values() if copie}
    renommees = 0
    for s in seances:
        if s.source == "TomTom" and s.original and EMPREINTES_TOMTOM.get(s.chemin, ("", False))[0] in copies:
            s.original, s.enregistreur = False, "inconnu"  # contenu identique à un fichier « (copie) »
            renommees += 1
    if renommees:
        journal(f"  {renommees} fichier(s) TomTom identiques à une « (copie) » sous un autre nom : traités comme des copies")
    journal("  séances lues : " + ", ".join(f"{n} {s}" for s, n in sorted(comptes.items(), key=lambda x: -x[1])))
    if erreurs:
        journal(f"  {len(erreurs)} fichier(s) illisibles, par exemple : " + " | ".join(f"{c} ({e})" for c, e in erreurs[:3]))
    lire_sources.erreurs = erreurs
    return seances

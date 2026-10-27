"""Conversion précise (v2), deuxième étape : de toutes les séances lues à une activité par sortie réelle.

1. tri préalable : durées impossibles (séances sans durée ou totaux journaliers) ; les séances trop courtes qui
   se suivent sont recollées en une sortie (détection automatique de pas découpée en minutes) ;
2. regroupement : la même sortie vue par plusieurs applis (horaires qui se recouvrent, après correction
   des fuseaux ; une heure « douteuse » peut être décalée d'un nombre entier d'heures) ;
3. choix du meilleur exemplaire (enregistreur d'origine, GPS, cardio…), complété par les autres ;
4. sport : celui de la source s'il est sûr et plausible, sinon déduit de la vitesse ; séance écartée si le sport
   reste incertain ou si la vitesse trahit un trajet en véhicule ;
5. écriture d'un FIT par activité, avec un index et la liste des séances écartées (et pourquoi).
"""
import bisect
import csv
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

import fitconv
import seances
from seances import (DUREE_MAX, DUREE_MIN, DUREE_MIN_DEFAUT, PARIS, POINTS_TRACE, PRIORITE, VEHICULE, VITESSE, UTC)

ECART_DEPART = 120  # s : départs à moins de 2 min = même sortie
RECOUVREMENT = 0.6  # part de la plus courte des deux séances couverte par l'autre
DECALAGES_DOUTEUX = (3600, 7200, -3600, -7200)  # heure locale écrite comme UTC (été / hiver)
# en voyage, une copie peut porter l'heure locale d'un autre fuseau : n'importe quel nombre entier d'heures,
# mais seulement si durée et distance sont identiques (voir _meme_copie)
DECALAGES_VOYAGE = tuple(h * 3600 for h in range(-12, 13) if h not in (0, 1, 2, -1, -2))


def _relatif(chemin):
    """Chemin d'origine sans son emplacement (dossier du projet, archive sources-brutes.zip ou dossier zips/) :
    identique que les exports soient décompressés ou archivés, pour que les choix ne dépendent pas du rangement."""
    for marque in ("sources-brutes.zip/", "sources-brutes/zips/", "Extracteur_Sport/"):
        if marque in chemin:
            chemin = chemin.split(marque, 1)[1]
            break
    return chemin.replace("convertis/", "zepp-fit/", 1) if chemin.startswith("convertis/") else chemin


def score(s):
    """Plus c'est haut, plus l'exemplaire est fiable."""
    # Une vraie trace GPS l'emporte : les applis sans GPS estiment la distance par les pas (Google Fit : moitié moins
    # que le GPS en médiane sur 224 cas). L'heure, elle, vient d'un exemplaire sûr (voir fusionner).
    points = (PRIORITE.get(s.enregistreur, 10) + (40 if s.original else 0) + (100 if s.gps else 0)
              + (15 if s.cardio else 0) + (10 if s.nb_points >= 60 else 0) + (5 if s.distance else 0))
    if any("interpolée" in q for q in s.qualite):
        points -= 25
    if s.heure_douteuse:
        points -= 20
    return points


def _meme_sortie(a, b, decalage=0):
    debut_b, fin_b = b.debut + timedelta(seconds=decalage), b.fin + timedelta(seconds=decalage)
    if abs((debut_b - a.debut).total_seconds()) <= ECART_DEPART and abs(a.duree - b.duree) <= 0.1 * max(a.duree, b.duree) + 60:
        return True
    commun = (min(a.fin, fin_b) - max(a.debut, debut_b)).total_seconds()
    if commun <= 0:
        return False
    if commun / min(a.duree, b.duree) < RECOUVREMENT:
        return False
    # une courte marche incluse dans une longue séance n'est pas « la même sortie » : durées comparables exigées
    return min(a.duree, b.duree) / max(a.duree, b.duree) >= 0.5


def _douteux_compatible(a, b, decalage):
    """Même sortie décalée d'un nombre entier d'heures (heure locale écrite comme UTC dans une des copies).

    Accepté si une des deux heures est « à vérifier » et que les durées collent (2 % + 30 s), ou, entre deux sources
    sûres (doublon d'été/hiver), si durée et distance sont quasi identiques. Sous 10 min : durée et distance quasi
    identiques exigées dans tous les cas (sinon deux petites marches différentes se confondraient)."""
    if min(a.duree, b.duree) < 600:  # courte séance : seulement si durée ET distance sont quasi identiques
        if not (a.distance and b.distance) or abs(a.distance - b.distance) > 0.03 * max(a.distance, b.distance) + 10:
            return False
        if abs(a.duree - b.duree) > 0.01 * max(a.duree, b.duree) + 3:
            return False
        return (a.heure_douteuse or b.heure_douteuse or abs(a.duree - b.duree) <= 2) and _meme_sortie(a, b, decalage)
    if a.heure_douteuse or b.heure_douteuse:
        meme_distance = a.distance and b.distance and abs(a.distance - b.distance) <= 0.03 * max(a.distance, b.distance) + 10
        # Smashrun compte le temps en mouvement : même distance et durée à 25 % près suffisent
        if meme_distance and abs(a.duree - b.duree) <= 0.25 * max(a.duree, b.duree):
            return _meme_sortie_decalee(a, b, decalage)
        if abs(a.duree - b.duree) > 0.02 * max(a.duree, b.duree) + 30:
            return False
        if a.distance and b.distance and abs(a.distance - b.distance) > 0.1 * max(a.distance, b.distance) + 50:
            return False
    else:
        if abs(a.duree - b.duree) > 0.005 * max(a.duree, b.duree) + 2:
            return False
        if not (a.distance and b.distance) or abs(a.distance - b.distance) > 0.02 * max(a.distance, b.distance) + 10:
            return False
    return _meme_sortie(a, b, decalage)


def _meme_sortie_decalee(a, b, decalage):
    """Départs alignés après décalage (à 10 min près : le temps en mouvement peut démarrer plus tard)."""
    return abs((b.debut + timedelta(seconds=decalage) - a.debut).total_seconds()) <= 600


def _meme_copie(a, b, decalage):
    """Même séance recopiée avec l'heure d'un autre fuseau : durée à 1 % + 3 s et distance (≥ 100 m) à 2 % près."""
    if not (a.distance and b.distance and min(a.distance, b.distance) >= 100):
        return False
    if abs(a.duree - b.duree) > 0.01 * max(a.duree, b.duree) + 3:
        return False
    if abs(a.distance - b.distance) > 0.02 * max(a.distance, b.distance) + 5:
        return False
    return abs((b.debut + timedelta(seconds=decalage) - a.debut).total_seconds()) <= 180


def regrouper(seances):
    """Groupes de séances décrivant la même sortie."""
    # ordre total (et pas seulement par heure) : le résultat ne dépend pas de l'ordre de lecture des fichiers
    seances = sorted(seances, key=lambda s: (s.debut, s.source, _relatif(s.chemin)))
    groupes, ouverts = [], []  # ouverts : groupes dont la fin est proche (fenêtre glissante)
    for s in seances:
        limite = s.debut - timedelta(hours=13)
        ouverts = [g for g in ouverts if g["fin"] >= limite]
        trouve = None
        for g in ouverts:
            for m in g["membres"]:
                if (_meme_sortie(m, s) or any(_douteux_compatible(m, s, d) for d in DECALAGES_DOUTEUX)
                        or any(_meme_copie(m, s, d) for d in DECALAGES_VOYAGE)):
                    trouve = g
                    break
            if trouve:
                break
        if trouve:
            trouve["membres"].append(s)
            trouve["fin"] = max(trouve["fin"], s.fin)
        else:
            g = {"membres": [s], "fin": s.fin}
            groupes.append(g)
            ouverts.append(g)
    return [g["membres"] for g in groupes]


def fusionner(membres):
    """Le meilleur exemplaire, complété par les autres. Renvoie (séance retenue, liste des sources)."""
    # à score égal : plus de points, plus de distance, puis source et chemin (choix stable, quel que soit l'ordre)
    membres = sorted(membres, key=lambda m: (-score(m), -m.nb_points, -(m.distance or 0), m.source, _relatif(m.chemin)))
    meilleur = membres[0]
    sures = [m for m in membres if not m.heure_douteuse]
    if meilleur.heure_douteuse and sures:  # l'heure d'un exemplaire au fuseau connu fait foi
        decalage = sures[0].debut - meilleur.debut
        if abs(decalage.total_seconds()) >= 1800:
            meilleur.debut = sures[0].debut
            meilleur.qualite.append(f"heure corrigée de {decalage.total_seconds() / 3600:+.0f} h d'après {sures[0].source}")
            charger = meilleur._points
            if charger:
                def recale(charger=charger, decalage=decalage):
                    pts = charger()
                    for p in pts:
                        p.temps += decalage
                    return pts
                meilleur._points = recale
    for champ in ("distance", "calories", "fc_moy", "fc_max", "titre"):
        if not getattr(meilleur, champ):
            valeur = next((getattr(m, champ) for m in membres[1:] if getattr(m, champ)), None)
            setattr(meilleur, champ, valeur)
    if meilleur.sous_sport == "generic":
        precis = next((m for m in membres[1:] if m.sport == meilleur.sport and m.sous_sport != "generic"), None)
        if precis:
            meilleur.sous_sport = precis.sous_sport
    if not meilleur.sport_sur:
        autre = next((m for m in membres[1:] if m.sport_sur), None)
        if autre:
            meilleur.sport, meilleur.sous_sport, meilleur.sport_sur = autre.sport, autre.sous_sport, True
            meilleur.qualite.append(f"sport donné par {autre.source}")
    return meilleur, membres


INTERIEUR_MAX = 1500  # m : une séance de montre plus lente que la marche n'est « à l'intérieur » qu'en deçà


def decider_sport(s):
    """Garde, corrige ou écarte selon la vitesse. Renvoie None si la séance est gardée, sinon la raison."""
    if s.sport == "swimming" and s.sous_sport not in ("lap_swimming", "open_water"):
        # pas d'indication : une vraie trace GPS ne se fait qu'en eau libre (le GPS ne passe pas sous un toit
        # et une piscine ne demande pas de tracé) ; sinon piscine
        s.sous_sport = "open_water" if s.gps else "lap_swimming"
        s.qualite.append("eau libre (trace GPS)" if s.gps else "piscine (sans trace GPS)")
    v = s.vitesse()
    if s.sport in ("walking", "hiking", "running", "other") and v and v > VEHICULE:
        return f"vitesse de véhicule ({v:.0f} km/h)"
    if (v and v < VITESSE["walking"][0] and s.distance < INTERIEUR_MAX and s.original and s.enregistreur in APPAREILS_GPS
            and s.sport in ("walking", "hiking", "running", "other")):
        # séance lancée volontairement sur la montre presque sans avancer (à l'intérieur, sur place, confinement
        # d'avril 2020) : une vraie séance au GPS inutile, pas une erreur de données. Au-delà d'INTERIEUR_MAX, c'est
        # une longue sortie entrecoupée de pauses (ex. 5,6 km en 4 h), laissée aux règles ordinaires
        s.qualite.append(f"marche à l'intérieur ou sur place ({v:.1f} km/h, séance lancée sur la montre)")
        s.sport, s.sous_sport = "walking", "indoor_walking"
        return None
    if s.sport == "other":
        if not v:
            return "sport inconnu, sans distance pour le deviner"
        if 2.0 <= v <= 6.5:
            s.sport, s.sous_sport = "walking", "generic"
        elif 7.5 <= v <= 16:
            s.sport, s.sous_sport = "running", "generic"
        else:
            return f"sport inconnu, vitesse ambiguë ({v:.1f} km/h)"
        s.qualite.append(f"sport déduit de la vitesse ({v:.1f} km/h)")
        return None
    if v and s.sous_sport != "treadmill":
        if s.sport in ("running", "walking") and 16 < v <= VEHICULE:
            # montre en mode course ou marche pendant une sortie à vélo (ex. TomTom, 42 km à 20 km/h)
            s.qualite.append(f"« {'course' if s.sport == 'running' else 'marche'} » à {v:.1f} km/h : vélo")
            s.sport, s.sous_sport = "cycling", "generic"
            return None
        if s.sport == "running" and 1.5 <= v < 5.0:
            s.sport = "walking"
            s.qualite.append(f"« course » à {v:.1f} km/h : marche")
        elif s.sport == "walking" and 8.0 < v <= 16:
            s.sport = "running"
            s.qualite.append(f"« marche » à {v:.1f} km/h : course")
        mini, maxi = VITESSE.get(s.sport, (0, 999))
        if not mini <= v <= maxi:
            return f"vitesse invraisemblable pour {s.sport} ({v:.1f} km/h)"
    return None


APPAREILS_GPS = {"Garmin", "Zepp", "TomTom"}  # montres : leur heure et leur trace font foi
ENREGISTREURS_DEDIES = APPAREILS_GPS | {"Runtastic"}  # séance lancée volontairement : peut durer des heures
DUREE_JOURNEE = 4 * 3600  # au-delà, une séance d'appli téléphone (détection automatique, import) est un journal du jour


def raison_journal(s):
    """Une appli téléphone ou une copie qui annonce plus de 4 h d'affilée enregistre la journée, pas une activité
    (Google Fit est apparu fin 2014 : ses « courses » de 11 h de juillet 2014 sont des imports de journées)."""
    if s.duree >= DUREE_JOURNEE and not (s.original and s.enregistreur in ENREGISTREURS_DEDIES):
        return f"journal de la journée ({s.duree / 3600:.1f} h, {s.enregistreur})"
    return None


def raison_journee(s):
    """Totaux journaliers et journées entières déguisés en séance, trajets en véhicule inclus dans une trace."""
    if s.duree >= 2 * 3600 and any("reconstruite en ligne droite" in q for q in s.qualite):
        return f"journée reconstruite en ligne droite ({s.duree / 3600:.1f} h)"
    locale = s.debut.astimezone(PARIS)
    if locale.strftime("%H:%M:%S") == "00:00:00" and s.enregistreur not in APPAREILS_GPS:
        return "total journalier (début à minuit pile)"
    if s.sport in ("walking", "running", "hiking", "cycling", "other") and s.duree >= 4 * 3600 and not s.original:
        return f"journée complète recopiée ({s.duree / 3600:.1f} h)"
    v = s.vitesse()
    if s.sport == "running" and v and s.duree > 1800 and v > 18:
        return f"course à {v:.1f} km/h de moyenne pendant {s.duree / 60:.0f} min : véhicule"
    if s.gps and s.sport in ("walking", "running", "hiking"):
        minutes = _minutes_en_vehicule(s.points())
        if minutes >= 3:
            return f"trajet en véhicule dans la trace ({minutes} min d'affilée au-dessus de 30 km/h)"
    return None


def _minutes_en_vehicule(points):
    """Plus longue suite de minutes consécutives parcourues à plus de 30 km/h (les sauts isolés du GPS ne comptent pas)."""
    gps = [p for p in points if p.lat is not None]
    par_minute = {}
    for a, b in zip(gps, gps[1:]):
        duree = (b.temps - a.temps).total_seconds()
        if 0 < duree <= 120:
            cle = a.temps.replace(second=0, microsecond=0)
            d, t = par_minute.get(cle, (0.0, 0.0))
            par_minute[cle] = (d + fitconv._distance(a, b), t + duree)
    suite = meilleure = 0
    precedente = None
    for minute in sorted(par_minute):
        d, t = par_minute[minute]
        rapide = t >= 30 and d / t * 3.6 > 30
        consecutive = precedente is not None and (minute - precedente).total_seconds() == 60
        suite = (suite + 1 if consecutive else 1) if rapide else 0
        meilleure = max(meilleure, suite)
        precedente = minute
    return meilleure


DUREE_DECOUPAGE = 2 * 3600  # traces GPS plus longues : découpées selon la vitesse
PAUSE_COUPURE = 20 * 60  # s : une pause plus longue sépare deux activités
FAMILLES = {"pied": (1.0, 16.0), "velo": (16.0, 35.0)}  # km/h, vitesse lissée sur 5 min


def _famille(v):
    if v < 1.0:
        return "arret"
    if v <= 16.0:
        return "pied"
    if v <= 35.0:
        return "velo"
    return "vehicule"


def decouper(s):
    """Découpe une longue trace GPS (journée enregistrée d'un bloc) en activités : à pied, vélo ; écarte les trajets
    en véhicule, les portions reconstruites en ligne droite et les longues pauses. Marche et course restent une même famille « à pied » (un trail alterne les
    deux) ; le sport d'un segment à pied est la course si plus de la moitié de ses minutes vont à plus de 7 km/h."""
    from dataclasses import replace

    # seulement un enregistrement d'origine : une copie de plusieurs heures est une journée recopiée (écartée) ;
    # ni une séance dont la montre donne le temps en mouvement : lancée volontairement, ses pauses en font partie
    if not (s.original and s.gps and s.duree >= DUREE_DECOUPAGE and not s.en_mouvement
            and s.sport in ("walking", "running", "hiking", "cycling", "other")):
        return [s]
    pts = [p for p in s.points() if p.lat is not None]
    if len(pts) < 60:
        return [s]
    minutes = {}
    for a, b in zip(pts, pts[1:]):
        duree = (b.temps - a.temps).total_seconds()
        if 0 < duree <= 120:
            cle = a.temps.replace(second=0, microsecond=0)
            d, t = minutes.get(cle, (0.0, 0.0))
            minutes[cle] = (d + fitconv._distance(a, b), t + duree)
    cles = sorted(minutes)
    reconstruites = seances.minutes_reconstruites(pts)
    brutes = [minutes[c][0] / minutes[c][1] * 3.6 if minutes[c][1] else 0.0 for c in cles]
    lisses = [sorted(brutes[max(0, i - 2):i + 3])[len(brutes[max(0, i - 2):i + 3]) // 2] for i in range(len(brutes))]
    # suites de minutes de même famille ; une pause > PAUSE_COUPURE sans point coupe aussi
    segments, courant = [], None
    for cle, v, brute in zip(cles, lisses, brutes):
        famille = "reconstruit" if cle in reconstruites else _famille(v)
        coupure = courant and (cle - courant["fin"]).total_seconds() > PAUSE_COUPURE
        if courant and famille == courant["famille"] and not coupure:
            courant["fin"], courant["vitesses"] = cle, courant["vitesses"] + [brute]
        else:
            courant = {"famille": famille, "debut": cle, "fin": cle, "vitesses": [brute]}
            segments.append(courant)
    # les suites de moins de 5 min (feux rouges, GPS qui saute) rejoignent la précédente
    fusionnes = []
    for seg in segments:
        courte = (seg["fin"] - seg["debut"]).total_seconds() < 300
        if fusionnes and courte and seg["famille"] in ("arret", fusionnes[-1]["famille"], "pied", "velo"):
            fusionnes[-1]["fin"], fusionnes[-1]["vitesses"] = seg["fin"], fusionnes[-1]["vitesses"] + seg["vitesses"]
        elif fusionnes and fusionnes[-1]["famille"] == seg["famille"]:
            fusionnes[-1]["fin"], fusionnes[-1]["vitesses"] = seg["fin"], fusionnes[-1]["vitesses"] + seg["vitesses"]
        else:
            fusionnes.append(seg)
    activites = []
    for seg in fusionnes:
        if seg["famille"] not in ("pied", "velo"):
            continue
        debut, fin = seg["debut"], seg["fin"] + timedelta(minutes=1)
        morceau = [p for p in pts if debut <= p.temps < fin]
        if len(morceau) < POINTS_TRACE:
            continue
        if seg["famille"] == "velo":
            sport = "cycling"
        else:
            rapides = sum(v > 7 for v in seg["vitesses"]) / max(len(seg["vitesses"]), 1)
            sport = "running" if rapides > 0.5 else ("hiking" if s.sport == "hiking" else "walking")
        a = replace(s, debut=morceau[0].temps, duree=(morceau[-1].temps - morceau[0].temps).total_seconds(),
                    distance=sum(fitconv._distance(x, y) for x, y in zip(morceau, morceau[1:])), sport=sport,
                    sous_sport="generic", sport_sur=True, calories=None, nb_points=len(morceau), en_mouvement=None,
                    qualite=s.qualite + [f"découpée d'une trace de {s.duree / 3600:.1f} h"],
                    _points=lambda morceau=morceau: morceau)
        activites.append(a)
    return activites or [s]


def raison_duree(s):
    if s.duree > DUREE_MAX:
        return f"total journalier ou fenêtre trop longue ({s.duree / 3600:.1f} h)"
    if s.duree < DUREE_MIN.get(s.sport, DUREE_MIN_DEFAUT):
        return f"trop courte ({s.duree / 60:.1f} min)"
    return None


TYPES_ACTIVITE = {"hiking": "walking"}  # pour le recollage, une randonnée est une marche
PAUSE_RECOLLAGE = 3 * 60  # s : deux morceaux trop courts séparés de moins de 3 min forment une même sortie
FAMILLES_RECOLLAGE = {"walking": "pied", "hiking": "pied", "running": "pied", "other": "pied", "cycling": "velo"}


def _recollee(morceaux):
    """Une séance faite de morceaux trop courts qui se suivent (détection automatique de pas découpée en minutes)."""
    from dataclasses import replace

    premier = morceaux[0][0]
    fin, distance, en_mouvement = premier.debut, 0.0, 0.0
    for s, _ in morceaux:
        nouveau = (s.fin - max(s.debut, fin)).total_seconds()  # deux copies décalées ne comptent qu'une fois
        if nouveau > 0:
            distance += (s.distance or 0) * nouveau / s.duree
            en_mouvement += nouveau
        fin = max(fin, s.fin)
    durees = Counter()
    for s, _ in morceaux:
        durees[s.sport] += s.duree
    connus = [sp for sp, _ in durees.most_common() if sp != "other"]
    sport = connus[0] if connus else "other"
    enregistreur = Counter(s.enregistreur for s, _ in morceaux).most_common(1)[0][0]

    def points(morceaux=morceaux):
        return sorted((p for s, _ in morceaux for p in s.points()), key=lambda p: p.temps)

    calories = sum(s.calories or 0 for s, _ in morceaux) or None
    return replace(premier, enregistreur=enregistreur, debut=premier.debut, duree=(fin - premier.debut).total_seconds(),
                   distance=distance or None, sport=sport, sous_sport="generic", sport_sur=sport != "other",
                   calories=calories, fc_moy=None, fc_max=None, nb_points=sum(s.nb_points for s, _ in morceaux),
                   gps=any(s.gps for s, _ in morceaux), cardio=False, titre=None,
                   heure_douteuse=any(s.heure_douteuse for s, _ in morceaux), en_mouvement=None,
                   qualite=[f"recollée de {len(morceaux)} morceaux trop courts "
                            f"({en_mouvement / 60:.0f} min en mouvement sur {(fin - premier.debut).total_seconds() / 60:.0f})"],
                   _points=points)


def recoller(courts, gardees):
    """Morceaux trop courts → (activités recollées, morceaux écartés). Les morceaux qui se suivent à moins de
    PAUSE_RECOLLAGE, de même famille (à pied, vélo), forment une sortie ; elle est gardée si elle passe les mêmes
    filtres qu'une séance ordinaire et ne tombe pas pendant une activité déjà retenue (sinon c'en est un bout)."""
    retenues = sorted((s.debut, s.fin, TYPES_ACTIVITE.get(s.sport, s.sport)) for s, _, _ in gardees)
    debuts = [d for d, _, _ in retenues]

    def pendant_une_retenue(r):
        """Un bout d'une activité retenue : la chevauche et du même type (une marche découpée autrement par une
        autre appli), ou recouverte pour plus de moitié. Sinon, une sortie à part qui la touche (ex. 12 min de course
        juste après une marche de montre dont la fin déborde de 4 min)."""
        i = bisect.bisect_left(debuts, r.fin)
        commun = 0.0
        for d, f, type_ in retenues[max(0, i - 50):i]:
            recouvert = max(0.0, (min(f, r.fin) - max(d, r.debut)).total_seconds())
            if recouvert and type_ == TYPES_ACTIVITE.get(r.sport, r.sport):
                return True
            commun += recouvert
        return commun > 0.5 * r.duree

    suites, courante = [], None
    for s, membres in sorted(courts, key=lambda c: (c[0].debut, c[0].source, _relatif(c[0].chemin))):
        famille = FAMILLES_RECOLLAGE.get(s.sport)
        if (courante and famille and famille == courante["famille"]
                and (s.debut - courante["fin"]).total_seconds() <= PAUSE_RECOLLAGE):
            courante["morceaux"].append((s, membres))
            courante["fin"] = max(courante["fin"], s.fin)
        else:
            courante = {"famille": famille, "fin": s.fin, "morceaux": [(s, membres)]}
            suites.append(courante)
    recollees, ecartees = [], []
    for suite in suites:
        morceaux = suite["morceaux"]
        raison = None
        if len(morceaux) > 1:
            r = _recollee(morceaux)
            raison = decider_sport(r)  # d'abord : le sport décide de quel type d'activité ce serait un bout
            if pendant_une_retenue(r):
                raison = "morceau d'une activité déjà retenue"
            else:
                raison = raison or raison_duree(r) or raison_journee(r)
                if not raison:
                    recollees.append((r, [m for _, membres in morceaux for m in membres], None))
                    continue
        for s, membres in morceaux:
            ecartees.append((s, membres, raison_duree(s) if len(morceaux) == 1 or raison.startswith("trop courte")
                             else f"{raison_duree(s)}, recollée : {raison}"))
    return recollees, ecartees


def preparer(seances, journal=print):
    """Toutes les séances → (activités retenues, écartées). Chaque élément : (séance, membres, raison)."""
    gardees, ecartees = [], []
    candidates = []
    for s in seances:  # tri préalable : durées nulles et totaux journaliers
        if s.duree <= 0 or s.duree > DUREE_MAX:
            ecartees.append((s, [s], raison_duree(s) or "sans durée"))
        else:
            candidates.append(s)
    # regroupées à part : une micro-copie d'une vraie sortie changerait le meilleur exemplaire de son groupe
    # (ex. une copie Samsung de 4 min au lieu du vélo de 5 min) ; ses morceaux sont reconnus au recollage
    groupes = (regrouper([s for s in candidates if s.duree >= DUREE_MIN_DEFAUT])
               + regrouper([s for s in candidates if s.duree < DUREE_MIN_DEFAUT]))
    # mêmes exports présents en plusieurs exemplaires et re-synchronisés d'une appli à l'autre :
    # on compte aussi les moments distincts (début à la minute près) pour un chiffre parlant
    longues = {s.debut.replace(second=0, microsecond=0) for s, _, _ in ecartees if s.duree > DUREE_MAX}
    journal(f"  {len(seances)} séances → {len(groupes)} sorties distinctes après regroupement "
            f"({len(ecartees)} fichiers écartés d'emblée : sans durée, ou {len(longues)} totaux de plus de "
            f"{DUREE_MAX // 3600} h une fois copies et re-synchronisations confondues)")
    decoupees, courts = 0, []
    for membres in groupes:
        s, membres = fusionner(membres)
        raison = raison_journal(s)
        if raison:
            ecartees.append((s, membres, raison))
            continue
        # sous le plus petit minimum, la vitesse d'un morceau de quelques secondes ne veut rien dire : il attend le
        # recollage tel quel. Entre 5 et 10 min, le sport est d'abord décidé (une « marche » à 10 km/h est une
        # course, gardée dès 5 min)
        if s.duree < DUREE_MIN_DEFAUT and s.sport in FAMILLES_RECOLLAGE:
            courts.append((s, membres))
            continue
        morceaux = decouper(s)
        decoupees += len(morceaux) > 1 or morceaux[0] is not s
        for m in morceaux:
            raison = decider_sport(m)
            if not raison and raison_duree(m) and m.duree < DUREE_MIN.get(m.sport, DUREE_MIN_DEFAUT) \
                    and m.sport in FAMILLES_RECOLLAGE:
                courts.append((m, membres))
                continue
            raison = raison or raison_duree(m) or raison_journee(m)
            (ecartees if raison else gardees).append((m, membres, raison))
    journal(f"  {decoupees} longue(s) trace(s) GPS découpée(s) selon la vitesse")
    recollees, restes = recoller(courts, gardees)
    gardees += recollees
    ecartees += restes
    journal(f"  {len(courts)} séances trop courtes (détection automatique de pas surtout) : {len(recollees)} sortie(s) "
            f"recollée(s) de leurs morceaux, le reste écarté et listé dans ecartees.csv")
    return gardees, ecartees


CATEGORIES = {  # (sport, sous-sport) → dossier de rangement ; sous-sport absent = tous les autres
    ("running", "treadmill"): "course-tapis", ("running", "trail"): "course-trail", ("running", None): "course",
    ("walking", None): "marche", ("hiking", None): "randonnee",
    ("cycling", "indoor_cycling"): "velo-interieur", ("cycling", None): "velo",
    ("swimming", "lap_swimming"): "natation-piscine", ("swimming", "open_water"): "natation-eau-libre",
    ("swimming", None): "natation-piscine",
    ("training", None): "musculation", ("fitness_equipment", None): "appareils",
    ("cross_country_skiing", None): "ski-de-fond", ("alpine_skiing", None): "ski", ("rowing", None): "aviron",
}


def categorie(s):
    return CATEGORIES.get((s.sport, s.sous_sport)) or CATEGORIES.get((s.sport, None)) or "autres"


def _nom_fichier(s):
    enregistreur = re.sub(r"[^A-Za-z0-9]+", "", s.enregistreur)
    return f"{categorie(s)}/{s.debut:%Y}/{s.debut:%Y-%m-%d_%H%M%S}_{s.sport}_{enregistreur}.fit"


def ecrire(gardees, ecartees, sortie, journal=print):
    """Un FIT par activité retenue + index.csv ; les écartées dans ecartees.csv."""
    sortie.mkdir(parents=True, exist_ok=True)
    lignes, comptes = [], Counter()
    for s, membres, _ in gardees:
        points = [p for p in s.points() if s.debut - timedelta(seconds=5) <= p.temps <= s.fin + timedelta(seconds=5)]
        trace = len(points) >= POINTS_TRACE
        if trace:
            if s.distance and not any(p.dist is not None for p in points) and not any(p.lat is not None for p in points):
                for p in points:  # mesures sans GPS ni distance : distance répartie dans le temps
                    p.dist = s.distance * (p.temps - s.debut).total_seconds() / s.duree
            octets = fitconv.ecrire_fit(None, points, s.calories, s.titre, s.sport, s.sous_sport,
                                        s.distance, s.duree, s.fc_moy, s.fc_max)
        else:
            octets = fitconv.seance_fit(None, s.debut, s.duree, s.distance, s.calories, s.fc_moy, s.fc_max,
                                        s.titre, s.sport, s.sous_sport)
        rel = _nom_fichier(s)
        while rel in comptes:  # deux activités à la même seconde
            rel = rel.replace(".fit", "_b.fit")
        comptes[rel] += 1
        (sortie / rel).parent.mkdir(parents=True, exist_ok=True)
        (sortie / rel).write_bytes(octets)
        lignes.append({
            "fichier": rel, "categorie": categorie(s), "debut_utc": s.debut.isoformat(), "sport": s.sport,
            "sous_sport": s.sous_sport,
            "duree_min": round(s.duree / 60, 1), "distance_km": round(s.distance / 1000, 3) if s.distance else "",
            "vitesse_kmh": round(s.vitesse(), 1) if s.vitesse() else "", "enregistreur": s.enregistreur,
            "source": s.source, "original": s.original, "trace": trace, "gps": s.gps, "cardio": s.cardio,
            "exemplaires": len(membres), "autres_sources": ", ".join(sorted({m.source for m in membres[1:]})),
            "remarques": " ; ".join(s.qualite), "chemin": s.chemin})
    lignes.sort(key=lambda l: l["debut_utc"])
    with (sortie / "index.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, list(lignes[0].keys()))
        w.writeheader()
        w.writerows(lignes)
    with (sortie / "ecartees.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["debut_utc", "sport", "duree_min", "distance_km", "source", "enregistreur", "exemplaires", "raison", "chemin"])
        for s, membres, raison in sorted(ecartees, key=lambda e: e[0].debut):
            w.writerow([s.debut.isoformat(), s.sport, round(s.duree / 60, 1),
                        round(s.distance / 1000, 3) if s.distance else "", s.source, s.enregistreur, len(membres), raison, s.chemin])
    journal(f"  {len(lignes)} activités écrites dans {sortie}, {len(ecartees)} séances écartées (ecartees.csv)")
    return lignes

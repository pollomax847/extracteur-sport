"""Pas, poids et sommeil de toutes les sources, une valeur par jour (utilisé par sport.py).

Sources reconnues, quel que soit le dossier, y compris dans les ZIP : Samsung Health (CSV), Withings, Mi Fit / Zepp (CSV),
Runtastic / adidas Running (JSON), Google Fit (« Résumés quotidiens.csv »), Moves (CSV) et Health Sync (poids et sommeil).
Les pas de Health Sync ne sont pas lus : l'export mélange sans les nommer les comptes de plusieurs applis (25/10/2026 :
41 500 pas en tout, la montre en avait compté 9 121 à 10 h 42) et rien ne permet de les séparer.

Doublons : plusieurs applications comptent souvent la même journée.
- pas : on garde le plus grand total du jour (le téléphone et le bracelet comptent chacun une partie) ;
- poids : la mesure la plus complète du jour (graisse, muscle…), sinon la plus tardive. Les recopies
  quotidiennes de MyFitnessPal et les erreurs de saisie (plus de 8 kg d'écart avec les pesées voisines) sont écartées ;
- sommeil : la nuit la plus détaillée (phases, cardio), sinon la plus longue. Le jour retenu est celui du réveil.
Données vides ou aberrantes écartées : 0 pas ou plus de 150 000, poids hors 30–250 kg, nuit de moins
d'une heure ou de plus de 16 heures.
"""
import csv
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import fichiers

PAS_MAX = 150_000
POIDS_MIN, POIDS_MAX = 30, 250
AGREGATEURS = {"MyFitnessPal", "Google Fit"}  # applis qui recopient les pesées des autres au lieu de mesurer
ECART_POIDS = 8  # kg d'écart avec la médiane des pesées voisines au-delà duquel une pesée est une erreur de saisie
SOMMEIL_MIN, SOMMEIL_MAX = 3600, 16 * 3600
FUSEAU = ZoneInfo("Europe/Paris")  # pour les sources qui donnent une heure locale sans fuseau


@dataclass
class Poids:
    jour: date  # jour local
    heure: datetime  # UTC
    kg: float
    source: str
    graisse: float | None = None  # %
    muscle: float | None = None  # kg
    os: float | None = None  # kg
    eau: float | None = None  # %
    imc: float | None = None

    def details(self):
        return sum(v is not None for v in (self.graisse, self.muscle, self.os, self.eau, self.imc))

    def locale(self):
        return self.heure.astimezone(FUSEAU)


@dataclass
class Sommeil:
    jour: date
    debut: datetime | None
    fin: datetime | None
    total: int
    source: str
    profond: int | None = None
    leger: int | None = None
    rem: int | None = None
    eveil: int | None = None
    reveils: int | None = None
    fc_moy: int | None = None
    fc_min: int | None = None
    fc_max: int | None = None
    decalage: timedelta = field(default=timedelta(0))  # fuseau local, pour la date et l'heure locale

    def details(self):
        return sum(v is not None for v in (self.debut, self.profond, self.leger, self.rem, self.eveil, self.fc_moy))


def _nom(fichier):
    return unicodedata.normalize("NFC", fichier.name)


def _csv(fichier, ligne_titre=False):
    with fichier.open(encoding="utf-8-sig", newline="") as f:
        if ligne_titre:
            next(f)  # Samsung : 1re ligne = nom de la table et version
        yield from csv.DictReader(f)


def _nombre(texte):
    try:
        valeur = float(texte)
    except (TypeError, ValueError):
        return None
    return valeur if valeur == valeur and valeur != 0 else None  # NaN et 0 = pas de mesure


def _ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _decalage_samsung(texte):
    """« UTC+0100 » → timedelta(hours=1)."""
    if not texte or len(texte) < 8:
        return timedelta(0)
    signe = -1 if texte[3] == "-" else 1
    return signe * timedelta(hours=int(texte[4:6]), minutes=int(texte[6:8]))


def _samsung_heure(texte):
    return datetime.fromisoformat(texte).replace(tzinfo=timezone.utc)  # les heures Samsung sont en UTC


# --- lecteurs : chacun produit des tuples ("pas", jour, nombre, source), ("poids", Poids) ou ("sommeil", Sommeil)

def _lire(fichier):
    nom = _nom(fichier)
    parent = unicodedata.normalize("NFC", fichier.parent.name)

    if nom.startswith(("com.samsung.shealth.tracker.pedometer_day_summary.", "com.samsung.shealth.activity.day_summary.")):
        for l in _csv(fichier, True):
            pas = _nombre(l.get("step_count"))
            if pas and l.get("day_time"):
                yield "pas", _ms(int(l["day_time"])).date(), int(pas), "Samsung"

    elif nom.startswith("com.samsung.health.weight.") and nom.endswith(".csv"):
        for l in _csv(fichier, True):
            kg = _nombre(l.get("weight"))
            if kg and l.get("start_time"):
                heure = _samsung_heure(l["start_time"])
                yield "poids", Poids((heure + _decalage_samsung(l.get("time_offset"))).date(), heure, kg, "Samsung",
                                     graisse=_nombre(l.get("body_fat")), muscle=_nombre(l.get("muscle_mass")))

    elif nom.startswith("com.samsung.shealth.sleep.") and nom.endswith(".csv"):
        P = "com.samsung.health.sleep."
        for l in _csv(fichier, True):
            if l.get(P + "start_time") and l.get(P + "end_time"):
                debut, fin = _samsung_heure(l[P + "start_time"]), _samsung_heure(l[P + "end_time"])
                decalage = _decalage_samsung(l.get(P + "time_offset"))
                yield "sommeil", Sommeil((fin + decalage).date(), debut, fin, int((fin - debut).total_seconds()),
                                         "Samsung", decalage=decalage)

    elif nom == "aggregates_steps.csv":  # Withings
        for l in _csv(fichier):
            pas = _nombre(l.get("value"))
            if pas:
                yield "pas", date.fromisoformat(l["date"]), int(pas), "Withings"

    elif nom == "weight.csv" and "Poids (kg)" in fichiers.texte(fichier, 200):  # Withings
        for l in _csv(fichier):
            kg = _nombre(l.get("Poids (kg)"))
            if kg:
                locale = datetime.fromisoformat(l["Date"]).replace(tzinfo=FUSEAU)  # heure locale, sans fuseau
                gras, eau = _nombre(l.get("Gras (kg)")), _nombre(l.get("Hydratation (kg)"))
                yield "poids", Poids(locale.date(), locale.astimezone(timezone.utc), kg, "Withings", graisse=gras and 100 * gras / kg,
                                     muscle=_nombre(l.get("Masse musculaire (kg)")),
                                     os=_nombre(l.get("Masse osseuse (kg)")), eau=eau and 100 * eau / kg)

    elif nom == "sleep.csv" and "profond (s)" in fichiers.texte(fichier, 300):  # Withings
        for l in _csv(fichier):
            debut_local, fin_local = datetime.fromisoformat(l["Début"]), datetime.fromisoformat(l["Fin"])
            phases = {k: int(_nombre(l.get(c)) or 0) for k, c in
                      (("leger", "léger (s)"), ("profond", "profond (s)"), ("rem", "rem (s)"), ("eveil", "éveillé (s)"))}
            total = phases["leger"] + phases["profond"] + phases["rem"] or int((fin_local - debut_local).total_seconds())
            fc = [int(_nombre(l.get(c)) or 0) or None for c in
                  ("Rythme cardiaque moyen", "Rythme cardiaque (min)", "Rythme cardiaque (max)")]
            yield "sommeil", Sommeil(fin_local.date(), debut_local.astimezone(timezone.utc), fin_local.astimezone(timezone.utc),
                                     total, "Withings", **{k: v or None for k, v in phases.items()},
                                     reveils=int(_nombre(l.get("réveils")) or 0) or None,
                                     fc_moy=fc[0], fc_min=fc[1], fc_max=fc[2], decalage=fin_local.utcoffset())

    elif parent == "ACTIVITY" and nom.startswith("ACTIVITY_"):  # Mi Fit / Zepp
        for l in _csv(fichier):
            pas = _nombre(l.get("steps"))
            if pas:
                yield "pas", date.fromisoformat(l["date"]), int(pas), "Mi Fit"

    elif parent == "BODY" and nom.startswith("BODY_"):
        for l in _csv(fichier):
            kg = _nombre(l.get("weight"))
            if kg:
                heure = datetime.fromtimestamp(int(l["timestamp"]), timezone.utc)
                yield "poids", Poids(heure.astimezone(FUSEAU).date(), heure, kg, "Mi Fit", graisse=_nombre(l.get("fatRate")),
                                     os=_nombre(l.get("boneMass")), eau=_nombre(l.get("bodyWaterRate")),
                                     imc=_nombre(l.get("bmi")))

    elif parent == "SLEEP" and nom.startswith("SLEEP_"):
        for l in _csv(fichier):
            profond, leger = int(_nombre(l.get("deepSleepTime")) or 0) * 60, int(_nombre(l.get("shallowSleepTime")) or 0) * 60
            if profond + leger and l.get("start") != l.get("stop"):
                debut = datetime.fromtimestamp(int(l["start"]), timezone.utc)
                fin = datetime.fromtimestamp(int(l["stop"]), timezone.utc)
                yield "sommeil", Sommeil(date.fromisoformat(l["date"]), debut, fin, profond + leger, "Mi Fit",
                                         profond=profond or None, leger=leger or None,
                                         eveil=int(_nombre(l.get("wakeTime")) or 0) * 60 or None)

    elif fichier.suffix == ".json" and parent in ("Daily-step-sessions", "daily-steps", "Weight", "Sleep-sessions"):
        d = json.loads(fichier.read_text())  # Runtastic : heures en ms UTC + décalage du fuseau en ms
        if "start_time" not in d:
            return
        decalage = timedelta(milliseconds=d.get("start_time_timezone_offset") or 0)
        debut = _ms(d["start_time"])
        if "steps" in d and d["steps"]:
            yield "pas", (debut + decalage).date(), int(d["steps"]), "Runtastic"
        elif "weight" in d and d["weight"]:
            # MyFitnessPal recopie chaque jour le dernier poids saisi : voir _vraies_pesees()
            source = "MyFitnessPal" if d.get("origin_type") == "my_fitness_pal" else "Runtastic"
            yield "poids", Poids((debut + decalage).date(), debut, float(d["weight"]), source)
        elif parent == "Sleep-sessions" and d.get("end_time"):
            fin = _ms(d["end_time"])
            phases = {k: int((d.get(c) or 0) / 1000) or None for k, c in
                      (("profond", "deep_sleep_duration"), ("leger", "light_sleep_duration"), ("eveil", "wake_duration"))}
            yield "sommeil", Sommeil((fin + decalage).date(), debut, fin, int((fin - debut).total_seconds()),
                                     "Runtastic", **phases, decalage=decalage)

    elif nom.startswith("Poids ") and nom.endswith(".csv") and fichiers.texte(fichier, 30).startswith("Date,Heure,Poids"):
        for l in _csv(fichier):  # Health Sync (export Health Connect) : heure locale sans fuseau
            kg = _nombre(l.get("Poids"))
            if kg:
                locale = datetime.strptime(l["Date"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=FUSEAU)
                yield "poids", Poids(locale.date(), locale.astimezone(timezone.utc), kg, "Health Sync",
                                     graisse=_nombre(l.get("Pourcentage de graisse corporelle")),
                                     muscle=_nombre(l.get("Masse musculaire")), os=_nombre(l.get("Masse osseuse")))

    elif nom.startswith("Sommeil ") and nom.endswith(".csv") and fichiers.texte(fichier, 50).startswith("Date,Heure,Durée en secondes"):
        yield from _sommeil_health_sync(fichier)

    elif nom == "Résumés quotidiens.csv":  # Google Fit (Takeout)
        for l in _csv(fichier):
            jour = date.fromisoformat(l["Date"])
            pas = _nombre(l.get("Nombre de pas"))
            if pas:
                yield "pas", jour, int(pas), "Google Fit"
            kg = _nombre(l.get("Poids moyen (kg)"))
            if kg:
                midi = datetime(jour.year, jour.month, jour.day, 12, tzinfo=FUSEAU)  # moyenne du jour : heure inconnue
                yield "poids", Poids(jour, midi.astimezone(timezone.utc), kg, "Google Fit")
            sommeil = _nombre(l.get('Durée de l\'activité "Sommeil" (ms)'))
            if sommeil:
                yield "sommeil", Sommeil(jour, None, None, int(sommeil / 1000), "Google Fit")

    elif fichier.suffix == ".csv" and "moves" in str(fichier.parent).lower():
        if not fichiers.texte(fichier, 60).startswith("Date,Activity,Group,Duration,Distance,Steps"):
            return
        par_jour = {}
        for l in _csv(fichier):
            pas = _nombre(l.get("Steps"))
            if pas:
                jour = datetime.strptime(l["Date"], "%d/%m/%y").date()
                par_jour[jour] = par_jour.get(jour, 0) + int(pas)
        for jour, pas in par_jour.items():
            yield "pas", jour, pas, "Moves"


PHASES_HEALTH_SYNC = {"deep": "profond", "light": "leger", "rem": "rem", "awake": "eveil"}


def _sommeil_health_sync(fichier):
    """Health Sync (export Health Connect) : une ligne par phase, heure locale sans fuseau. Deux applis y écrivent souvent
    la même nuit à une ou deux minutes d'écart (nuit du 24 au 25/10/2026 : 998 min de phases pour 9,6 h au lit, réveil
    à 07:11 comme sur la montre) : chaque minute ne compte qu'une fois, avec la phase de la première ligne qui la couvre.
    Une nuit = des phases séparées de moins de 2 h."""
    lignes = sorted((datetime.strptime(l["Date"], "%Y.%m.%d %H:%M:%S").replace(second=0),
                     int(_nombre(l.get("Durée en secondes")) or 0), (l.get("Phase de sommeil") or "").strip().lower())
                    for l in _csv(fichier))
    minutes = {}
    for debut, duree, phase in lignes:
        for m in range(duree // 60):
            minutes.setdefault(debut + timedelta(minutes=m), phase)
    nuits = []
    for m in sorted(minutes):
        if nuits and m - nuits[-1][-1] <= timedelta(hours=2):
            nuits[-1].append(m)
        else:
            nuits.append([m])
    for nuit in nuits:
        duree_phase = {}
        for m in nuit:
            cle = PHASES_HEALTH_SYNC.get(minutes[m], "dormi")
            duree_phase[cle] = duree_phase.get(cle, 0) + 60
        total = sum(v for k, v in duree_phase.items() if k != "eveil")
        # une seule phase sur toute la nuit (ex. « deep » pendant 8 h) : pas un vrai détail des phases
        phases = {k: duree_phase.get(k) for k in ("profond", "leger", "rem", "eveil")}
        if len({"profond", "leger", "rem"} & duree_phase.keys()) < 2:
            phases = {}
        debut, fin = nuit[0].replace(tzinfo=FUSEAU), (nuit[-1] + timedelta(minutes=1)).replace(tzinfo=FUSEAU)
        yield "sommeil", Sommeil(fin.date(), debut.astimezone(timezone.utc), fin.astimezone(timezone.utc), total,
                                 "Health Sync", **phases, decalage=fin.utcoffset())


def collecter(racines, exclus=()):
    """Lit toutes les racines et renvoie (pas, poids, sommeil), une entrée par jour, et le nombre de valeurs lues."""
    pas, poids, sommeil, pesees = {}, {}, {}, []
    lus = {"pas": 0, "poids": 0, "sommeil": 0, "écartés": 0}
    for racine in racines:
        if not racine.is_dir():
            continue
        for fichier in fichiers.parcourir(racine, exclus):  # dossiers et contenu des ZIP
            if fichier.suffix not in (".csv", ".json"):
                continue
            try:
                valeurs = list(_lire(fichier))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                continue  # fichier d'un autre format qui porte le même nom
            for v in valeurs:
                lus[v[0]] += 1
                if v[0] == "pas":
                    _, jour, n, source = v
                    if not 0 < n <= PAS_MAX:
                        lus["écartés"] += 1
                    elif n > pas.get(jour, (0, ""))[0]:
                        pas[jour] = (n, source)
                elif v[0] == "poids":
                    if POIDS_MIN <= v[1].kg <= POIDS_MAX:
                        pesees.append(v[1])
                    else:
                        lus["écartés"] += 1
                else:
                    s = v[1]
                    if not SOMMEIL_MIN <= s.total <= SOMMEIL_MAX:
                        lus["écartés"] += 1
                    elif s.jour not in sommeil or (s.details(), s.total) > (sommeil[s.jour].details(), sommeil[s.jour].total):
                        sommeil[s.jour] = s
    gardees = _vraies_pesees(pesees)
    lus["écartés"] += len(pesees) - len(gardees)
    for p in gardees:
        if p.jour not in poids or (p.details(), p.heure) > (poids[p.jour].details(), poids[p.jour].heure):
            poids[p.jour] = p
    return pas, poids, sommeil, lus


def _vraies_pesees(pesees):
    """Écarte les pesées qui n'en sont pas :
    - MyFitnessPal (via Runtastic) et Google Fit répètent le dernier poids connu : seul le jour où la valeur change compte ;
    - une valeur à plus de ECART_POIDS kg de la médiane des pesées des 60 jours autour est une erreur de saisie ;
    - la même pesée recopiée d'une appli à l'autre ne compte qu'une fois."""
    pesees = sorted(pesees, key=lambda p: p.heure)
    gardees, precedent = [], {}
    for p in pesees:
        if p.source in AGREGATEURS:
            if p.kg == precedent.get(p.source):
                continue
            precedent[p.source] = p.kg
        gardees.append(p)
    sans_aberrantes = []
    for p in gardees:
        voisines = sorted(q.kg for q in gardees if q is not p and abs((q.jour - p.jour).days) <= 60)
        if len(voisines) >= 3 and abs(p.kg - voisines[len(voisines) // 2]) > ECART_POIDS:
            continue
        sans_aberrantes.append(p)
    # Une même pesée recopiée par une autre appli (souvent le lendemain) : même poids à 0,1 kg près, à 2 jours près.
    # On garde la plus détaillée, puis celle d'une balance ou d'une saisie plutôt que d'un agrégateur, puis la plus ancienne.
    resultat = []
    for p in sorted(sans_aberrantes, key=lambda p: (-p.details(), p.source in AGREGATEURS, p.heure)):
        if not any(q.source != p.source and abs(q.kg - p.kg) <= 0.1 and abs((q.jour - p.jour).days) <= 2 for q in resultat):
            resultat.append(p)
    return resultat


# --- formats d'envoi vers Endurain (API /health/…)

def json_pas(jour, n):
    return {"date": jour.isoformat(), "steps": n}


def json_poids(p):
    corps = {"date": p.jour.isoformat(), "weight": round(p.kg, 2)}
    for cle, valeur in (("body_fat", p.graisse), ("muscle_mass", p.muscle), ("bone_mass", p.os),
                        ("body_water", p.eau), ("bmi", p.imc)):
        if valeur is not None and 0 < valeur < 100:
            corps[cle] = round(float(valeur), 2)
    return corps


def json_sommeil(s):
    corps = {"date": s.jour.isoformat(), "total_sleep_seconds": s.total}
    if s.debut and s.fin:
        corps.update(sleep_start_time_gmt=s.debut.replace(tzinfo=None).isoformat(),
                     sleep_end_time_gmt=s.fin.replace(tzinfo=None).isoformat(),
                     sleep_start_time_local=(s.debut + s.decalage).replace(tzinfo=None).isoformat(),
                     sleep_end_time_local=(s.fin + s.decalage).replace(tzinfo=None).isoformat())
    for cle, valeur in (("deep_sleep_seconds", s.profond), ("light_sleep_seconds", s.leger), ("rem_sleep_seconds", s.rem),
                        ("awake_sleep_seconds", s.eveil), ("awake_count", s.reveils)):
        if valeur is not None:
            corps[cle] = int(valeur)
    for cle, valeur in (("avg_heart_rate", s.fc_moy), ("min_heart_rate", s.fc_min), ("max_heart_rate", s.fc_max)):
        if valeur and 20 <= valeur <= 220:
            corps[cle] = int(valeur)
    return corps


def ecrire_csv(dossier, pas, poids, sommeil):
    """Copie lisible des valeurs retenues (sante/pas.csv, poids.csv, sommeil.csv)."""
    dossier.mkdir(parents=True, exist_ok=True)
    with (dossier / "pas.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "pas", "source"])
        w.writerows([j.isoformat(), n, s] for j, (n, s) in sorted(pas.items()))
    with (dossier / "poids.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "kg", "graisse_pct", "muscle_kg", "source"])
        w.writerows([j.isoformat(), p.kg, p.graisse or "", p.muscle or "", p.source] for j, p in sorted(poids.items()))
    with (dossier / "sommeil.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "debut_utc", "fin_utc", "total_h", "profond_h", "leger_h", "rem_h", "source"])
        h = lambda s: round(s / 3600, 2) if s else ""
        w.writerows([j.isoformat(), s.debut.isoformat() if s.debut else "", s.fin.isoformat() if s.fin else "",
                     h(s.total), h(s.profond), h(s.leger), h(s.rem), s.source] for j, s in sorted(sommeil.items()))

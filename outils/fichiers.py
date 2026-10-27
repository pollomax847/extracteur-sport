"""Parcours des fichiers d'une source, y compris le contenu des archives ZIP (utilisé par convertir.py et sante.py).

Les membres d'un ZIP sont rendus comme des zipfile.Path, qui s'utilisent comme des Path :
.name, .suffix, .stem, .parent, « / », .exists(), .read_text(), .open().
"""
import os
import zipfile
from pathlib import Path


def parcourir(racine, exclus=(), sortie=None):
    """Tous les fichiers sous racine, puis ceux contenus dans chaque .zip (sans ouvrir les ZIP imbriqués).

    exclus : noms de dossiers de premier niveau à ignorer ; sortie : dossier où l'on écrit, jamais relu.
    """
    for dossier, sous_dossiers, noms in os.walk(racine):
        dossier = Path(dossier)
        # les dossiers exclus ne sont même pas parcourus
        sous_dossiers[:] = sorted(d for d in sous_dossiers
                                  if not (dossier == racine and d in exclus) and dossier / d != sortie)
        for nom in sorted(noms):
            fichier = dossier / nom
            if fichier.suffix.lower() != ".zip":
                yield fichier
                continue
            try:
                archive = zipfile.ZipFile(fichier)
            except (zipfile.BadZipFile, OSError):
                continue
            # une seule racine par archive : les recherches (exists, /) partagent l'index des noms
            racine_zip = zipfile.Path(archive)
            for membre in archive.namelist():
                if not membre.endswith("/"):
                    yield racine_zip / membre


def texte(fichier, debut=None):
    """Contenu texte (les n premiers caractères si debut est donné), pour un Path comme pour un membre de ZIP."""
    with fichier.open("r", encoding="utf-8-sig", errors="replace") as f:
        return f.read(debut) if debut else f.read()

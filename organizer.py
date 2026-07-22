"""
Nettoyeur intelligent du dossier Telechargements
=================================================

Analyse le dossier Telechargements et range automatiquement les fichiers par
categorie (PDF, Images, Archives, Installateurs, Videos, Audio, fichiers
anciens a verifier).

Fonctions :
  - mode simulation (dry-run) : previsualiser les deplacements sans rien deplacer
  - historique des deplacements (fichier JSON persistant)
  - annulation (undo) du dernier lot de deplacements
  - exclusions personnalisees (extensions, noms de fichiers, motifs)
  - aucune suppression automatique : uniquement des deplacements

Utilisable en ligne de commande (CLI) ou via l'interface graphique (GUI).
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_DIR = Path.home() / ".download_organizer"
HISTORY_FILE = APP_DIR / "history.json"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "app.log"

logger = logging.getLogger("download_organizer")


def _ensure_logging_configured() -> None:
    if logger.handlers:
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

DEFAULT_CATEGORIES = {
    "PDF": {
        "extensions": [".pdf"],
        "target": "Documents/PDF",
    },
    "Images": {
        "extensions": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tiff"],
        "target": "Images",
    },
    "Archives": {
        "extensions": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"],
        "target": "Archives",
    },
    "Installateurs": {
        "extensions": [".exe", ".msi", ".msix", ".pkg", ".dmg", ".apk", ".appimage"],
        "target": "Installateurs",
    },
    "Videos": {
        "extensions": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"],
        "target": "Videos",
    },
    "Audio": {
        "extensions": [".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma", ".m4a"],
        "target": "Audio",
    },
}

# Fichiers plus vieux que ce seuil (en jours) et qui ne correspondent a
# aucune categorie ci-dessus sont ranges dans "A verifier".
OLD_FILE_THRESHOLD_DAYS = 90
OLD_FILES_TARGET = "A verifier"

# Fichiers dont le CONTENU (hash SHA-256) est identique a un autre fichier -
# soit un autre fichier du meme lot, soit un fichier deja range du meme nom -
# sont ranges ici plutot que dupliques sous un nom numerote. Detection par
# contenu, pas seulement par nom : "rapport.pdf" et "rapport (1).pdf"
# telecharges deux fois par le navigateur sont bien reconnus comme doublons.
DUPLICATES_TARGET = "Doublons"
HASH_CHUNK_SIZE = 1024 * 1024

# Taille (en octets) de l'echantillon lu en debut ET en fin de fichier pour
# le hash "rapide" utilise comme PRE-FILTRE avant de decider quels fichiers
# meritent un hash SHA-256 complet (voir _partial_hash / _find_duplicates_
# within_batch). Ce hash partiel ne sert JAMAIS a lui seul a conclure a un
# doublon : deux fichiers de meme taille dont le debut+fin different ne
# peuvent pas etre identiques (donc jamais compares par hash complet, ce
# qui evite l'essentiel des lectures completes des que beaucoup de fichiers
# partagent une taille proche - captures d'ecran, factures PDF...), mais
# deux fichiers dont le debut+fin CONCORDENT restent toujours verifies par
# un hash SHA-256 complet avant toute conclusion, exactement comme avant
# cette optimisation.
PARTIAL_HASH_SAMPLE_SIZE = 64 * 1024

# Motifs toujours exclus, independamment de la configuration utilisateur.
# Non modifiables depuis la GUI : evite qu'un champ "Motifs" vide par megarde
# ne desactive la protection de fichiers systeme/temporaires.
ALWAYS_EXCLUDED_PATTERNS = ["*.crdownload", "*.part", "*.tmp", "desktop.ini", "*.download"]

# ---------------------------------------------------------------------------
# Reconnaissance par signature de fichier (magic bytes)
# ---------------------------------------------------------------------------
# Contrairement a un tri par simple extension, ceci lit les premiers octets
# du fichier pour identifier son type reel. Deux usages :
#  1. classer correctement un fichier sans extension ou a l'extension
#     inconnue, si sa signature correspond a un type reconnu ;
#  2. detecter un fichier dont l'extension NE correspond PAS a son contenu
#     reel (ex: un .exe renomme en .pdf) et le signaler pour verification
#     manuelle plutot que de le classer aveuglement par son extension.
SIGNATURE_READ_SIZE = 16

# Extensions "conteneur ZIP" legitimes qui ne sont pas des archives au sens
# ou l'utilisateur l'entend (docx/xlsx/apk sont techniquement des fichiers
# ZIP). Pour ces extensions, on ne se fie pas a une signature ZIP generique
# pour reclassifier le fichier en "Archives" : on laisse le comportement
# habituel (extension non reconnue -> ancien/exclu) plutot que de mal ranger
# un document bureautique.
ZIP_LIKE_NON_ARCHIVE_EXTENSIONS = {
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".epub", ".jar", ".apk", ".aar", ".xpi", ".vsdx",
}

# Extensions deja rangees dans DEFAULT_CATEGORIES qui sont, elles aussi,
# techniquement des conteneurs ZIP (apk, msix) : leur signature detectee sera
# "Archives" alors que leur categorie voulue est "Installateurs". Ce n'est
# pas une incoherence a signaler, c'est le format normal de ces installateurs.
EXPECTED_SIGNATURE_OVERRIDES = {
    (".apk", "Archives"): "Installateurs",
    (".msix", "Archives"): "Installateurs",
}


def _detect_file_signature(path: Path, cache: Optional[dict] = None) -> Optional[str]:
    """Identifie le type reel d'un fichier via ses premiers octets (magic
    bytes), independamment de son extension. Renvoie une cle de
    DEFAULT_CATEGORIES ou None si le type n'est pas reconnu (auquel cas le
    fichier suit le classement normal par extension/anciennete)."""
    if cache is not None and path in cache:
        return cache[path]
    try:
        with open(path, "rb") as f:
            header = f.read(SIGNATURE_READ_SIZE)
    except OSError:
        header = b""

    result = None
    if header.startswith(b"%PDF-"):
        result = "PDF"
    elif (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
        or header.startswith(b"BM")
    ):
        result = "Images"
    elif (
        header.startswith(b"Rar!\x1a\x07")
        or header.startswith(b"7z\xbc\xaf\x27\x1c")
        or header.startswith(b"\x1f\x8b")
        or header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    ):
        result = "Archives"
    elif header.startswith(b"MZ"):
        result = "Installateurs"

    if cache is not None:
        cache[path] = result
    return result

DEFAULT_WATCH_INTERVAL_SECONDS = 20

DEFAULT_CONFIG = {
    "downloads_dir": str(Path.home() / "Downloads"),
    "base_target_dir": str(Path.home()),
    "old_file_threshold_days": OLD_FILE_THRESHOLD_DAYS,
    "watch_interval_seconds": DEFAULT_WATCH_INTERVAL_SECONDS,
    "exclusions": {
        "extensions": [],       # ex: [".tmp", ".crdownload"]
        "filenames": [],        # ex: ["ne_pas_toucher.pdf"]
        "patterns": [],         # motifs personnalises additionnels (ex: "*.bak")
    },
    # Categories additionnelles definies par l'utilisateur (voir
    # get_effective_categories) : [{"name": str, "extensions": [str], "target": str}, ...]
    "custom_categories": [],
}


def _atomic_write_json(path: Path, data) -> None:
    """Ecrit un fichier JSON de maniere atomique (fichier temporaire + remplacement),
    pour eviter un fichier tronque/corrompu en cas de crash ou coupure pendant l'ecriture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Equivalent de _atomic_write_json pour du texte brut deja serialise
    (utilise par l'historique au format JSONL, une ligne = un lot)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _quarantine_corrupted_file(path: Path) -> None:
    """Renomme un fichier illisible/corrompu au lieu de le perdre silencieusement,
    pour permettre a l'utilisateur de recuperer ses donnees manuellement si besoin."""
    try:
        backup = path.with_suffix(path.suffix + f".corrompu-{int(time.time())}.bak")
        path.replace(backup)
        _ensure_logging_configured()
        logger.warning("Fichier corrompu mis en quarantaine : %s -> %s", path, backup)
    except OSError:
        pass


def _is_safe_relative_target(target: str) -> bool:
    """Rejette tout dossier cible qui n'est pas un sous-chemin relatif sur.
    Un target absolu (lecteur ou racine) ou contenant '..' pourrait, une
    fois combine avec base_target_dir via l'operateur `/` de pathlib,
    produire un chemin totalement hors de l'arborescence de destination
    prevue (voir _is_within_base pour le garde-fou complementaire applique
    au chemin final resolu, qui attrape aussi les cas qu'is_absolute() ne
    detecte pas sous Windows, comme un chemin "enracine" sans lecteur)."""
    path = PurePath(target)
    if path.is_absolute() or path.drive:
        return False
    if ".." in path.parts:
        return False
    return True


def _is_valid_custom_categories(value) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            return False
        if not isinstance(item.get("target"), str) or not item["target"].strip():
            return False
        if not _is_safe_relative_target(item["target"]):
            return False
        extensions = item.get("extensions")
        if not isinstance(extensions, list) or not extensions or not all(isinstance(e, str) for e in extensions):
            return False
        name_patterns = item.get("name_patterns", [])
        if not isinstance(name_patterns, list) or not all(isinstance(p, str) for p in name_patterns):
            return False
    return True


def get_effective_categories(config: dict) -> dict:
    """Combine DEFAULT_CATEGORIES et les categories personnalisees ajoutees
    par l'utilisateur (config["custom_categories"]) en un seul dict pret a
    l'emploi {nom: {"extensions": [...], "target": str, "name_patterns": [...]}}.
    Une categorie personnalisee ne peut jamais reutiliser le nom d'une
    categorie integree (elle serait alors simplement ignoree) - les
    categories de base (et la detection par signature de fichier des
    quatre d'entre elles qui en beneficient : PDF, Images, Archives,
    Installateurs) restent inchangees, seule une extension de la liste
    est possible via l'ajout de nouvelles categories.

    `name_patterns` (optionnel, glob via fnmatch) permet de router un
    fichier vers cette categorie d'apres son NOM plutot que sa seule
    extension (ex: "facture*.pdf" -> categorie "Factures"), teste en
    priorite sur toute correspondance par extension - y compris celle
    d'une categorie integree (voir _category_for)."""
    effective = copy.deepcopy(DEFAULT_CATEGORIES)
    for custom in config.get("custom_categories", []):
        if not isinstance(custom, dict):
            continue
        name = str(custom.get("name", "")).strip()
        target = str(custom.get("target", "")).strip()
        extensions = [
            e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
            for e in custom.get("extensions", []) if isinstance(e, str) and e.strip()
        ]
        name_patterns = [
            p.strip() for p in custom.get("name_patterns", []) if isinstance(p, str) and p.strip()
        ]
        if name and target and extensions and name not in effective:
            effective[name] = {"extensions": extensions, "target": target, "name_patterns": name_patterns}
    return effective


def _is_valid_exclusions(value) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("extensions", "filenames", "patterns"):
        items = value.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            return False
    return True


def _merge_config_data(data: dict) -> dict:
    """Fusionne un dict de configuration (venant du disque ou d'un import
    utilisateur) sur les valeurs par defaut, en ne retenant que les champs
    connus et correctement types - jamais de confiance aveugle dans un
    fichier JSON externe, qu'il vienne d'une session precedente ou d'un
    fichier importe depuis un autre ordinateur."""
    if not isinstance(data, dict):
        raise ValueError("Le contenu ne represente pas une configuration valide (objet JSON attendu).")

    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key in ("downloads_dir", "base_target_dir", "old_file_threshold_days", "watch_interval_seconds"):
        expected_type = type(DEFAULT_CONFIG[key])
        value = data.get(key)
        # bool est une sous-classe d'int : on l'exclut explicitement pour
        # qu'un champ numerique corrompu en "true"/"false" soit rejete.
        if key in data and isinstance(value, expected_type) and not isinstance(value, bool):
            merged[key] = value
    if _is_valid_exclusions(data.get("exclusions")):
        merged["exclusions"].update(data["exclusions"])
    if _is_valid_custom_categories(data.get("custom_categories")):
        merged["custom_categories"] = data["custom_categories"]
    return merged


def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return _merge_config_data(data)
        except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError):
            _ensure_logging_configured()
            logger.warning("config.json invalide ou corrompu, restauration des valeurs par defaut.", exc_info=True)
            _quarantine_corrupted_file(CONFIG_FILE)
    try:
        save_config(DEFAULT_CONFIG)
    except OSError:
        _ensure_logging_configured()
        logger.warning("Impossible d'ecrire la configuration initiale sur disque.", exc_info=True)
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    _atomic_write_json(CONFIG_FILE, config)


def export_config(config: dict, output_path: Path) -> None:
    """Exporte la configuration actuelle vers un fichier JSON choisi par
    l'utilisateur (transfert vers un autre PC, sauvegarde manuelle)."""
    _atomic_write_json(Path(output_path), config)


def import_config(input_path: Path) -> dict:
    """Lit et valide un fichier de configuration exporte, avec exactement
    les memes garde-fous que le chargement normal (champs inconnus ignores,
    types incorrects rejetes) - un fichier importe n'est pas plus digne de
    confiance qu'un config.json corrompu trouve sur disque."""
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    return _merge_config_data(data)


# ---------------------------------------------------------------------------
# Historique
# ---------------------------------------------------------------------------
#
# Stockage au format JSONL (un objet JSON par ligne, un lot par ligne) dans
# HISTORY_FILE avec le suffixe ".jsonl" a la place de ".json" - jamais un
# chemin fige a l'import : il est recalcule depuis HISTORY_FILE a chaque
# appel (voir _history_jsonl_path), pour que les tests qui redirigent
# HISTORY_FILE vers un dossier temporaire continuent de fonctionner sans
# modification.
#
# Avant cette optimisation, chaque lot reel reecrivait l'INTEGRALITE du
# fichier history.json (relecture + reserialisation complete a chaque appel
# de append_batch_to_history) : le cout d'un ajout croissait avec la taille
# deja accumulee de l'historique (croissance quadratique du temps cumule sur
# de nombreux lots). Le format JSONL permet un vrai append (une ecriture en
# fin de fichier, sans jamais relire ni reserialiser les lots deja presents)
# - le seul cas ou l'integralite du fichier est encore reecrite est la purge
# volontaire (purge_history), l'annulation (qui modifie un statut existant)
# et le compactage occasionnel declenche par MAX_HISTORY_BATCHES.
#
# Migration transparente : un ancien history.json (tableau JSON classique)
# encore present est automatiquement converti en history.jsonl des la
# premiere lecture ou le premier ajout, sans aucune perte de donnees ; le
# fichier d'origine n'est supprime qu'une fois la conversion confirmee
# ecrite sur disque.

# Cache en memoire du nombre de lots deja presents dans le fichier JSONL
# courant, pour eviter de relire tout le fichier a chaque appel de
# append_batch_to_history juste pour connaitre sa longueur. Invalide
# automatiquement des que le chemin change (ex : tests qui redirigent
# HISTORY_FILE) puisqu'il est compare au chemin courant a chaque acces.
_history_count_cache = {"path": None, "count": None}


def _history_jsonl_path() -> Path:
    return HISTORY_FILE.with_suffix(".jsonl")


def _write_history_jsonl(history: list) -> None:
    """Reecrit l'integralite du fichier JSONL a partir d'une liste (purge,
    annulation, compactage, migration) et met a jour le cache de comptage
    en consequence."""
    jsonl_path = _history_jsonl_path()
    lines = [json.dumps(entry, ensure_ascii=False) for entry in history]
    content = "\n".join(lines)
    if content:
        content += "\n"
    _atomic_write_text(jsonl_path, content)
    global _history_count_cache
    _history_count_cache = {"path": jsonl_path, "count": len(history)}


def _migrate_legacy_history() -> list:
    """Convertit l'ancien history.json (tableau JSON) en history.jsonl s'il
    existe encore et qu'aucun history.jsonl n'a deja ete cree. Renvoie la
    liste des lots (vide si aucun historique legacy ou s'il est corrompu -
    meme comportement de quarantaine que load_history() employait avant
    l'introduction du format JSONL)."""
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("history.json ne contient pas une liste JSON valide")
    except (json.JSONDecodeError, OSError, ValueError):
        _ensure_logging_configured()
        logger.warning("history.json invalide ou corrompu, historique reinitialise.", exc_info=True)
        _quarantine_corrupted_file(HISTORY_FILE)
        return []

    _write_history_jsonl(data)
    try:
        HISTORY_FILE.unlink()
    except OSError:
        # La migration a deja ete ecrite avec succes (source de verite
        # desormais le fichier .jsonl) : l'ancien fichier restant sur le
        # disque n'est pas grave, juste redondant.
        pass
    return data


def load_history() -> list:
    jsonl_path = _history_jsonl_path()
    if not jsonl_path.exists():
        return _migrate_legacy_history()

    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError:
        _ensure_logging_configured()
        logger.warning("Impossible de lire history.jsonl.", exc_info=True)
        return []

    history = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            history.append(json.loads(line))
        except json.JSONDecodeError:
            # Une ligne isolee corrompue (ex : coupure de courant en plein
            # ajout) ne fait perdre que ce seul lot, jamais tout l'historique
            # - contrairement a l'ancien format ou toute corruption partielle
            # invalidait le fichier entier.
            _ensure_logging_configured()
            logger.warning("Ligne %d de history.jsonl corrompue, ignoree.", lineno)
    return history


def save_history(history: list) -> None:
    _write_history_jsonl(history)


# Plafond de securite : au-dela, on tronque automatiquement aux plus recents
# a chaque ajout, pour qu'un usage quotidien sur plusieurs annees ne fasse
# jamais grossir l'historique indefiniment. Le compactage (reecriture
# complete au format JSONL) ne se declenche desormais que lorsque ce plafond
# est effectivement depasse, pas a chaque ajout.
MAX_HISTORY_BATCHES = 2000


# Frequence de checkpoint incremental du lot en cours pendant execute() (voir
# audit A1 : sans checkpoint, l'historique n'etait ecrit qu'une seule fois, a
# la toute fin de la boucle de deplacement). Les HISTORY_CHECKPOINT_INTERVAL
# premiers fichiers de chaque lot reel sont toujours checkpointes un par un
# (fenetre de risque la plus probable en pratique), le reste du lot ne l'est
# plus qu'une fois tous les HISTORY_CHECKPOINT_INTERVAL fichiers - voir
# execute() pour le detail et la justification (checkpointer litteralement
# chaque fichier d'un tres gros lot degraderait sensiblement la performance,
# chaque checkpoint reecrivant l'integralite du lot deja traite).
HISTORY_CHECKPOINT_INTERVAL = 50


def _last_line_offset(jsonl_path: Path) -> int:
    """Renvoie l'offset en octets ou commence la derniere ligne non vide du
    fichier JSONL. Lit le fichier entier - n'est utilise qu'apres un
    compactage (rare, uniquement au franchissement de MAX_HISTORY_BATCHES),
    ou une reecriture complete a deja eu lieu et ou l'on a simplement besoin
    de relocaliser le lot le plus recent dans le fichier fraichement
    reecrit."""
    content = jsonl_path.read_bytes()
    trimmed = content.rstrip(b"\r\n")
    idx = trimmed.rfind(b"\n")
    return idx + 1 if idx != -1 else 0


def append_batch_to_history(batch: dict) -> tuple:
    """Ajoute `batch` en fin de fichier JSONL - un vrai append, sans jamais
    relire ni reecrire les lots deja presents (cout independant de la
    taille totale de l'historique deja accumule, cf. commit 7834cb5).

    Renvoie (offset, longueur) en octets du debut de la ligne fraichement
    ecrite, pour permettre a l'appelant de la mettre a jour EN PLACE par la
    suite via update_batch_checkpoint - utilise par execute() pour
    checkpointer la progression d'un lot reel au fur et a mesure des
    deplacements plutot que d'attendre la toute fin de la boucle (voir
    audit A1 : sans cela, un arret brutal du processus en plein milieu d'un
    gros lot laissait les fichiers deja deplaces sans aucune trace
    recuperable dans l'historique)."""
    jsonl_path = _history_jsonl_path()
    if not jsonl_path.exists():
        # Declenche la migration une seule fois (no-op si aucun history.json
        # legacy n'existe) avant le tout premier ajout au format JSONL.
        _migrate_legacy_history()

    global _history_count_cache
    if _history_count_cache["path"] == jsonl_path and _history_count_cache["count"] is not None:
        count = _history_count_cache["count"]
    else:
        count = len(load_history())

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    # Ecriture en mode binaire (et non texte) : necessaire pour que l'offset
    # renvoye soit un veritable decalage en octets, exploitable ensuite par
    # update_batch_checkpoint via seek()/truncate() - la traduction de fin de
    # ligne du mode texte (\n -> \r\n sous Windows) rendrait ce calcul
    # ambigu. Les lignes JSONL restent lisibles indifferemment (load_history
    # se base sur str.splitlines(), qui reconnait aussi bien \n que \r\n).
    # offset lu via tell() juste apres l'ouverture en mode "ab" (position
    # deja au bout du fichier a l'ouverture en mode append, cree vide si
    # absent) plutot que via un stat() separe avant l'ouverture, pour
    # reduire au minimum la fenetre entre la lecture de la position et
    # l'ecriture elle-meme.
    line = json.dumps(batch, ensure_ascii=False).encode("utf-8") + b"\n"
    with open(jsonl_path, "ab") as f:
        offset = f.tell()
        f.write(line)
    length = len(line)
    count += 1
    _history_count_cache = {"path": jsonl_path, "count": count}

    if count > MAX_HISTORY_BATCHES:
        # Compactage occasionnel (rare : seulement au franchissement du
        # plafond) plutot qu'a chaque ajout comme avant.
        history = load_history()[-MAX_HISTORY_BATCHES:]
        _write_history_jsonl(history)
        # Le compactage reecrit l'integralite du fichier : l'offset calcule
        # ci-dessus n'est plus valide. Le lot qu'on vient d'ajouter est
        # forcement le plus recent, donc toujours en derniere ligne -
        # relocalise sa position exacte (cout O(taille totale de
        # l'historique), mais uniquement dans ce cas rare, deja accepte
        # comme cout intrinseque du compactage lui-meme).
        offset = _last_line_offset(jsonl_path)
        length = jsonl_path.stat().st_size - offset

    return offset, length


def update_batch_checkpoint(offset: int, length: int, batch: dict, file_obj=None) -> int:
    """Reecrit EN PLACE, a l'octet pres, la ligne JSONL precedemment ecrite a
    `offset` (de longueur `length` octets, cf. append_batch_to_history) avec
    l'etat a jour de `batch` - sans jamais relire ni reecrire les lots
    precedents du fichier. La nouvelle ligne peut avoir une longueur
    differente de l'ancienne (les entrees changent de statut au fil des
    deplacements) : le contenu est tronque/etendu en consequence a partir de
    `offset`, jamais au-dela.

    Cout O(taille d'un seul lot), independant de la taille totale de
    l'historique - c'est ce qui permet a execute() de checkpointer la
    progression d'un gros lot reel sans reintroduire le cout quadratique que
    le format JSONL append-only (commit 7834cb5) avait justement elimine.

    `file_obj`, si fourni, doit etre un fichier deja ouvert en mode "r+b" sur
    le fichier JSONL courant : il est alors reutilise directement plutot que
    d'ouvrir puis refermer un nouveau handle a chaque appel (execute() garde
    ainsi un seul handle ouvert pour toute la duree d'un gros lot, cout non
    negligeable de l'ouverture/fermeture repetee mesure empiriquement sur de
    tres gros lots). Dans les deux cas, un flush() explicite est effectue
    apres l'ecriture pour que les octets quittent bien le tampon Python et
    atteignent l'OS avant de rendre la main - sans quoi reutiliser un handle
    ouvert sur toute la duree du lot pourrait laisser un checkpoint recent
    uniquement dans un tampon en memoire de processus, jamais persiste si le
    processus est tue brutalement (exactement le risque que ce mecanisme de
    checkpoint est cense eliminer, cf. audit A1) : fermer un fichier force
    deja ce flush, mais un handle garde ouvert entre deux checkpoints ne le
    ferait pas sans cet appel explicite.

    A n'utiliser que sur la toute derniere ligne du fichier (aucune ligne
    ajoutee apres celle-ci entretemps) : c'est garanti tant qu'un seul lot
    reel est execute a la fois (execute() est le seul appelant). Renvoie la
    nouvelle longueur en octets, a repasser au prochain appel."""
    line = json.dumps(batch, ensure_ascii=False).encode("utf-8") + b"\n"
    if file_obj is not None:
        file_obj.seek(offset)
        file_obj.write(line)
        file_obj.truncate(offset + len(line))
        file_obj.flush()
    else:
        jsonl_path = _history_jsonl_path()
        with open(jsonl_path, "r+b") as f:
            f.seek(offset)
            f.write(line)
            f.truncate(offset + len(line))
            f.flush()
    return len(line)


def purge_history(keep_last: Optional[int] = None) -> int:
    """Ne conserve que les `keep_last` lots les plus recents (tous les
    supprimer si `keep_last` est None ou 0). Renvoie le nombre de lots
    retires. Les fichiers deja deplaces sur le disque ne sont jamais
    affectes - seul le journal d'historique est purge (l'annulation des
    lots retires ne sera simplement plus possible depuis l'interface)."""
    history = load_history()
    original_count = len(history)
    if not keep_last:
        history = []
    else:
        history = history[-keep_last:]
    save_history(history)
    return original_count - len(history)


# ---------------------------------------------------------------------------
# Modele d'un deplacement planifie
# ---------------------------------------------------------------------------

@dataclass
class PlannedMove:
    source: Path
    destination: Path
    category: str
    reason: str = ""


@dataclass
class OrganizeResult:
    moves: list = field(default_factory=list)        # list[PlannedMove] effectues ou simules
    excluded: list = field(default_factory=list)      # list[Path] ignores (exclusion ou non categorise)
    errors: list = field(default_factory=list)        # list[tuple[Path, str]]
    skipped_dirs: list = field(default_factory=list)  # list[Path] sous-dossiers non parcourus


# ---------------------------------------------------------------------------
# Logique principale
# ---------------------------------------------------------------------------

class DownloadOrganizer:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_config()
        self.categories = get_effective_categories(self.config)

    # -- helpers -----------------------------------------------------------

    def _is_excluded(self, path: Path) -> bool:
        excl = self.config["exclusions"]
        suffix = path.suffix.lower()
        normalized_extensions = {
            e.lower() if e.startswith(".") else f".{e.lower()}"
            for e in excl.get("extensions", []) if isinstance(e, str) and e.strip()
        }
        if suffix in normalized_extensions:
            return True
        normalized_filenames = {
            f.lower() for f in excl.get("filenames", []) if isinstance(f, str)
        }
        if path.name.lower() in normalized_filenames:
            return True
        all_patterns = list(excl.get("patterns", [])) + ALWAYS_EXCLUDED_PATTERNS
        for pattern in all_patterns:
            if isinstance(pattern, str) and fnmatch.fnmatch(path.name.lower(), pattern.lower()):
                return True
        return False

    def _category_by_name_pattern(self, path: Path) -> Optional[str]:
        """Categorie personnalisee dont un motif de nom (glob) correspond a
        ce fichier, ou None. Separee de _category_for pour que plan() sache
        qu'une correspondance ici est un choix EXPLICITE de l'utilisateur
        sur la DESTINATION du fichier (ex: "facture*.pdf" route vers
        "Factures" plutot que "PDF", alors que le contenu reel est bien un
        PDF). Ce choix ne dispense jamais de la verification d'incoherence
        extension/signature : voir plan(), qui l'applique AVANT tout
        routage par motif de nom, precisement pour qu'un motif large
        (ex: "*.pdf") ne puisse jamais faire passer un executable renomme
        pour un PDF sans declencher la quarantaine."""
        filename_lower = path.name.lower()
        for category, info in self.categories.items():
            for pattern in info.get("name_patterns") or ():
                if fnmatch.fnmatch(filename_lower, pattern.lower()):
                    return category
        return None

    def _category_by_extension(self, path: Path) -> Optional[str]:
        """Categorie dont une extension correspond a `path`, en ignorant
        totalement les motifs de nom - separee de `_category_for` pour que
        plan() puisse comparer CETTE categorie (une vraie correspondance
        d'extension) a celle detectee par signature, sans jamais melanger
        un routage par motif de nom avec une correspondance d'extension
        (voir plan() : merger les deux ici aurait fait declencher une fausse
        incoherence extension/signature des qu'un motif de nom route un
        fichier vers une categorie dont le nom differe du nom detecte par
        signature, meme quand le contenu est parfaitement coherent -
        bug trouve a l'audit)."""
        suffix = path.suffix.lower()
        for category, info in self.categories.items():
            if suffix in info["extensions"]:
                return category
        return None

    def _category_for(self, path: Path) -> Optional[str]:
        # Les regles par motif de nom (categories personnalisees
        # uniquement - les categories integrees n'en definissent jamais)
        # sont testees EN PREMIER, avant toute correspondance par extension
        # y compris celle d'une categorie integree : un motif de nom est
        # une regle plus specifique/intentionnelle que la simple extension,
        # c'est precisement ce qui permet par exemple a "facture*.pdf" de
        # router vers une categorie "Factures" plutot que vers le PDF
        # generique. Aucune categorie existante ne definissant ce champ
        # (absent de DEFAULT_CATEGORIES), ce nouveau test est un pur ajout :
        # il ne change jamais le comportement d'une configuration qui
        # n'utilise pas cette fonctionnalite.
        pattern_category = self._category_by_name_pattern(path)
        if pattern_category is not None:
            return pattern_category
        return self._category_by_extension(path)

    def _is_old(self, path: Path) -> bool:
        threshold_days = self.config.get("old_file_threshold_days", OLD_FILE_THRESHOLD_DAYS)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        age_days = (time.time() - mtime) / 86400
        return age_days >= threshold_days

    def _target_dir_for_category(self, category: str) -> Path:
        base = Path(self.config["base_target_dir"])
        if category in (OLD_FILES_TARGET, DUPLICATES_TARGET):
            return base / category
        return base / self.categories[category]["target"]

    def scan_stale_review_folders(self, threshold_days: Optional[int] = None) -> list[dict]:
        """Signale les fichiers qui dorment depuis longtemps dans "A verifier"
        ou "Doublons" sans jamais rien supprimer ni deplacer : ces deux
        dossiers accumulent des fichiers que l'utilisateur doit trier
        manuellement, et rien ne l'avertit jamais s'ils s'y entassent.
        Retourne une liste (une entree par dossier non vide parmi les deux),
        chacune avec le nombre de fichiers et la taille totale en octets."""
        if threshold_days is None:
            threshold_days = self.config.get("old_file_threshold_days", OLD_FILE_THRESHOLD_DAYS)
        cutoff = time.time() - threshold_days * 86400
        results = []
        for category in (OLD_FILES_TARGET, DUPLICATES_TARGET):
            folder = self._target_dir_for_category(category)
            if not folder.is_dir():
                continue
            count = 0
            total_bytes = 0
            for entry in folder.rglob("*"):
                if not entry.is_file():
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                if stat.st_mtime <= cutoff:
                    count += 1
                    total_bytes += stat.st_size
            if count:
                results.append({"folder": category, "count": count, "total_bytes": total_bytes})
        return results

    @staticmethod
    def _file_hash(path: Path, cache: Optional[dict] = None) -> Optional[str]:
        """Hash SHA-256 du contenu d'un fichier, en flux (pas de lecture
        integrale en memoire). Renvoie None si le fichier est illisible
        (verrouille, supprime entre-temps, permissions) : dans ce cas la
        detection de doublon est simplement desactivee pour ce fichier,
        il suit le classement normal par extension/anciennete."""
        if cache is not None and path in cache:
            return cache[path]
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                    digest.update(chunk)
            result = digest.hexdigest()
        except OSError:
            result = None
        if cache is not None:
            cache[path] = result
        return result

    @staticmethod
    def _partial_hash(path: Path, size: int, cache: Optional[dict] = None) -> Optional[str]:
        """Hash rapide (debut + fin du fichier) utilise UNIQUEMENT comme
        pre-filtre avant hash complet (voir PARTIAL_HASH_SAMPLE_SIZE) : deux
        fichiers de meme taille dont ce hash differe sont garantis differents
        (pas de faux negatif possible - il suffit d'un octet different en
        debut ou fin pour que ce hash differe), mais deux fichiers dont ce
        hash concorde ne sont PAS garantis identiques (le milieu du fichier
        n'est pas lu) : ce cas doit toujours etre confirme par un hash SHA-256
        complet, jamais traite comme une preuve suffisante a lui seul."""
        if cache is not None and path in cache:
            return cache[path]
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                digest.update(f.read(PARTIAL_HASH_SAMPLE_SIZE))
                if size > PARTIAL_HASH_SAMPLE_SIZE * 2:
                    f.seek(-PARTIAL_HASH_SAMPLE_SIZE, os.SEEK_END)
                    digest.update(f.read(PARTIAL_HASH_SAMPLE_SIZE))
            result = digest.hexdigest()
        except OSError:
            result = None
        if cache is not None:
            cache[path] = result
        return result

    def _unique_destination(self, target_dir: Path, filename: str, reserved: set) -> Path:
        """Trouve une destination libre, en tenant aussi compte des destinations
        deja attribuees a d'autres fichiers du meme lot (pas encore deplaces sur
        le disque) via `reserved`."""
        stem, suffix = Path(filename).stem, Path(filename).suffix
        candidate = target_dir / filename
        counter = 1
        while candidate.exists() or candidate in reserved:
            candidate = target_dir / f"{stem} ({counter}){suffix}"
            counter += 1
        reserved.add(candidate)
        return candidate

    @staticmethod
    def _is_sensitive_system_path(path: Path) -> Optional[str]:
        """Detecte si un chemin correspond a la racine d'un lecteur ou a un
        dossier systeme sensible (Windows, Program Files...). Protege contre
        une configuration corrompue ou modifiee manuellement (config.json)
        qui pointerait le dossier Telechargements ou la destination racine
        vers un tel emplacement : sans ce garde-fou, l'outil deplacerait
        silencieusement des fichiers systeme reconnus par leur extension
        (.exe, .zip, etc.). Renvoie une description du dossier sensible
        detecte, ou None si le chemin est sans danger apparent."""
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path

        if resolved.parent == resolved:
            return f"la racine du lecteur ({resolved})"

        for env_var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(env_var)
            if not value:
                continue
            try:
                sensitive_dir = Path(value).resolve()
            except OSError:
                continue
            if resolved == sensitive_dir or sensitive_dir in resolved.parents:
                return f"un dossier systeme ({sensitive_dir})"

        return None

    @staticmethod
    def _is_within_base(candidate: Path, base: Path) -> bool:
        """Verifie que `candidate` reste bien a l'interieur de `base` une
        fois les deux chemins resolus. Complement necessaire a
        _is_safe_relative_target : celle-ci rejette les cas evidents
        (target absolu, '..') a la saisie, mais pathlib.Path.__truediv__
        peut aussi produire un chemin hors de `base` a partir d'un target
        "enracine" sans lecteur (ex: '/Windows' ou '\\Windows'), qu'
        is_absolute() ne detecte pas sous Windows - d'ou cette verification
        sur le resultat final plutot que sur la chaine d'entree seule."""
        try:
            candidate_resolved = candidate.resolve()
            base_resolved = base.resolve()
        except OSError:
            candidate_resolved, base_resolved = candidate, base
        return candidate_resolved == base_resolved or base_resolved in candidate_resolved.parents

    # -- planification -------------------------------------------------

    def plan(self) -> OrganizeResult:
        downloads_dir_str = self.config["downloads_dir"].strip() if self.config["downloads_dir"] else ""
        base_target_str = self.config["base_target_dir"].strip() if self.config["base_target_dir"] else ""
        result = OrganizeResult()

        if not downloads_dir_str:
            result.errors.append((Path("."), "Le dossier Telechargements n'est pas renseigne."))
            return result

        downloads_dir = Path(downloads_dir_str)
        if not downloads_dir.exists():
            result.errors.append((downloads_dir, "Le dossier Telechargements est introuvable."))
            return result

        sensitive = self._is_sensitive_system_path(downloads_dir)
        if sensitive:
            result.errors.append((
                downloads_dir,
                f"Ce dossier correspond a {sensitive} : par securite, l'outil "
                "refuse de trier un dossier systeme. Verifiez la configuration.",
            ))
            return result

        if not base_target_str:
            result.errors.append((Path("."), "Le dossier de destination racine n'est pas renseigne."))
            return result

        base_target_dir = Path(base_target_str)
        if not base_target_dir.exists():
            result.errors.append((base_target_dir, "Le dossier de destination racine est introuvable."))
            return result

        sensitive = self._is_sensitive_system_path(base_target_dir)
        if sensitive:
            result.errors.append((
                base_target_dir,
                f"Ce dossier correspond a {sensitive} : par securite, l'outil "
                "refuse d'y deplacer des fichiers. Verifiez la configuration.",
            ))
            return result

        # Meme garde-fou pour le dossier cible de chaque categorie (integree
        # ou personnalisee) : une categorie personnalisee mal configuree (ou
        # importee depuis un config.json externe) pourrait sinon rediriger
        # des fichiers hors de base_target_dir, voire vers un dossier
        # systeme, sans jamais passer par les deux verifications ci-dessus.
        for category_name in self.categories:
            candidate_dir = self._target_dir_for_category(category_name)
            if not self._is_within_base(candidate_dir, base_target_dir):
                result.errors.append((
                    candidate_dir,
                    f"Le dossier cible de la categorie '{category_name}' sort du dossier de "
                    "destination racine : par securite, l'outil refuse cette configuration.",
                ))
                return result
            category_sensitive = self._is_sensitive_system_path(candidate_dir)
            if category_sensitive:
                result.errors.append((
                    candidate_dir,
                    f"Le dossier cible de la categorie '{category_name}' correspond a "
                    f"{category_sensitive} : par securite, l'outil refuse d'y deplacer des fichiers.",
                ))
                return result

        reserved_destinations = set()
        hash_cache: dict = {}
        partial_hash_cache: dict = {}
        signature_cache: dict = {}
        candidates = []  # list[(Path entry, str category, str reason)]

        try:
            entries = sorted(downloads_dir.iterdir())
        except OSError as exc:
            result.errors.append((downloads_dir, f"Impossible de lire le contenu du dossier : {exc}"))
            return result

        for entry in entries:
            if entry.is_dir():
                result.skipped_dirs.append(entry)
                continue
            if self._is_excluded(entry):
                result.excluded.append(entry)
                continue

            pattern_category = self._category_by_name_pattern(entry)
            # _category_by_extension (PAS _category_for, qui integre aussi
            # les motifs de nom) : la verification d'incoherence
            # extension/signature ci-dessous doit comparer une vraie
            # correspondance d'extension a la signature detectee, jamais
            # une categorie choisie par motif de nom dont le nom differe
            # legitimement du nom detecte par signature.
            ext_category = self._category_by_extension(entry)
            suffix = entry.suffix.lower()
            # Lecture de 16 octets seulement : cout negligeable, calcule pour
            # chaque fichier (utilise ensuite pour detecter les incoherences
            # extension/contenu, ou classer les extensions inconnues).
            signature_category = _detect_file_signature(entry, signature_cache)
            override = EXPECTED_SIGNATURE_OVERRIDES.get((suffix, signature_category))
            if override is not None:
                signature_category = override

            category = ext_category
            reason = ""

            # Un document bureautique/conteneur ZIP legitime (docx/xlsx/epub/
            # jar/...) est TOUJOURS detecte avec la signature "Archives" (un
            # tel fichier est litteralement un ZIP en interne) : ce n'est
            # jamais une incoherence a signaler, quelle que soit la categorie
            # qui revendique l'extension - integree (cas ci-dessous, aucune
            # categorie integree ne revendique ces extensions) OU
            # PERSONNALISEE (bug trouve a l'audit : une categorie
            # personnalisee sur ".docx"/".xlsx"/etc. faisait passer
            # systematiquement 100% de ces fichiers legitimes pour une
            # incoherence extension/signature, faute pour la branche
            # ci-dessous de considerer ce garde-fou). Calcule une seule fois
            # et utilise comme garde commune aux deux branches qui suivent.
            zip_like_container_ok = (
                suffix in ZIP_LIKE_NON_ARCHIVE_EXTENSIONS and signature_category == "Archives"
            )

            # Coherence extension/signature verifiee AVANT tout routage par
            # motif de nom : un motif de nom (ex: "*.pdf" pour tout envoyer
            # vers "Factures") choisit OU va le fichier, mais ne doit jamais
            # decider QUE le contenu reel n'a pas besoin d'etre verifie -
            # sinon un executable renomme en ".pdf" et capture par un motif
            # aussi large que "*.pdf" (une config tout a fait plausible,
            # pas une attaque deliberee) serait route en toute confiance
            # vers "Factures" sans jamais declencher la quarantaine
            # censee proteger precisement contre ce cas (bug trouve a
            # l'audit). Un motif de nom NARROW cible comme "facture*.pdf"
            # applique sur un vrai PDF ne declenche jamais cette branche,
            # puisqu'il n'y a alors aucune incoherence reelle.
            if (
                ext_category is not None
                and signature_category is not None
                and signature_category != ext_category
                and not zip_like_container_ok
            ):
                category = OLD_FILES_TARGET
                reason = (
                    f"extension '{suffix}' incoherente avec le contenu reel du fichier "
                    f"(signature detectee : {signature_category}) - verification manuelle recommandee"
                )
            elif (
                ext_category is None
                and suffix in ZIP_LIKE_NON_ARCHIVE_EXTENSIONS
                and signature_category is not None
                and not zip_like_container_ok
            ):
                # Le garde-fou ZIP_LIKE_NON_ARCHIVE_EXTENSIONS ne doit couvrir
                # QUE le cas legitime ou un document bureautique (docx/xlsx/
                # apk/...) est reconnu comme conteneur ZIP generique
                # (signature "Archives") : ce n'est alors pas une incoherence
                # a signaler. Mais si la signature detectee est tout autre
                # chose (ex: "Installateurs" pour un en-tete MZ), le fichier
                # est en realite un executable renomme avec cette extension
                # et NE DOIT PAS echapper a la detection - meme si aucune
                # categorie d'extension connue (ext_category) n'existe pour
                # ce suffixe. Meme traitement que l'incoherence
                # extension/signature ci-dessus : signalement pour
                # verification manuelle plutot que classement silencieux.
                category = OLD_FILES_TARGET
                reason = (
                    f"extension '{suffix}' incoherente avec le contenu reel du fichier "
                    f"(signature detectee : {signature_category}) - verification manuelle recommandee"
                )
            elif pattern_category is not None:
                # Route explicitement par motif de nom : l'utilisateur a
                # deliberement demande ce routage pour ce nom de fichier -
                # mais seulement une fois la coherence extension/signature
                # ci-dessus confirmee (aucune incoherence detectee).
                category = pattern_category
                reason = "motif de nom personnalise"
            elif ext_category is not None:
                reason = f"extension {suffix}"
            elif signature_category is not None and suffix not in ZIP_LIKE_NON_ARCHIVE_EXTENSIONS:
                # Extension absente/inconnue, mais le contenu est reconnu par sa signature.
                category = signature_category
                reason = f"signature de fichier reconnue comme {signature_category} (extension '{suffix or '(aucune)'}' non reconnue)"
            elif self._is_old(entry):
                category = OLD_FILES_TARGET
                reason = f"fichier de plus de {self.config.get('old_file_threshold_days', OLD_FILE_THRESHOLD_DAYS)} jours, type non reconnu"
            else:
                result.excluded.append(entry)
                continue

            candidates.append((entry, category, reason))

        duplicate_of = self._find_duplicates_within_batch(candidates, hash_cache, partial_hash_cache)

        for entry, category, reason in candidates:
            if entry in duplicate_of:
                keeper = duplicate_of[entry]
                dup_dir = self._target_dir_for_category(DUPLICATES_TARGET)
                destination = self._unique_destination(dup_dir, entry.name, reserved_destinations)
                dup_reason = f"doublon de contenu identique a {keeper.name} (meme lot)"
                result.moves.append(PlannedMove(source=entry, destination=destination, category=DUPLICATES_TARGET, reason=dup_reason))
                continue

            target_dir = self._target_dir_for_category(category)
            natural_destination = target_dir / entry.name
            if natural_destination.exists() and natural_destination not in reserved_destinations:
                existing_hash = self._file_hash(natural_destination, hash_cache)
                source_hash = self._file_hash(entry, hash_cache)
                if existing_hash is not None and existing_hash == source_hash:
                    dup_dir = self._target_dir_for_category(DUPLICATES_TARGET)
                    destination = self._unique_destination(dup_dir, entry.name, reserved_destinations)
                    try:
                        existing_label = natural_destination.relative_to(base_target_dir)
                    except ValueError:
                        existing_label = natural_destination
                    dup_reason = f"doublon de contenu identique a {existing_label} (deja range)"
                    result.moves.append(PlannedMove(source=entry, destination=destination, category=DUPLICATES_TARGET, reason=dup_reason))
                    continue

            destination = self._unique_destination(target_dir, entry.name, reserved_destinations)
            result.moves.append(PlannedMove(source=entry, destination=destination, category=category, reason=reason))

        return result

    @staticmethod
    def _find_duplicates_within_batch(candidates: list, hash_cache: dict, partial_hash_cache: Optional[dict] = None) -> dict:
        """Detecte les doublons de contenu parmi les fichiers du lot courant.
        Ne hash que les fichiers dont un autre fichier partage exactement la
        meme taille (evite de hasher inutilement tout le dossier). Renvoie
        {Path doublon -> Path fichier conserve (le "garde")}.

        A l'interieur de chaque groupe de meme taille, un second pre-filtre
        rapide (_partial_hash, debut+fin du fichier) reduit encore l'ensemble
        des fichiers necessitant un hash SHA-256 complet : sur un dossier
        contenant beaucoup de fichiers de tailles proches mais de contenu
        different (captures d'ecran, factures PDF...), le hash complet
        (_file_hash, qui lit tout le fichier) n'est alors calcule que pour
        les fichiers dont le debut+fin coincide deja - jamais pour decider
        seul d'un doublon, uniquement pour restreindre les candidats. Deux
        fichiers de contenu strictement identique ont necessairement le meme
        hash partiel (leur debut et leur fin sont identiques par definition),
        ils ne peuvent donc jamais se retrouver separes dans deux sous-
        groupes differents : ce pre-filtre ne change donc jamais le resultat
        final de detection, seulement le nombre de lectures completes de
        fichiers necessaires pour l'obtenir."""
        if partial_hash_cache is None:
            partial_hash_cache = {}
        by_size: dict = defaultdict(list)
        for entry, _category, _reason in candidates:
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            by_size[size].append(entry)

        duplicate_of = {}
        for size, group in by_size.items():
            if len(group) < 2:
                continue

            by_partial: dict = defaultdict(list)
            for entry in group:
                partial = DownloadOrganizer._partial_hash(entry, size, partial_hash_cache)
                by_partial[partial].append(entry)

            for partial_group in by_partial.values():
                if len(partial_group) < 2:
                    # Aucun autre fichier de meme taille ne partage ce debut+fin :
                    # impossible que ce soit un doublon, aucun hash complet requis.
                    continue
                seen_hashes: dict = {}
                for entry in sorted(partial_group, key=lambda p: p.name.lower()):
                    digest = DownloadOrganizer._file_hash(entry, hash_cache)
                    if digest is None:
                        continue
                    if digest in seen_hashes:
                        duplicate_of[entry] = seen_hashes[digest]
                    else:
                        seen_hashes[digest] = entry
        return duplicate_of

    # -- execution --------------------------------------------------------

    @staticmethod
    def _checkpoint_history(checkpoint: tuple, batch: dict, file_obj=None, raise_on_error: bool = False) -> tuple:
        """Reecrit en place le checkpoint d'historique du lot en cours
        (cf. append_batch_to_history/update_batch_checkpoint) avec l'etat
        actuel de `batch`. En cours de boucle (raise_on_error=False, valeur
        par defaut), un echec (disque plein, permissions, antivirus qui
        verrouille temporairement le fichier) ne fait qu'un avertissement
        journalise, sans interrompre execute() ni faire perdre les
        deplacements deja effectues en memoire : l'etat sur disque reste
        simplement celui du dernier checkpoint reussi, et un nouvel essai a
        lieu automatiquement au prochain deplacement. Pour l'enregistrement
        final (raise_on_error=True), l'appelant a besoin de savoir si
        l'ecriture a effectivement reussi (pour positionner
        batch["history_error"] le cas echeant), donc l'exception est
        laissee remonter. `file_obj` est transmis tel quel a
        update_batch_checkpoint (handle persistant reutilise pour tout le
        lot, cf. execute())."""
        offset, length = checkpoint
        try:
            new_length = update_batch_checkpoint(offset, length, batch, file_obj=file_obj)
            return (offset, new_length)
        except OSError as exc:
            if raise_on_error:
                raise
            _ensure_logging_configured()
            logger.error("Echec de la mise a jour incrementale de l'historique : %s", exc)
            return (offset, length)

    def execute(self, result: OrganizeResult, simulate: bool = True) -> dict:
        """Execute (ou simule) les deplacements planifies. Retourne le lot pour l'historique."""
        batch = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "simulated": simulate,
            "moves": [],
        }

        # Correctif audit A1 (Critique) : pour un lot reel, l'historique du
        # lot est checkpointe sur disque au fur et a mesure des
        # deplacements, pas seulement a la toute fin de la boucle. Sans
        # cela, un arret brutal du processus (fin de tache forcee, coupure
        # de courant, plantage systeme) en plein milieu d'un gros lot (ex :
        # 500 fichiers) laissait les fichiers deja physiquement deplaces
        # sans AUCUNE trace dans l'historique - ni possibilite
        # d'annulation, ni visibilite dans l'onglet Historique.
        #
        # Le premier checkpoint (lot encore vide) est ecrit ICI, AVANT le
        # premier deplacement reel, via un vrai append JSONL
        # (append_batch_to_history - cout independant de la taille de
        # l'historique deja accumule, commit 7834cb5). Chaque deplacement
        # traite declenche ensuite une reecriture EN PLACE de cette meme
        # ligne (_checkpoint_history / update_batch_checkpoint, par
        # offset/longueur) : jamais une relecture ni reecriture des lots
        # precedents, pour ne pas reintroduire le cout quadratique que
        # l'optimisation JSONL append-only avait justement elimine.
        checkpoint = None
        if not simulate:
            try:
                checkpoint = append_batch_to_history(batch)
            except OSError as exc:
                _ensure_logging_configured()
                logger.error("Echec de l'enregistrement initial de l'historique : %s", exc)

        # Un seul handle est ouvert (en "r+b") pour toute la duree du lot et
        # reutilise a chaque checkpoint, plutot que d'ouvrir puis refermer un
        # nouveau handle a chaque fichier : l'ouverture/fermeture repetee
        # d'un fichier a elle seule un cout non negligeable sur de tres gros
        # lots (mesure empiriquement). update_batch_checkpoint flush()
        # explicitement ce handle apres chaque ecriture, pour que les octets
        # quittent bien le tampon Python et atteignent l'OS avant de rendre
        # la main - la protection contre un arret brutal ne depend donc pas
        # de la fermeture du fichier.
        checkpoint_file = None
        if not simulate and checkpoint is not None:
            try:
                checkpoint_file = open(_history_jsonl_path(), "r+b")
            except OSError as exc:
                _ensure_logging_configured()
                logger.error("Echec de l'ouverture de l'historique pour checkpoint incremental : %s", exc)

        try:
            # Chaque checkpoint reecrit l'integralite du lot serialise en JSON
            # (toutes les entrees deja traitees, pas seulement la derniere) :
            # checkpointer apres CHAQUE fichier reintroduirait un cout
            # quadratique sur un gros lot (mesure empiriquement : +336% de
            # temps d'execution sur 3000 fichiers), exactement le defaut que
            # l'ecriture JSONL append-only avait elimine pour l'historique
            # dans son ensemble. HISTORY_CHECKPOINT_INTERVAL borne ce cout :
            # au-dela des tout premiers fichiers (toujours checkpointes un
            # par un - c'est la fenetre de risque la plus probable en
            # pratique, cf. B2 : un utilisateur impatient qui force la
            # fermeture tot faute d'indicateur de progression), le reste du
            # lot n'est checkpointe que tous les HISTORY_CHECKPOINT_INTERVAL
            # fichiers. La fenetre de perte potentielle en cas d'arret brutal
            # reste ainsi bornee a une poignee de fichiers au pire, au lieu
            # du lot entier (des centaines de fichiers) comme avant ce
            # correctif.
            moves_processed = 0
            for move in result.moves:
                entry = {
                    "source": str(move.source),
                    "destination": str(move.destination),
                    "category": move.category,
                    "reason": move.reason,
                    "status": "planifie" if simulate else "erreur",
                }
                if not simulate:
                    try:
                        move.destination.parent.mkdir(parents=True, exist_ok=True)
                        # Reverification juste avant le deplacement reel : entre plan()
                        # et execute() (fenetre potentiellement large en Mode Veille, qui
                        # attend une stabilisation avant de proposer le lot), un fichier a
                        # pu apparaitre a la destination planifiee. Sans ce garde-fou,
                        # shutil.move l'ecraserait silencieusement (sur un deplacement
                        # inter-disque, il retombe sur copy+unlink des que os.rename
                        # echoue). Meme mecanisme de reverification que
                        # _attempt_restore_entry pour l'annulation.
                        if move.destination.exists():
                            entry["status"] = "erreur"
                            entry["error"] = (
                                "un fichier existe deja a cet emplacement (apparu apres la "
                                "planification) : deplacement annule pour eviter un ecrasement"
                            )
                            _ensure_logging_configured()
                            logger.error(
                                "Deplacement annule (destination apparue entre plan() et execute()) : %s -> %s",
                                move.source, move.destination,
                            )
                        else:
                            shutil.move(str(move.source), str(move.destination))
                            entry["status"] = "deplace"
                    except OSError as exc:
                        entry["status"] = "erreur"
                        entry["error"] = str(exc)
                        _ensure_logging_configured()
                        logger.error("Echec du deplacement %s -> %s : %s", move.source, move.destination, exc)
                        # Sur un deplacement inter-disque, shutil.move copie puis supprime la
                        # source ; si la copie echoue en cours de route, un fichier partiel/
                        # tronque peut rester a destination alors que la source existe encore.
                        # On le nettoie pour ne pas laisser un fichier corrompu se faire passer
                        # pour le fichier complet.
                        if move.source.exists() and move.destination.exists():
                            try:
                                move.destination.unlink()
                                entry["error"] += " (fichier partiel nettoye a la destination)"
                            except OSError:
                                pass
                batch["moves"].append(entry)
                moves_processed += 1
                # Checkpoint incremental : voir le commentaire au-dessus de la
                # boucle pour la justification de HISTORY_CHECKPOINT_INTERVAL.
                if not simulate and checkpoint is not None:
                    if (
                        moves_processed <= HISTORY_CHECKPOINT_INTERVAL
                        or moves_processed % HISTORY_CHECKPOINT_INTERVAL == 0
                    ):
                        checkpoint = self._checkpoint_history(checkpoint, batch, file_obj=checkpoint_file)

            if not simulate:
                # Enregistrement final. Deux cas :
                # - le tout premier checkpoint avait echoue (checkpoint est
                #   encore None ici) : on n'a jamais rien ecrit pour ce lot,
                #   donc on retente un unique append complet (aucun risque de
                #   doublon, rien n'existe encore sur disque pour ce lot).
                # - un checkpoint a bien ete etabli : un dernier
                #   update_batch_checkpoint garantit que l'etat final complet
                #   est bien celui persiste, meme si une mise a jour
                #   intermediaire avait echoue temporairement en cours de route.
                # Comme avant : les fichiers ont deja ete physiquement deplaces
                # a ce stade, un echec d'ecriture de l'historique (disque plein,
                # permissions, antivirus) ne doit ni faire planter l'appli ni
                # faire perdre le resultat du lot en memoire - on le signale
                # dans le batch plutot que de laisser l'exception se propager.
                try:
                    if checkpoint is not None:
                        self._checkpoint_history(checkpoint, batch, file_obj=checkpoint_file, raise_on_error=True)
                    else:
                        append_batch_to_history(batch)
                except OSError as exc:
                    _ensure_logging_configured()
                    logger.error("Echec de l'enregistrement de l'historique : %s", exc)
                    batch["history_error"] = str(exc)
        finally:
            # Ferme systematiquement le handle de checkpoint, y compris si
            # une exception non geree interrompt la boucle (arret brutal
            # simule dans les tests, ou toute erreur inattendue) - c'est ce
            # qui garantit que le dernier checkpoint ecrit reste lisible/non
            # verrouille pour une lecture ulterieure (ex: undo) plutot que de
            # laisser un handle ouvert indefiniment.
            if checkpoint_file is not None:
                try:
                    checkpoint_file.close()
                except OSError:
                    pass

        return batch

    # -- annulation ---------------------------------------------------------

    @staticmethod
    def _attempt_restore_entry(entry: dict) -> tuple:
        """Tente de remettre un fichier deplace a son emplacement d'origine.
        Modifie entry["status"] en place. Renvoie (deplace_avec_succes,
        conflit_potentiellement_resoluble_plus_tard)."""
        dest_str, src_str = entry.get("destination"), entry.get("source")
        if not dest_str or not src_str:
            # history.json modifie/corrompu manuellement : entree incomplete,
            # on ne peut pas savoir ou remettre ce fichier.
            entry["status"] = "annulation_impossible"
            return False, False
        src = Path(dest_str)
        dst = Path(src_str)
        try:
            if not src.exists():
                entry["status"] = "annulation_impossible"
                return False, False
            elif dst.exists():
                return False, True  # peut etre retente plus tard si le conflit se resout
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                entry["status"] = "annule"
                return True, False
        except OSError:
            return False, True

    def _restore_batch_entries(self, history: list, batch: dict, source_paths=None) -> dict:
        """Base commune des trois variantes d'annulation (dernier lot,
        selection de fichiers du dernier lot, lot quelconque de
        l'historique) : restaure chaque entree "deplace" de `batch` (ou
        seulement celles dont la source figure dans `source_paths`), puis
        sauvegarde l'historique complet - `batch` doit etre une reference
        vers un element de `history`, jamais une copie, sans quoi les
        statuts mis a jour ne seraient pas persistes."""
        if source_paths is not None:
            source_paths = {str(p) for p in source_paths}
        undone = []
        errors = []
        for entry in batch["moves"]:
            if entry.get("status") != "deplace":
                continue
            if source_paths is not None and entry.get("source") not in source_paths:
                continue
            dest_str, src_str = entry.get("destination"), entry.get("source")
            success, _retryable = self._attempt_restore_entry(entry)
            if success:
                undone.append(entry)
            else:
                reason = (
                    "entree d'historique incomplete, annulation impossible pour ce fichier"
                    if not dest_str or not src_str else
                    "fichier introuvable (deja deplace ou renomme)" if entry["status"] == "annulation_impossible" else
                    "un fichier existe deja a cet emplacement, annulation ignoree pour ce fichier"
                )
                errors.append((dest_str or str(entry), reason))

        # Le lot n'est marque definitivement annule que s'il ne reste PLUS
        # AUCUNE entree a l'etat "deplace" : une annulation selective doit
        # laisser les fichiers non selectionnes annulables plus tard, et un
        # conflit potentiellement resoluble (fichier reapparu a l'emplacement
        # d'origine) doit laisser le lot disponible pour un nouvel essai.
        if not any(e.get("status") == "deplace" for e in batch["moves"]):
            batch["undone"] = True
        save_history(history)

        return {"undone": undone, "errors": errors}

    def undo_last_batch(self) -> dict:
        history = load_history()
        real_batches = [b for b in history if not b.get("simulated") and not b.get("undone")]
        if not real_batches:
            return {"undone": [], "message": "Aucun lot a annuler."}
        return self._restore_batch_entries(history, real_batches[-1])

    def undo_selected_files(self, source_paths) -> dict:
        """Comme undo_last_batch, mais ne restaure que les fichiers dont le
        chemin source d'origine (avant deplacement) figure dans
        `source_paths` - les autres entrees du dernier lot restent
        deplacees, disponibles pour une annulation ulterieure (totale ou
        partielle d'un autre sous-ensemble)."""
        history = load_history()
        real_batches = [b for b in history if not b.get("simulated") and not b.get("undone")]
        if not real_batches:
            return {"undone": [], "errors": [], "message": "Aucun lot a annuler."}
        return self._restore_batch_entries(history, real_batches[-1], source_paths=source_paths)

    def undo_batch_at(self, history_index: int) -> dict:
        """Annule un lot QUELCONQUE de l'historique, designe par son index
        absolu dans history.json (pas par sa position dans un affichage,
        qui peut etre inverse ou tronque). Un lot ancien peut entrer en
        conflit avec des deplacements posterieurs (fichier recree a la
        source depuis) : _attempt_restore_entry refuse alors d'ecraser et
        l'erreur est simplement remontee, le lot restant retentable."""
        history = load_history()
        if not 0 <= history_index < len(history):
            return {"undone": [], "errors": [], "message": "Lot introuvable dans l'historique."}
        batch = history[history_index]
        if batch.get("simulated"):
            return {"undone": [], "errors": [],
                    "message": "Ce lot est une simulation : aucun fichier n'a ete deplace, rien a annuler."}
        if batch.get("undone"):
            return {"undone": [], "errors": [], "message": "Ce lot a deja ete entierement annule."}
        return self._restore_batch_entries(history, batch)


# ---------------------------------------------------------------------------
# Rapport de session (transparence : explique chaque decision prise)
# ---------------------------------------------------------------------------

# Libelles lisibles des statuts d'entree de lot, partages entre le rapport
# HTML (export_html_report) et le detail de lot affiche dans la GUI
# (gui.py, _show_batch_detail) - pour ne jamais avoir deux formulations
# differentes du meme statut selon l'endroit ou on le consulte.
BATCH_STATUS_LABELS = {
    "deplace": "Deplace",
    "erreur": "Erreur",
    "annule": "Annule (undo)",
    "annulation_impossible": "Annulation impossible",
    "planifie": "Simule",
}


def export_html_report(batch: dict, path: Path, base_dir: Optional[Path] = None) -> None:
    """Ecrit un rapport HTML autonome listant chaque fichier traite lors d'un
    lot reel, avec sa categorie, sa destination et la raison du classement,
    pour que l'utilisateur puisse verifier/auditer ce que l'outil a fait.

    Si `base_dir` est fourni, les chemins de destination sont affiches
    relatifs a ce dossier plutot qu'en chemin absolu complet : ce rapport est
    concu pour etre partage/exporte, et un chemin absolu Windows revele le
    nom du compte utilisateur (ex: C:\\Users\\<nom>\\...) sans que ce soit
    utile a la comprehension du rapport."""
    rows = []
    for m in batch["moves"]:
        status_label = BATCH_STATUS_LABELS.get(m.get("status"), m.get("status", ""))
        destination_display = m.get("destination", "")
        if base_dir is not None and destination_display:
            try:
                destination_display = str(Path(destination_display).relative_to(base_dir))
            except ValueError:
                pass  # hors de base_dir (rare) : on garde le chemin complet
        rows.append(
            "<tr>"
            # .get() plutot que m["source"] (bug trouve a l'audit) : un lot
            # malforme/edite a la main ne doit jamais faire planter tout
            # l'export d'un rapport pour une seule entree incomplete.
            f"<td>{_html_escape(Path(m.get('source', '')).name)}</td>"
            f"<td>{_html_escape(m.get('category', ''))}</td>"
            f"<td>{_html_escape(destination_display)}</td>"
            f"<td>{_html_escape(m.get('reason', ''))}</td>"
            f"<td>{_html_escape(status_label)}</td>"
            f"<td>{_html_escape(m.get('error', ''))}</td>"
            "</tr>"
        )

    html = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>Rapport de rangement - {_html_escape(batch.get('timestamp', ''))}</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; font-size: 0.9rem; }}
th {{ background: #f0f0f0; }}
</style></head>
<body>
<h1>Rapport de rangement</h1>
<p>Date : {_html_escape(batch.get('timestamp', ''))}</p>
<p>Nombre de fichiers traites : {len(batch['moves'])}</p>
<table>
<thead><tr><th>Fichier</th><th>Categorie</th><th>Destination</th><th>Raison</th><th>Statut</th><th>Detail erreur</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</body></html>
"""
    path.write_text(html, encoding="utf-8")


def _html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ensure_console_encoding_tolerant() -> None:
    """Empeche le CLI de planter avec un UnicodeEncodeError brut quand un nom
    de fichier contient un caractere (emoji, etc.) hors du code page de la
    console (bug trouve a l'audit, A12) : sur une invite Windows standard non
    explicitement basculee en UTF-8, sys.stdout/sys.stderr utilisent par
    defaut le code page heritee de la console (souvent cp1252/cp850), qui ne
    peut pas encoder la plupart des emojis. print() levait alors une
    exception non interceptee AVANT meme d'avoir affiche le moindre plan.
    reconfigure(errors="backslashreplace") (disponible depuis Python 3.7)
    remplace un caractere non encodable par sa representation "\\uXXXX"
    lisible plutot que de faire planter tout le programme - le nom de
    fichier reste identifiable meme si l'emoji ne s'affiche pas litteralement."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Flux redirige/mocke sans .reconfigure() (tests, certains
            # environnements non interactifs) : rien a faire, ce n'est de
            # toute facon pas le cas vise par ce correctif (une vraie console
            # Windows).
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            # Flux deja ferme ou ne supportant pas reconfigure() pour cette
            # option : ne doit jamais empecher le CLI de demarrer.
            pass


def _print_plan(result: OrganizeResult) -> None:
    if result.errors:
        for path, err in result.errors:
            print(f"[ERREUR] {path}: {err}")
        return

    if not result.moves:
        print("Aucun fichier a ranger.")
    for move in result.moves:
        print(f"  {move.source.name}  ->  {move.destination}  ({move.category}: {move.reason})")

    if result.excluded:
        print(f"\n{len(result.excluded)} fichier(s) ignore(s) (exclusion ou non categorise, non ancien).")
    if result.skipped_dirs:
        print(f"{len(result.skipped_dirs)} sous-dossier(s) non parcouru(s) (non geres par cet outil).")


def main(argv: Optional[list] = None) -> int:
    import argparse

    # Voir _ensure_console_encoding_tolerant : doit s'executer avant le
    # moindre print() (y compris ceux du mode --undo et du mode --gui), sinon
    # un nom de fichier contenant un caractere hors code page console fait
    # planter le CLI avec un traceback brut (bug trouve a l'audit, A12).
    _ensure_console_encoding_tolerant()

    parser = argparse.ArgumentParser(description="Nettoyeur intelligent du dossier Telechargements")
    parser.add_argument("--run", action="store_true", help="Execute reellement les deplacements (sinon simulation)")
    parser.add_argument("--undo", action="store_true", help="Annule le dernier lot de deplacements reels")
    parser.add_argument("--gui", action="store_true", help="Lance l'interface graphique")
    parser.add_argument("--downloads-dir", type=str, help="Chemin du dossier Telechargements a analyser")
    args = parser.parse_args(argv)

    if args.gui:
        # gui.py fait "from organizer import ...", ce qui re-executerait ce
        # fichier une seconde fois sous un nom de module distinct si on ne
        # reutilise pas l'entree __main__ deja chargee.
        sys.modules.setdefault("organizer", sys.modules["__main__"])
        from gui import run_gui
        run_gui()
        return 0

    config = load_config()
    if args.downloads_dir:
        # Remplacement ponctuel pour cette execution uniquement : non persiste,
        # pour eviter qu'un test rapide en ligne de commande n'ecrase
        # durablement le dossier configure dans la GUI.
        config["downloads_dir"] = args.downloads_dir

    organizer = DownloadOrganizer(config)

    if args.undo:
        out = organizer.undo_last_batch()
        for entry in out["undone"]:
            print(f"[ANNULE] {entry['destination']} -> {entry['source']}")
        for path, err in out.get("errors", []):
            print(f"[ERREUR] {path}: {err}")
        if not out["undone"] and not out.get("errors"):
            print(out.get("message", ""))
        return 0

    result = organizer.plan()
    simulate = not args.run
    print("MODE SIMULATION" if simulate else "MODE REEL (deplacements effectifs)")
    _print_plan(result)

    if result.errors:
        return 1

    if result.moves:
        organizer.execute(result, simulate=simulate)
        if not simulate:
            print(f"\n{len(result.moves)} fichier(s) deplace(s). Utilisez --undo pour annuler.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

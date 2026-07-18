"""
Nettoyeur intelligent du dossier Telechargements
=================================================

Analyse le dossier Telechargements et range automatiquement les fichiers par
categorie (PDF, Images, Archives, Installateurs, fichiers anciens a verifier).

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
from pathlib import Path
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

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("history.json ne contient pas une liste JSON valide")
            return data
        except (json.JSONDecodeError, OSError, ValueError):
            _ensure_logging_configured()
            logger.warning("history.json invalide ou corrompu, historique reinitialise.", exc_info=True)
            _quarantine_corrupted_file(HISTORY_FILE)
            return []
    return []


def save_history(history: list) -> None:
    _atomic_write_json(HISTORY_FILE, history)


def append_batch_to_history(batch: dict) -> None:
    history = load_history()
    history.append(batch)
    save_history(history)


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

    def _category_for(self, path: Path) -> Optional[str]:
        suffix = path.suffix.lower()
        for category, info in DEFAULT_CATEGORIES.items():
            if suffix in info["extensions"]:
                return category
        return None

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
        return base / DEFAULT_CATEGORIES[category]["target"]

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

        reserved_destinations = set()
        hash_cache: dict = {}
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

            ext_category = self._category_for(entry)
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

            if ext_category is not None and signature_category is not None and signature_category != ext_category:
                # L'extension et le contenu reel du fichier ne correspondent pas
                # (ex: un .exe renomme en .pdf) : on ne fait pas confiance a
                # l'extension aveuglement, on isole le fichier pour verification.
                category = OLD_FILES_TARGET
                reason = (
                    f"extension '{suffix}' incoherente avec le contenu reel du fichier "
                    f"(signature detectee : {signature_category}) - verification manuelle recommandee"
                )
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

        duplicate_of = self._find_duplicates_within_batch(candidates, hash_cache)

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
    def _find_duplicates_within_batch(candidates: list, hash_cache: dict) -> dict:
        """Detecte les doublons de contenu parmi les fichiers du lot courant.
        Ne hash que les fichiers dont un autre fichier partage exactement la
        meme taille (evite de hasher inutilement tout le dossier). Renvoie
        {Path doublon -> Path fichier conserve (le "garde")}."""
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
            seen_hashes: dict = {}
            for entry in sorted(group, key=lambda p: p.name.lower()):
                digest = DownloadOrganizer._file_hash(entry, hash_cache)
                if digest is None:
                    continue
                if digest in seen_hashes:
                    duplicate_of[entry] = seen_hashes[digest]
                else:
                    seen_hashes[digest] = entry
        return duplicate_of

    # -- execution --------------------------------------------------------

    def execute(self, result: OrganizeResult, simulate: bool = True) -> dict:
        """Execute (ou simule) les deplacements planifies. Retourne le lot pour l'historique."""
        batch = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "simulated": simulate,
            "moves": [],
        }

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

        if not simulate:
            # Les fichiers ont deja ete physiquement deplaces a ce stade : un
            # echec d'ecriture de l'historique (disque plein, permissions,
            # antivirus) ne doit ni faire planter l'appli ni faire perdre le
            # resultat du lot. On le signale dans le batch plutot que de
            # laisser l'exception se propager.
            try:
                append_batch_to_history(batch)
            except OSError as exc:
                _ensure_logging_configured()
                logger.error("Echec de l'enregistrement de l'historique : %s", exc)
                batch["history_error"] = str(exc)

        return batch

    # -- annulation ---------------------------------------------------------

    def undo_last_batch(self) -> dict:
        history = load_history()
        real_batches = [b for b in history if not b.get("simulated") and not b.get("undone")]
        if not real_batches:
            return {"undone": [], "message": "Aucun lot a annuler."}

        last_batch = real_batches[-1]
        undone = []
        errors = []
        pending = False  # au moins une entree "deplace" restante non resolue
        for entry in last_batch["moves"]:
            if entry.get("status") != "deplace":
                continue
            dest_str, src_str = entry.get("destination"), entry.get("source")
            if not dest_str or not src_str:
                # history.json modifie/corrompu manuellement : entree
                # incomplete, on ne peut pas savoir ou remettre ce fichier.
                errors.append((str(entry), "entree d'historique incomplete, annulation impossible pour ce fichier"))
                entry["status"] = "annulation_impossible"
                continue
            src = Path(dest_str)
            dst = Path(src_str)
            try:
                if not src.exists():
                    errors.append((str(src), "fichier introuvable (deja deplace ou renomme)"))
                    entry["status"] = "annulation_impossible"
                elif dst.exists():
                    errors.append((str(dst), "un fichier existe deja a cet emplacement, annulation ignoree pour ce fichier"))
                    pending = True  # peut etre retente plus tard si le conflit se resout
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    entry["status"] = "annule"
                    undone.append(entry)
            except OSError as exc:
                errors.append((str(src), str(exc)))
                pending = True

        # Le lot n'est marque comme definitivement annule que si toutes ses
        # entrees ont ete traitees avec succes ou jugees irrecuperables ; s'il
        # reste des conflits potentiellement resolubles (fichier en place a la
        # destination d'origine), on le laisse disponible pour un nouvel essai.
        if not pending:
            last_batch["undone"] = True
        save_history(history)

        return {"undone": undone, "errors": errors}


# ---------------------------------------------------------------------------
# Rapport de session (transparence : explique chaque decision prise)
# ---------------------------------------------------------------------------

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
        status_label = {
            "deplace": "Deplace",
            "erreur": "Erreur",
            "annule": "Annule (undo)",
            "annulation_impossible": "Annulation impossible",
            "planifie": "Simule",
        }.get(m.get("status"), m.get("status", ""))
        destination_display = m.get("destination", "")
        if base_dir is not None and destination_display:
            try:
                destination_display = str(Path(destination_display).relative_to(base_dir))
            except ValueError:
                pass  # hors de base_dir (rare) : on garde le chemin complet
        rows.append(
            "<tr>"
            f"<td>{_html_escape(Path(m['source']).name)}</td>"
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

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
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import fnmatch
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

# Motifs toujours exclus, independamment de la configuration utilisateur.
# Non modifiables depuis la GUI : evite qu'un champ "Motifs" vide par megarde
# ne desactive la protection de fichiers systeme/temporaires.
ALWAYS_EXCLUDED_PATTERNS = ["*.crdownload", "*.part", "*.tmp", "desktop.ini", "*.download"]

DEFAULT_CONFIG = {
    "downloads_dir": str(Path.home() / "Downloads"),
    "base_target_dir": str(Path.home()),
    "old_file_threshold_days": OLD_FILE_THRESHOLD_DAYS,
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


def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json ne contient pas un objet JSON valide")

            merged = copy.deepcopy(DEFAULT_CONFIG)
            for key in ("downloads_dir", "base_target_dir", "old_file_threshold_days"):
                if key in data and isinstance(data[key], type(DEFAULT_CONFIG[key])):
                    merged[key] = data[key]
            if _is_valid_exclusions(data.get("exclusions")):
                merged["exclusions"].update(data["exclusions"])
            return merged
        except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError):
            _ensure_logging_configured()
            logger.warning("config.json invalide ou corrompu, restauration des valeurs par defaut.", exc_info=True)
            _quarantine_corrupted_file(CONFIG_FILE)
    save_config(DEFAULT_CONFIG)
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    _atomic_write_json(CONFIG_FILE, config)


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
        if category == "A verifier":
            return base / OLD_FILES_TARGET
        return base / DEFAULT_CATEGORIES[category]["target"]

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

        if not base_target_str:
            result.errors.append((Path("."), "Le dossier de destination racine n'est pas renseigne."))
            return result

        base_target_dir = Path(base_target_str)
        if not base_target_dir.exists():
            result.errors.append((base_target_dir, "Le dossier de destination racine est introuvable."))
            return result

        reserved_destinations = set()

        for entry in sorted(downloads_dir.iterdir()):
            if entry.is_dir():
                result.skipped_dirs.append(entry)
                continue
            if self._is_excluded(entry):
                result.excluded.append(entry)
                continue

            category = self._category_for(entry)
            reason = ""
            if category is None:
                if self._is_old(entry):
                    category = "A verifier"
                    reason = f"fichier de plus de {self.config.get('old_file_threshold_days', OLD_FILE_THRESHOLD_DAYS)} jours, type non reconnu"
                else:
                    result.excluded.append(entry)
                    continue
            else:
                reason = f"extension {entry.suffix.lower()}"

            target_dir = self._target_dir_for_category(category)
            destination = self._unique_destination(target_dir, entry.name, reserved_destinations)
            result.moves.append(PlannedMove(source=entry, destination=destination, category=category, reason=reason))

        return result

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
            append_batch_to_history(batch)

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
            src = Path(entry["destination"])
            dst = Path(entry["source"])
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

def export_html_report(batch: dict, path: Path) -> None:
    """Ecrit un rapport HTML autonome listant chaque fichier traite lors d'un
    lot reel, avec sa categorie, sa destination et la raison du classement,
    pour que l'utilisateur puisse verifier/auditer ce que l'outil a fait."""
    rows = []
    for m in batch["moves"]:
        status_label = {
            "deplace": "Deplace",
            "erreur": "Erreur",
            "annule": "Annule (undo)",
            "annulation_impossible": "Annulation impossible",
            "planifie": "Simule",
        }.get(m.get("status"), m.get("status", ""))
        rows.append(
            "<tr>"
            f"<td>{_html_escape(Path(m['source']).name)}</td>"
            f"<td>{_html_escape(m.get('category', ''))}</td>"
            f"<td>{_html_escape(m.get('destination', ''))}</td>"
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

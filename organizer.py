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

import json
import shutil
import sys
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

DEFAULT_CONFIG = {
    "downloads_dir": str(Path.home() / "Downloads"),
    "base_target_dir": str(Path.home()),
    "old_file_threshold_days": OLD_FILE_THRESHOLD_DAYS,
    "exclusions": {
        "extensions": [],       # ex: [".tmp", ".crdownload"]
        "filenames": [],        # ex: ["ne_pas_toucher.pdf"]
        "patterns": ["*.crdownload", "*.part", "*.tmp", "desktop.ini", "*.download"],
    },
}


def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
            merged.update({k: v for k, v in data.items() if k != "exclusions"})
            if "exclusions" in data:
                merged["exclusions"].update(data["exclusions"])
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Historique
# ---------------------------------------------------------------------------

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_history(history: list) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


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
    moves: list = field(default_factory=list)      # list[PlannedMove] effectues ou simules
    excluded: list = field(default_factory=list)    # list[Path] ignores
    errors: list = field(default_factory=list)      # list[tuple[Path, str]]


# ---------------------------------------------------------------------------
# Logique principale
# ---------------------------------------------------------------------------

class DownloadOrganizer:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_config()

    # -- helpers -----------------------------------------------------------

    def _is_excluded(self, path: Path) -> bool:
        excl = self.config["exclusions"]
        if path.suffix.lower() in [e.lower() for e in excl.get("extensions", [])]:
            return True
        if path.name in excl.get("filenames", []):
            return True
        for pattern in excl.get("patterns", []):
            if fnmatch.fnmatch(path.name.lower(), pattern.lower()):
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

    def _unique_destination(self, target_dir: Path, filename: str) -> Path:
        dest = target_dir / filename
        if not dest.exists():
            return dest
        stem, suffix = Path(filename).stem, Path(filename).suffix
        counter = 1
        while True:
            candidate = target_dir / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    # -- planification -------------------------------------------------

    def plan(self) -> OrganizeResult:
        downloads_dir = Path(self.config["downloads_dir"])
        result = OrganizeResult()

        if not downloads_dir.exists():
            result.errors.append((downloads_dir, "Le dossier Telechargements est introuvable."))
            return result

        for entry in sorted(downloads_dir.iterdir()):
            if entry.is_dir():
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
            destination = self._unique_destination(target_dir, entry.name)
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
            batch["moves"].append(entry)

        if not simulate:
            append_batch_to_history(batch)

        return batch

    # -- annulation ---------------------------------------------------------

    def undo_last_batch(self) -> dict:
        history = load_history()
        real_batches = [b for b in history if not b.get("simulated")]
        if not real_batches:
            return {"undone": [], "message": "Aucun lot a annuler."}

        last_batch = real_batches[-1]
        undone = []
        errors = []
        for entry in last_batch["moves"]:
            if entry.get("status") != "deplace":
                continue
            src = Path(entry["destination"])
            dst = Path(entry["source"])
            try:
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    undone.append(entry)
                else:
                    errors.append((str(src), "fichier introuvable (deja deplace ou renomme)"))
            except OSError as exc:
                errors.append((str(src), str(exc)))

        # marque le lot comme annule pour ne pas le reproposer
        for b in history:
            if b is last_batch:
                b["undone"] = True
        save_history(history)

        return {"undone": undone, "errors": errors}


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


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Nettoyeur intelligent du dossier Telechargements")
    parser.add_argument("--run", action="store_true", help="Execute reellement les deplacements (sinon simulation)")
    parser.add_argument("--undo", action="store_true", help="Annule le dernier lot de deplacements reels")
    parser.add_argument("--gui", action="store_true", help="Lance l'interface graphique")
    parser.add_argument("--downloads-dir", type=str, help="Chemin du dossier Telechargements a analyser")
    args = parser.parse_args(argv)

    if args.gui:
        from gui import run_gui
        run_gui()
        return 0

    config = load_config()
    if args.downloads_dir:
        config["downloads_dir"] = args.downloads_dir
        save_config(config)

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

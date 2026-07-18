"""Tests de regression pour organizer.py.

Ces tests verifient en priorite la garantie centrale de l'outil : aucun
deplacement/annulation ne doit jamais ecraser un fichier existant. Ils
couvrent aussi les bugs trouves lors des audits de code successifs, pour
empecher toute regression future.
"""

import copy
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import organizer as org


class OrganizerTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)

        # Isole completement les tests du vrai ~/.download_organizer de l'utilisateur
        # (y compris le fichier de log, sans quoi le handler deja ouvert sur le vrai
        # chemin resterait actif malgre le changement d'APP_DIR).
        self._orig_app_dir = org.APP_DIR
        self._orig_history_file = org.HISTORY_FILE
        self._orig_config_file = org.CONFIG_FILE
        self._orig_log_file = org.LOG_FILE
        org.APP_DIR = self.tmp / "appdata"
        org.HISTORY_FILE = org.APP_DIR / "history.json"
        org.CONFIG_FILE = org.APP_DIR / "config.json"
        org.LOG_FILE = org.APP_DIR / "app.log"
        for handler in list(org.logger.handlers):
            handler.close()
            org.logger.removeHandler(handler)

        self.downloads = self.tmp / "Downloads"
        self.downloads.mkdir()
        self.target = self.tmp / "Target"
        self.target.mkdir()

        self.config = copy.deepcopy(org.DEFAULT_CONFIG)
        self.config["downloads_dir"] = str(self.downloads)
        self.config["base_target_dir"] = str(self.target)

    def _cleanup(self):
        for handler in list(org.logger.handlers):
            handler.close()
            org.logger.removeHandler(handler)
        org.APP_DIR = self._orig_app_dir
        org.HISTORY_FILE = self._orig_history_file
        org.CONFIG_FILE = self._orig_config_file
        org.LOG_FILE = self._orig_log_file

    def _write(self, relative_path: str, content: str = "x"):
        path = self.downloads / relative_path
        path.write_text(content)
        return path

    def _organizer(self, **overrides):
        cfg = copy.deepcopy(self.config)
        cfg.update(overrides)
        return org.DownloadOrganizer(cfg)

    # -- garantie centrale : jamais d'ecrasement -------------------------

    def test_execute_does_not_overwrite_on_move(self):
        self._write("a.pdf", "original")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)
        self.assertTrue((self.target / "Documents" / "PDF" / "a.pdf").exists())

    def test_undo_does_not_overwrite_new_file(self):
        self._write("a.pdf", "original")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)

        # Un nouveau fichier de meme nom arrive apres le rangement.
        (self.downloads / "a.pdf").write_text("NOUVEAU FICHIER")

        out = o.undo_last_batch()
        self.assertEqual(out["undone"], [])
        self.assertTrue(out["errors"])
        self.assertEqual((self.downloads / "a.pdf").read_text(), "NOUVEAU FICHIER")

    def test_same_batch_collision_does_not_overwrite(self):
        # Deux fichiers sources distincts qui, sans reservation en memoire,
        # se verraient attribuer la meme destination "dedupliquee".
        (self.target / "Documents" / "PDF").mkdir(parents=True)
        (self.target / "Documents" / "PDF" / "x.pdf").write_text("deja present")
        self._write("x.pdf", "un")
        self._write("y.pdf", "deux")

        o = self._organizer()
        result = o.plan()
        destinations = [str(m.destination) for m in result.moves]
        self.assertEqual(len(destinations), len(set(destinations)))

        o.execute(result, simulate=False)
        # Le fichier deja present ne doit pas avoir ete efface.
        self.assertEqual((self.target / "Documents" / "PDF" / "x.pdf").read_text(), "deja present")

    # -- annulation --------------------------------------------------------

    def test_undo_twice_reports_nothing_left(self):
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)

        first = o.undo_last_batch()
        self.assertEqual(len(first["undone"]), 1)

        second = o.undo_last_batch()
        self.assertEqual(second["undone"], [])
        self.assertIn("message", second)

    def test_undo_full_round_trip(self):
        self._write("a.pdf", "contenu")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)
        self.assertFalse((self.downloads / "a.pdf").exists())

        o.undo_last_batch()
        self.assertTrue((self.downloads / "a.pdf").exists())
        self.assertEqual((self.downloads / "a.pdf").read_text(), "contenu")

    # -- validations de dossiers -------------------------------------------

    def test_empty_downloads_dir_is_rejected(self):
        o = self._organizer(downloads_dir="")
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)

    def test_missing_downloads_dir_is_rejected(self):
        o = self._organizer(downloads_dir=str(self.tmp / "n_existe_pas"))
        result = o.plan()
        self.assertTrue(result.errors)

    def test_empty_base_target_dir_is_rejected(self):
        o = self._organizer(base_target_dir="")
        result = o.plan()
        self.assertTrue(result.errors)

    def test_missing_base_target_dir_is_rejected(self):
        o = self._organizer(base_target_dir=str(self.tmp / "n_existe_pas"))
        result = o.plan()
        self.assertTrue(result.errors)

    def test_subdirectories_are_reported_not_silently_dropped(self):
        (self.downloads / "un_sous_dossier").mkdir()
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.skipped_dirs), 1)

    # -- detection de doublons par hash --------------------------------------

    def test_duplicate_within_same_batch_is_routed_to_doublons(self):
        self._write("rapport.pdf", "contenu identique")
        self._write("rapport (1).pdf", "contenu identique")
        o = self._organizer()
        result = o.plan()

        categories = sorted(m.category for m in result.moves)
        self.assertEqual(categories, ["Doublons", "PDF"])
        # Le premier (ordre alphabetique) est conserve dans sa categorie normale.
        keeper = next(m for m in result.moves if m.category == "PDF")
        self.assertEqual(keeper.source.name, "rapport (1).pdf")
        duplicate = next(m for m in result.moves if m.category == "Doublons")
        self.assertEqual(duplicate.source.name, "rapport.pdf")
        self.assertIn("rapport (1).pdf", duplicate.reason)

    def test_same_name_different_content_is_not_a_duplicate(self):
        self._write("a.pdf", "contenu A")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)

        # Nouveau telechargement, meme nom, contenu different.
        self._write("a.pdf", "contenu B, totalement different")
        result2 = o.plan()
        self.assertEqual(len(result2.moves), 1)
        self.assertEqual(result2.moves[0].category, "PDF")
        self.assertEqual(result2.moves[0].destination.name, "a (1).pdf")

    def test_duplicate_of_already_organized_file_is_routed_to_doublons(self):
        self._write("a.pdf", "contenu identique")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)
        self.assertTrue((self.target / "Documents" / "PDF" / "a.pdf").exists())

        # Meme fichier retelecharge sous le meme nom.
        self._write("a.pdf", "contenu identique")
        result2 = o.plan()
        self.assertEqual(len(result2.moves), 1)
        self.assertEqual(result2.moves[0].category, "Doublons")
        self.assertIn("deja range", result2.moves[0].reason)

        o.execute(result2, simulate=False)
        # Le fichier deja range n'a pas ete touche/ecrase.
        self.assertEqual((self.target / "Documents" / "PDF" / "a.pdf").read_text(), "contenu identique")
        self.assertTrue((self.target / "Doublons" / "a.pdf").exists())

    def test_three_way_duplicates_keep_only_one(self):
        self._write("x.pdf", "meme contenu")
        self._write("y.pdf", "meme contenu")
        self._write("z.pdf", "meme contenu")
        o = self._organizer()
        result = o.plan()
        keepers = [m for m in result.moves if m.category == "PDF"]
        duplicates = [m for m in result.moves if m.category == "Doublons"]
        self.assertEqual(len(keepers), 1)
        self.assertEqual(len(duplicates), 2)

    # -- exclusions ----------------------------------------------------------

    def test_extension_exclusion_without_leading_dot_still_works(self):
        self._write("note.tmp")
        o = self._organizer(exclusions={"extensions": ["tmp"], "filenames": [], "patterns": []})
        result = o.plan()
        self.assertEqual(len(result.moves), 0)
        self.assertEqual(len(result.excluded), 1)

    def test_builtin_protections_always_active_even_with_empty_patterns(self):
        old_time = time.time() - 200 * 86400
        f = self._write("desktop.ini")
        os.utime(f, (old_time, old_time))
        o = self._organizer(exclusions={"extensions": [], "filenames": [], "patterns": []})
        result = o.plan()
        self.assertEqual(len(result.moves), 0)

    def test_filename_exclusion_is_case_insensitive(self):
        self._write("Rapport.pdf")
        o = self._organizer(exclusions={"extensions": [], "filenames": ["rapport.pdf"], "patterns": []})
        result = o.plan()
        self.assertEqual(len(result.moves), 0)

    # -- robustesse config/historique ---------------------------------------

    def test_corrupted_config_falls_back_to_defaults(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.CONFIG_FILE.write_text("{ceci n'est pas du JSON valide", encoding="utf-8")
        cfg = org.load_config()
        self.assertEqual(cfg["downloads_dir"], org.DEFAULT_CONFIG["downloads_dir"])

    def test_config_with_invalid_exclusions_type_does_not_crash(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.CONFIG_FILE.write_text(json.dumps({"exclusions": None}), encoding="utf-8")
        cfg = org.load_config()  # ne doit pas lever d'exception
        self.assertIsInstance(cfg["exclusions"], dict)

    def test_corrupted_history_returns_empty_list_not_crash(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.HISTORY_FILE.write_text("pas du JSON", encoding="utf-8")
        self.assertEqual(org.load_history(), [])

    def test_config_persists_across_reload(self):
        cfg = copy.deepcopy(org.DEFAULT_CONFIG)
        cfg["downloads_dir"] = str(self.downloads)
        org.save_config(cfg)
        reloaded = org.load_config()
        self.assertEqual(reloaded["downloads_dir"], str(self.downloads))

    # -- rapport HTML ---------------------------------------------------------

    def test_export_html_report_creates_readable_file(self):
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        batch = o.execute(result, simulate=False)

        report_path = self.tmp / "rapport.html"
        org.export_html_report(batch, report_path)
        content = report_path.read_text(encoding="utf-8")
        self.assertIn("a.pdf", content)
        self.assertIn("<table>", content)


if __name__ == "__main__":
    unittest.main()

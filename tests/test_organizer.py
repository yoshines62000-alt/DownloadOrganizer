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
        moved = self.target / "Documents" / "PDF" / "a.pdf"
        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_text(), "original")

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

    def test_undo_selected_files_restores_only_the_chosen_file(self):
        self._write("a.pdf", "contenu a")
        self._write("b.pdf", "contenu b")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)
        self.assertFalse((self.downloads / "a.pdf").exists())
        self.assertFalse((self.downloads / "b.pdf").exists())

        o.undo_selected_files([str(self.downloads / "a.pdf")])
        self.assertTrue((self.downloads / "a.pdf").exists())
        self.assertFalse((self.downloads / "b.pdf").exists())  # b.pdf reste deplace

    def test_undo_selected_files_leaves_batch_undoable_for_remaining_files(self):
        # Apres une annulation partielle, le fichier restant doit encore
        # pouvoir etre annule ensuite (le lot ne doit pas etre marque
        # "termine" tant qu'il reste des entrees "deplace").
        self._write("a.pdf", "a")
        self._write("b.pdf", "b")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)

        o.undo_selected_files([str(self.downloads / "a.pdf")])
        o.undo_selected_files([str(self.downloads / "b.pdf")])
        self.assertTrue((self.downloads / "b.pdf").exists())

        history = org.load_history()
        self.assertTrue(history[-1]["undone"])  # les deux fichiers traites : lot termine

    def test_undo_selected_files_with_no_matching_source_does_nothing(self):
        self._write("a.pdf", "a")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)
        outcome = o.undo_selected_files(["/chemin/qui/nexiste/pas.pdf"])
        self.assertEqual(outcome["undone"], [])
        self.assertFalse((self.downloads / "a.pdf").exists())

    # -- validations de dossiers -------------------------------------------

    def test_empty_downloads_dir_is_rejected(self):
        o = self._organizer(downloads_dir="")
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)
        self.assertIn("pas renseigne", result.errors[0][1])

    def test_missing_downloads_dir_is_rejected(self):
        o = self._organizer(downloads_dir=str(self.tmp / "n_existe_pas"))
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)
        self.assertIn("introuvable", result.errors[0][1])

    def test_empty_base_target_dir_is_rejected(self):
        o = self._organizer(base_target_dir="")
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)
        self.assertIn("pas renseigne", result.errors[0][1])

    def test_missing_base_target_dir_is_rejected(self):
        o = self._organizer(base_target_dir=str(self.tmp / "n_existe_pas"))
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)
        self.assertIn("introuvable", result.errors[0][1])

    def test_downloads_dir_pointing_at_drive_root_is_rejected(self):
        drive_root = Path(self.tmp.anchor)  # ex: "C:\\"
        o = self._organizer(downloads_dir=str(drive_root))
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)
        self.assertIn("dossier systeme", result.errors[0][1])

    def test_base_target_dir_pointing_at_windows_system_dir_is_rejected(self):
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
        if not system_root or not Path(system_root).exists():
            self.skipTest("SystemRoot/windir non disponible sur cette machine")
        self._write("a.pdf")
        o = self._organizer(base_target_dir=system_root)
        result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)
        self.assertIn("dossier systeme", result.errors[0][1])

    def test_normal_target_dir_is_not_flagged_as_sensitive(self):
        # Garde-fou contre les faux positifs : le dossier de test normal
        # (sous un dossier temporaire) ne doit jamais etre rejete a tort.
        self.assertIsNone(org.DownloadOrganizer._is_sensitive_system_path(self.target))

    def test_unreadable_downloads_dir_is_reported_not_crashed(self):
        # Simule un dossier existant mais illisible (permissions refusees,
        # lecteur reseau instable) : plan() doit degrader gracieusement
        # plutot que de laisser l'exception se propager et planter l'appli.
        import unittest.mock as mock
        self._write("a.pdf")
        o = self._organizer()
        with mock.patch.object(Path, "iterdir", side_effect=OSError("Acces refuse")):
            result = o.plan()
        self.assertTrue(result.errors)
        self.assertFalse(result.moves)

    def test_subdirectories_are_reported_not_silently_dropped(self):
        (self.downloads / "un_sous_dossier").mkdir()
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.skipped_dirs), 1)
        self.assertEqual(result.skipped_dirs[0].name, "un_sous_dossier")

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

    def test_same_size_different_content_is_not_a_duplicate(self):
        # Meme taille en octets, contenu different : l'optimisation qui
        # regroupe par taille avant de hasher ne doit pas se transformer en
        # detection de doublon par taille seule.
        self._write("a.pdf", "AAAA")
        self._write("b.pdf", "BBBB")
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 2)
        self.assertTrue(all(m.category == "PDF" for m in result.moves))

    # -- reconnaissance par signature de fichier (magic bytes) --------------

    def _write_bytes(self, relative_path: str, content: bytes):
        path = self.downloads / relative_path
        path.write_bytes(content)
        return path

    def test_exe_renamed_as_pdf_is_flagged_for_review(self):
        # En-tete MZ (executable Windows) mais extension .pdf : incoherence.
        self._write_bytes("facture.pdf", b"MZ" + b"\x00" * 62)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "A verifier")
        self.assertIn("incoherente", result.moves[0].reason)
        self.assertIn("Installateurs", result.moves[0].reason)

    def test_unknown_extension_recognized_by_signature(self):
        self._write_bytes("mystere.download2", b"%PDF-1.7\n%content")
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "PDF")
        self.assertIn("signature", result.moves[0].reason)

    def test_matching_extension_and_signature_is_not_flagged(self):
        self._write_bytes("photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 60)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "Images")
        self.assertNotIn("incoherente", result.moves[0].reason)

    def test_apk_zip_signature_is_not_falsely_flagged(self):
        # Un .apk est techniquement un conteneur ZIP : ce n'est pas une
        # incoherence a signaler, c'est le format normal de cet installateur.
        self._write_bytes("appli.apk", b"PK\x03\x04" + b"\x00" * 60)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "Installateurs")
        self.assertNotIn("incoherente", result.moves[0].reason)

    def test_docx_zip_signature_not_misfiled_as_archive(self):
        # Un .docx est un ZIP en interne mais n'est pas une categorie geree ;
        # il ne doit pas etre reclasse en "Archives" pour autant.
        old_time = time.time() - 200 * 86400
        f = self._write_bytes("document.docx", b"PK\x03\x04" + b"\x00" * 60)
        os.utime(f, (old_time, old_time))
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "A verifier")

    def test_msix_zip_signature_is_not_falsely_flagged(self):
        # Comme .apk, .msix est un conteneur ZIP legitime pour un installateur.
        self._write_bytes("appli.msix", b"PK\x03\x04" + b"\x00" * 60)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "Installateurs")
        self.assertNotIn("incoherente", result.moves[0].reason)

    def test_signature_detection_handles_empty_and_short_files(self):
        # Un fichier vide ou plus court que la taille de lecture de signature
        # ne doit jamais faire planter la detection, meme sans correspondre
        # a aucune signature connue.
        self._write_bytes("vide.inconnu", b"")
        self._write_bytes("court.inconnu2", b"XY")
        o = self._organizer()
        result = o.plan()  # ne doit lever aucune exception
        # Ni l'un ni l'autre n'a de signature/extension reconnue et ils ne
        # sont pas assez vieux : ils tombent dans "excluded", pas un crash.
        self.assertEqual(len(result.moves), 0)
        self.assertEqual(len(result.excluded), 2)

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
        self.assertEqual(cfg["exclusions"], org.DEFAULT_CONFIG["exclusions"])

    def test_config_with_boolean_threshold_is_rejected(self):
        # bool est une sous-classe d'int en Python : un JSON corrompu avec
        # "old_file_threshold_days": true ne doit pas etre accepte tel quel.
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.CONFIG_FILE.write_text(json.dumps({"old_file_threshold_days": True}), encoding="utf-8")
        cfg = org.load_config()
        self.assertEqual(cfg["old_file_threshold_days"], org.DEFAULT_CONFIG["old_file_threshold_days"])

    def test_export_config_then_import_round_trips(self):
        cfg = copy.deepcopy(org.DEFAULT_CONFIG)
        cfg["downloads_dir"] = str(self.downloads)
        cfg["old_file_threshold_days"] = 45
        cfg["exclusions"]["extensions"] = [".tmp"]
        export_path = self.tmp / "export" / "ma_config.json"

        org.export_config(cfg, export_path)
        self.assertTrue(export_path.exists())

        imported = org.import_config(export_path)
        self.assertEqual(imported["downloads_dir"], str(self.downloads))
        self.assertEqual(imported["old_file_threshold_days"], 45)
        self.assertEqual(imported["exclusions"]["extensions"], [".tmp"])

    def test_import_config_rejects_non_object_json(self):
        bad_path = self.tmp / "bad_config.json"
        bad_path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(ValueError):
            org.import_config(bad_path)

    def test_import_config_ignores_unknown_and_mistyped_fields(self):
        # Un fichier importe n'est pas plus digne de confiance qu'un
        # config.json corrompu trouve sur disque : memes garde-fous.
        malicious_path = self.tmp / "suspicious_config.json"
        malicious_path.write_text(json.dumps({
            "downloads_dir": str(self.downloads),
            "old_file_threshold_days": True,  # bool rejete, comme pour load_config
            "some_unknown_field": "ignored",
            "exclusions": "not-a-dict",
        }), encoding="utf-8")
        imported = org.import_config(malicious_path)
        self.assertEqual(imported["downloads_dir"], str(self.downloads))
        self.assertEqual(imported["old_file_threshold_days"], org.DEFAULT_CONFIG["old_file_threshold_days"])
        self.assertEqual(imported["exclusions"], org.DEFAULT_CONFIG["exclusions"])
        self.assertNotIn("some_unknown_field", imported)

    def test_import_config_raises_on_malformed_json(self):
        bad_path = self.tmp / "not_json.json"
        bad_path.write_text("{not valid json at all", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            org.import_config(bad_path)

    def test_corrupted_history_returns_empty_list_not_crash(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.HISTORY_FILE.write_text("pas du JSON", encoding="utf-8")
        self.assertEqual(org.load_history(), [])

    def test_custom_category_routes_matching_files_without_code_changes(self):
        o = self._organizer(custom_categories=[
            {"name": "Musique", "extensions": [".mp3", ".flac"], "target": "Musique"},
        ])
        self._write("chanson.mp3")
        result = o.plan()
        moved = [m for m in result.moves if m.category == "Musique"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].destination.parent, self.target / "Musique")

    def test_custom_category_cannot_override_a_builtin_category_name(self):
        # "PDF" est deja une categorie integree avec sa propre detection de
        # signature - une categorie personnalisee du meme nom est ignoree
        # plutot que de silencieusement remplacer le comportement integre.
        o = self._organizer(custom_categories=[
            {"name": "PDF", "extensions": [".weird"], "target": "Autre"},
        ])
        self.assertEqual(o.categories["PDF"]["target"], org.DEFAULT_CATEGORIES["PDF"]["target"])
        self.assertNotIn(".weird", o.categories["PDF"]["extensions"])

    def test_builtin_category_extension_always_wins_over_custom_category(self):
        # Un ".pdf" doit toujours suivre la detection integree (signature +
        # categorie "PDF"), meme si une categorie personnalisee tente de
        # reclamer la meme extension.
        o = self._organizer(custom_categories=[
            {"name": "AutrePDF", "extensions": [".pdf"], "target": "AutrePDF"},
        ])
        self._write("doc.pdf", "%PDF-1.4 contenu")
        result = o.plan()
        moved = [m for m in result.moves if Path(m.source).name == "doc.pdf"]
        self.assertEqual(moved[0].category, "PDF")

    def test_get_effective_categories_ignores_malformed_custom_entries(self):
        config = copy.deepcopy(org.DEFAULT_CONFIG)
        config["custom_categories"] = [
            {"name": "", "extensions": [".x"], "target": "X"},  # nom vide
            {"name": "SansExtension", "extensions": [], "target": "Y"},  # aucune extension
            {"name": "SansCible", "extensions": [".z"], "target": ""},  # cible vide
            "pas un dict",
        ]
        effective = org.get_effective_categories(config)
        self.assertNotIn("", effective)
        self.assertNotIn("SansExtension", effective)
        self.assertNotIn("SansCible", effective)

    def test_import_config_rejects_malformed_custom_categories(self):
        malicious_path = self.tmp / "bad_categories.json"
        malicious_path.write_text(json.dumps({
            "custom_categories": [{"name": "X"}],  # extensions/target manquants
        }), encoding="utf-8")
        imported = org.import_config(malicious_path)
        self.assertEqual(imported["custom_categories"], org.DEFAULT_CONFIG["custom_categories"])

    def test_purge_history_keeps_only_the_n_most_recent_batches(self):
        for i in range(5):
            org.append_batch_to_history({"index": i})
        removed = org.purge_history(keep_last=2)
        self.assertEqual(removed, 3)
        remaining = org.load_history()
        self.assertEqual([b["index"] for b in remaining], [3, 4])

    def test_purge_history_with_no_argument_clears_everything(self):
        org.append_batch_to_history({"index": 1})
        org.append_batch_to_history({"index": 2})
        removed = org.purge_history()
        self.assertEqual(removed, 2)
        self.assertEqual(org.load_history(), [])

    def test_purge_history_keep_last_larger_than_history_removes_nothing(self):
        org.append_batch_to_history({"index": 1})
        removed = org.purge_history(keep_last=50)
        self.assertEqual(removed, 0)
        self.assertEqual(len(org.load_history()), 1)

    def test_append_batch_to_history_auto_truncates_at_max_batches(self):
        original_max = org.MAX_HISTORY_BATCHES
        org.MAX_HISTORY_BATCHES = 3
        try:
            for i in range(5):
                org.append_batch_to_history({"index": i})
            history = org.load_history()
            self.assertEqual(len(history), 3)
            # Les plus anciens sont retires en premier, les plus recents gardes.
            self.assertEqual([b["index"] for b in history], [2, 3, 4])
        finally:
            org.MAX_HISTORY_BATCHES = original_max

    def test_config_persists_across_reload(self):
        cfg = copy.deepcopy(org.DEFAULT_CONFIG)
        cfg["downloads_dir"] = str(self.downloads)
        org.save_config(cfg)
        reloaded = org.load_config()
        self.assertEqual(reloaded["downloads_dir"], str(self.downloads))

    def test_history_write_failure_does_not_crash_execute(self):
        # Les fichiers sont deja deplaces physiquement a ce stade : un echec
        # d'ecriture de l'historique (disque plein, permissions) ne doit pas
        # faire perdre le resultat du lot ni planter l'appli.
        import unittest.mock as mock
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        with mock.patch.object(org, "append_batch_to_history", side_effect=OSError("disque plein")):
            batch = o.execute(result, simulate=False)  # ne doit pas lever d'exception
        self.assertIn("history_error", batch)
        moved = [m for m in batch["moves"] if m["status"] == "deplace"]
        self.assertEqual(len(moved), 1)
        self.assertTrue((self.target / "Documents" / "PDF" / "a.pdf").exists())

    def test_undo_with_malformed_history_entry_does_not_crash(self):
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        batch = o.execute(result, simulate=False)
        # Simule une entree d'historique corrompue (edition manuelle) a
        # laquelle il manque la cle "source".
        history = org.load_history()
        del history[-1]["moves"][0]["source"]
        org.save_history(history)

        out = o.undo_last_batch()  # ne doit pas lever d'exception (KeyError)
        self.assertEqual(out["undone"], [])
        self.assertTrue(out["errors"])

    def test_undo_retries_after_conflict_is_resolved(self):
        self._write("a.pdf", "original")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)

        # Un nouveau fichier bloque temporairement l'annulation.
        blocker = self.downloads / "a.pdf"
        blocker.write_text("bloque temporairement")
        first = o.undo_last_batch()
        self.assertEqual(first["undone"], [])
        self.assertTrue(first["errors"])

        # Une fois le conflit resolu (fichier bloquant retire), une nouvelle
        # tentative d'annulation doit pouvoir reussir sur le meme lot.
        blocker.unlink()
        second = o.undo_last_batch()
        self.assertEqual(len(second["undone"]), 1)
        self.assertEqual(blocker.read_text(), "original")

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

    def test_export_html_report_escapes_injected_content(self):
        # Verifie que _html_escape est bien applique aux champs interpoles,
        # pas seulement que le fichier ne plante pas a la generation.
        batch = {
            "timestamp": "2026-01-01 12:00:00",
            "simulated": False,
            "moves": [
                {
                    "source": "<script>bad.pdf",
                    "destination": "C:/Target/PDF/<script>bad.pdf",
                    "category": "PDF",
                    "reason": "extension .pdf & test",
                    "status": "deplace",
                }
            ],
        }
        report_path = self.tmp / "rapport_injection.html"
        org.export_html_report(batch, report_path)
        content = report_path.read_text(encoding="utf-8")
        self.assertNotIn("<script>bad.pdf", content)
        self.assertIn("&lt;script&gt;bad.pdf", content)
        self.assertIn("&amp;", content)

    def test_export_html_report_hides_username_via_relative_paths(self):
        # Le rapport est concu pour etre partage : un chemin de destination
        # absolu (C:\Users\<nom_utilisateur>\...) reveler ait le nom du
        # compte Windows sans que ce soit utile au rapport.
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()
        batch = o.execute(result, simulate=False)

        report_path = self.tmp / "rapport_relatif.html"
        org.export_html_report(batch, report_path, base_dir=self.target)
        content = report_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.target), content)
        self.assertIn("Documents", content)  # chemin relatif toujours lisible


if __name__ == "__main__":
    unittest.main()

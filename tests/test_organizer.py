"""Tests de regression pour organizer.py.

Ces tests verifient en priorite la garantie centrale de l'outil : aucun
deplacement/annulation ne doit jamais ecraser un fichier existant. Ils
couvrent aussi les bugs trouves lors des audits de code successifs, pour
empecher toute regression future.
"""

import copy
import io
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

    def test_execute_does_not_overwrite_destination_that_appeared_after_plan(self):
        # Regression trouvee a l'audit : contrairement a _attempt_restore_entry
        # (annulation), execute() ne reverifiait jamais que move.destination
        # n'existait pas juste avant le shutil.move reel. Si un fichier
        # apparait a la destination planifiee entre plan() et execute() (une
        # fenetre potentiellement large en Mode Veille), shutil.move
        # l'ecrasait silencieusement.
        self._write("a.pdf", "original")
        o = self._organizer()
        result = o.plan()

        dest = result.moves[0].destination
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("fichier apparu entre plan() et execute()")

        batch = o.execute(result, simulate=False)

        self.assertEqual(dest.read_text(), "fichier apparu entre plan() et execute()")
        self.assertTrue((self.downloads / "a.pdf").exists())
        self.assertEqual((self.downloads / "a.pdf").read_text(), "original")
        self.assertEqual(batch["moves"][0]["status"], "erreur")
        self.assertIn("existe deja", batch["moves"][0]["error"])

    # -- audit B2 : indicateur de progression pendant plan()/execute() -----

    def test_plan_progress_callback_reports_every_entry_up_to_the_total(self):
        for index in range(4):
            self._write(f"doc{index}.pdf")
        (self.downloads / "un_sous_dossier").mkdir()  # aussi compte comme une entree
        o = self._organizer()
        calls = []
        result = o.plan(progress_callback=lambda done, total: calls.append((done, total)))

        self.assertEqual(len(result.moves), 4)
        # 4 fichiers + 1 sous-dossier = 5 entrees examinees.
        self.assertEqual(len(calls), 5)
        # Toujours le meme total, une progression strictement croissante de 1 a 5.
        self.assertTrue(all(total == 5 for _done, total in calls))
        self.assertEqual([done for done, _total in calls], [1, 2, 3, 4, 5])

    def test_plan_without_progress_callback_still_works(self):
        self._write("a.pdf")
        o = self._organizer()
        result = o.plan()  # aucune exception meme sans callback (valeur par defaut None)
        self.assertEqual(len(result.moves), 1)

    def test_execute_progress_callback_reports_every_move(self):
        for index in range(3):
            self._write(f"doc{index}.pdf", content=f"contenu-{index}")
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 3)

        calls = []
        o.execute(result, simulate=False, progress_callback=lambda done, total: calls.append((done, total)))

        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_execute_simulate_progress_callback_reports_every_move(self):
        for index in range(2):
            self._write(f"doc{index}.pdf")
        o = self._organizer()
        result = o.plan()
        calls = []
        o.execute(result, simulate=True, progress_callback=lambda done, total: calls.append((done, total)))
        self.assertEqual(calls, [(1, 2), (2, 2)])

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

    # -- annulation d'un lot quelconque de l'historique --------------------

    def test_undo_batch_at_restores_older_batch_and_leaves_recent_intact(self):
        self._write("a.pdf", "contenu a")
        o = self._organizer()
        o.execute(o.plan(), simulate=False)
        self._write("b.pdf", "contenu b")
        o.execute(o.plan(), simulate=False)

        out = o.undo_batch_at(0)
        self.assertEqual(len(out["undone"]), 1)
        self.assertEqual(out["errors"], [])
        self.assertEqual((self.downloads / "a.pdf").read_text(), "contenu a")
        # Le lot le plus recent n'est pas touche : b.pdf reste range.
        self.assertFalse((self.downloads / "b.pdf").exists())
        self.assertTrue((self.target / "Documents" / "PDF" / "b.pdf").exists())

        history = org.load_history()
        self.assertTrue(history[0].get("undone"))
        self.assertFalse(history[1].get("undone"))

    def test_undo_batch_at_conflict_does_not_overwrite_nor_mark_batch_undone(self):
        self._write("a.pdf", "original")
        o = self._organizer()
        o.execute(o.plan(), simulate=False)
        # Un fichier de meme nom reapparait a la source apres le rangement.
        (self.downloads / "a.pdf").write_text("NOUVEAU FICHIER")

        out = o.undo_batch_at(0)
        self.assertEqual(out["undone"], [])
        self.assertTrue(out["errors"])
        self.assertEqual((self.downloads / "a.pdf").read_text(), "NOUVEAU FICHIER")
        # Conflit potentiellement resoluble : le lot doit rester retentable.
        history = org.load_history()
        self.assertFalse(history[0].get("undone"))

    def test_undo_batch_at_refuses_simulated_batch(self):
        org.append_batch_to_history({
            "timestamp": "2026-01-01 12:00:00", "simulated": True,
            "moves": [{"source": "x.pdf", "destination": "y/x.pdf", "status": "planifie"}],
        })
        o = self._organizer()
        out = o.undo_batch_at(0)
        self.assertEqual(out["undone"], [])
        self.assertEqual(out["errors"], [])
        self.assertIn("simulation", out["message"])

    def test_undo_batch_at_refuses_already_undone_batch(self):
        self._write("a.pdf", "contenu")
        o = self._organizer()
        o.execute(o.plan(), simulate=False)
        first = o.undo_last_batch()
        self.assertEqual(len(first["undone"]), 1)

        out = o.undo_batch_at(0)
        self.assertEqual(out["undone"], [])
        self.assertEqual(out["errors"], [])
        self.assertIn("deja", out["message"])

    def test_undo_batch_at_out_of_range_index_returns_message(self):
        o = self._organizer()
        for bad_index in (-1, 0, 42):
            out = o.undo_batch_at(bad_index)
            self.assertEqual(out["undone"], [])
            self.assertEqual(out["errors"], [])
            self.assertIn("introuvable", out["message"])

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

    def test_exe_renamed_as_docx_is_flagged_for_review(self):
        # Un executable Windows (en-tete MZ) renomme en .docx ne doit pas
        # echapper a la detection sous pretexte que .docx fait partie des
        # extensions "conteneur ZIP legitime" : ce garde-fou ne doit couvrir
        # que le cas ou la signature detectee est bien "Archives" (un vrai
        # document bureautique), pas n'importe quelle signature.
        self._write_bytes("cv_candidat.docx", b"MZ" + b"\x00" * 62)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "A verifier")
        self.assertIn("incoherente", result.moves[0].reason)
        self.assertIn("Installateurs", result.moves[0].reason)

    def test_exe_renamed_as_apk_still_uses_apk_category(self):
        # Cas de non-regression : un .apk garde son comportement existant
        # (ext_category deja connu = "Installateurs"), le nouveau garde-fou
        # cible sur les extensions sans categorie connue (docx/xlsx/...) ne
        # doit rien changer ici.
        self._write_bytes("appli.apk", b"MZ" + b"\x00" * 62)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "Installateurs")

    def test_msix_zip_signature_is_not_falsely_flagged(self):
        # Comme .apk, .msix est un conteneur ZIP legitime pour un installateur.
        self._write_bytes("appli.msix", b"PK\x03\x04" + b"\x00" * 60)
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "Installateurs")
        self.assertNotIn("incoherente", result.moves[0].reason)

    def test_custom_category_with_docx_extension_is_not_falsely_flagged(self):
        # Regression trouvee a l'audit (A2) : une categorie personnalisee sur
        # ".docx" faisait passer 100% des vrais .docx (conteneur ZIP legitime,
        # signature "Archives") pour une incoherence extension/signature,
        # puisque la branche "ext_category revendique deja l'extension" ne
        # consultait pas ZIP_LIKE_NON_ARCHIVE_EXTENSIONS (contrairement a la
        # branche "extension inconnue" qui, elle, la consultait deja - voir
        # test_docx_zip_signature_not_misfiled_as_archive pour le cas sans
        # categorie personnalisee).
        o = self._organizer(custom_categories=[
            {"name": "DocumentsOffice", "extensions": [".docx", ".xlsx"], "target": "Documents/Office"},
        ])
        self._write_bytes("rapport_reel.docx", b"PK\x03\x04" + b"\x00" * 60)
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "DocumentsOffice")
        self.assertNotIn("incoherente", result.moves[0].reason)
        self.assertEqual(result.moves[0].destination.parent, self.target / "Documents" / "Office")

    def test_custom_category_with_xlsx_extension_is_not_falsely_flagged(self):
        o = self._organizer(custom_categories=[
            {"name": "DocumentsOffice", "extensions": [".docx", ".xlsx"], "target": "Documents/Office"},
        ])
        self._write_bytes("budget_reel.xlsx", b"PK\x03\x04" + b"\x00" * 60)
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "DocumentsOffice")
        self.assertNotIn("incoherente", result.moves[0].reason)

    def test_custom_category_with_epub_extension_is_not_falsely_flagged(self):
        o = self._organizer(custom_categories=[
            {"name": "Lectures", "extensions": [".epub"], "target": "Lectures"},
        ])
        self._write_bytes("roman_reel.epub", b"PK\x03\x04" + b"\x00" * 60)
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "Lectures")
        self.assertNotIn("incoherente", result.moves[0].reason)

    def test_exe_renamed_as_docx_with_custom_category_is_still_flagged_for_review(self):
        # Non-regression : le garde-fou doit continuer a detecter une vraie
        # anomalie (executable renomme en .docx) meme quand une categorie
        # personnalisee revendique l'extension - seul le cas legitime
        # (signature "Archives") doit etre exempte, pas n'importe quelle
        # signature.
        o = self._organizer(custom_categories=[
            {"name": "DocumentsOffice", "extensions": [".docx"], "target": "Documents/Office"},
        ])
        self._write_bytes("cv_candidat.docx", b"MZ" + b"\x00" * 62)
        result = o.plan()
        self.assertEqual(len(result.moves), 1)
        self.assertEqual(result.moves[0].category, "A verifier")
        self.assertIn("incoherente", result.moves[0].reason)
        self.assertIn("Installateurs", result.moves[0].reason)

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

    def test_builtin_protections_cover_partial_download_variants_and_shortcuts(self):
        # Audit A8 (note)/A9 : *.opdownload (Opera), *.!ut/*.!qb (torrent) et
        # *.lnk (raccourcis Windows) doivent rester proteges en permanence,
        # meme vieux et meme avec des exclusions personnalisees videes -
        # exactement comme *.crdownload/*.part/*.tmp/desktop.ini/*.download.
        old_time = time.time() - 200 * 86400
        names = ["film.opdownload", "installeur.exe.!ut", "jeu.!qb", "Raccourci.lnk"]
        for name in names:
            f = self._write(name)
            os.utime(f, (old_time, old_time))
        o = self._organizer(exclusions={"extensions": [], "filenames": [], "patterns": []})
        result = o.plan()
        self.assertEqual(len(result.moves), 0)
        self.assertEqual(len(result.excluded), len(names))

    def test_filename_exclusion_is_case_insensitive(self):
        self._write("Rapport.pdf")
        o = self._organizer(exclusions={"extensions": [], "filenames": ["rapport.pdf"], "patterns": []})
        result = o.plan()
        self.assertEqual(len(result.moves), 0)

    # -- audit L1 : app.log ne grossit plus indefiniment (rotation) --------

    def test_log_file_uses_a_rotating_handler_with_a_bounded_size(self):
        org._ensure_logging_configured()
        self.assertEqual(len(org.logger.handlers), 1)
        handler = org.logger.handlers[0]
        self.assertIsInstance(handler, org.logging.handlers.RotatingFileHandler)
        self.assertEqual(handler.maxBytes, org.LOG_MAX_BYTES)
        self.assertEqual(handler.backupCount, org.LOG_BACKUP_COUNT)

    def test_log_rotation_caps_the_number_of_backup_files(self):
        # Force un tout petit plafond pour declencher plusieurs rotations
        # sans avoir a ecrire un vrai megaoctet de journal dans ce test.
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        original_max_bytes, original_backup_count = org.LOG_MAX_BYTES, org.LOG_BACKUP_COUNT
        org.LOG_MAX_BYTES, org.LOG_BACKUP_COUNT = 500, 2
        try:
            org._ensure_logging_configured()
            for i in range(200):
                org.logger.warning("ligne de journal de test numero %d - contenu de remplissage", i)
        finally:
            org.LOG_MAX_BYTES, org.LOG_BACKUP_COUNT = original_max_bytes, original_backup_count

        self.assertTrue(org.LOG_FILE.exists())
        # Au plus backupCount fichiers de sauvegarde (.1, .2, ...) en plus du
        # fichier courant - jamais une croissance illimitee du nombre de
        # fichiers ni de leur taille individuelle.
        backups = sorted(org.LOG_FILE.parent.glob(org.LOG_FILE.name + ".*"))
        self.assertLessEqual(len(backups), 2)

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

    def test_video_files_are_routed_to_builtin_videos_category(self):
        # Contenu distinct par fichier : un contenu identique ("x" par
        # defaut) ferait passer tous ces fichiers pour des doublons entre
        # eux, ce qui n'est pas ce que ce test verifie.
        names = ("film.mp4", "serie.mkv", "clip.avi", "vieux.wmv", "web.webm", "quicktime.mov")
        for index, name in enumerate(names):
            self._write(name, content=f"contenu-{index}")
        o = self._organizer()
        result = o.plan()
        by_name = {Path(m.source).name: m for m in result.moves}
        for name in names:
            self.assertEqual(by_name[name].category, "Videos")
            self.assertEqual(by_name[name].destination.parent, self.target / "Videos")

    def test_audio_files_are_routed_to_builtin_audio_category(self):
        names = ("chanson.mp3", "album.flac", "voix.wav", "piste.aac", "son.ogg", "vieux.wma", "livre.m4a")
        for index, name in enumerate(names):
            self._write(name, content=f"contenu-{index}")
        o = self._organizer()
        result = o.plan()
        by_name = {Path(m.source).name: m for m in result.moves}
        for name in names:
            self.assertEqual(by_name[name].category, "Audio")
            self.assertEqual(by_name[name].destination.parent, self.target / "Audio")

    def test_custom_category_routes_matching_files_without_code_changes(self):
        # Extension ".mid" volontairement choisie car non revendiquee par
        # aucune categorie integree (contrairement a ".mp3"/".flac", desormais
        # couvertes par la categorie integree "Audio" - voir
        # test_builtin_category_extension_always_wins_over_custom_category).
        o = self._organizer(custom_categories=[
            {"name": "Musique", "extensions": [".mid", ".xm"], "target": "Musique"},
        ])
        self._write("chanson.mid")
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

    def test_custom_category_name_pattern_routes_by_filename(self):
        o = self._organizer(custom_categories=[
            {"name": "Factures", "extensions": [".pdf"], "target": "Factures", "name_patterns": ["facture*.pdf"]},
        ])
        self._write("facture_juillet.pdf", "%PDF-1.4 contenu")
        result = o.plan()
        moved = [m for m in result.moves if Path(m.source).name == "facture_juillet.pdf"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].category, "Factures")
        self.assertEqual(moved[0].destination.parent, self.target / "Factures")

    def test_name_pattern_takes_priority_over_a_builtin_category_extension(self):
        # A la difference d'une categorie personnalisee par EXTENSION (qui
        # perd toujours face a une categorie integree), un motif de nom est
        # une regle plus specifique et doit l'emporter meme sur PDF.
        o = self._organizer(custom_categories=[
            {"name": "Factures", "extensions": [".pdf"], "target": "Factures", "name_patterns": ["facture*.pdf"]},
        ])
        self._write("facture_aout.pdf", "%PDF-1.4 contenu facture")
        self._write("autre_doc.pdf", "%PDF-1.4 contenu different")
        result = o.plan()
        by_name = {Path(m.source).name: m.category for m in result.moves}
        self.assertEqual(by_name["facture_aout.pdf"], "Factures")
        self.assertEqual(by_name["autre_doc.pdf"], "PDF")

    def test_name_pattern_match_on_genuinely_matching_content_is_not_flagged_as_mismatch(self):
        # Un vrai PDF ("%PDF-1.4...") route par motif de nom vers une
        # categorie de nom different ("Factures") ne doit PAS etre traite
        # comme une incoherence extension/contenu simplement parce que le
        # nom de la categorie ("Factures") ne correspond pas au nom "PDF"
        # detecte par signature - il n'y a ici aucune incoherence REELLE
        # (le contenu est bien un PDF), seul le nom de la categorie differe.
        o = self._organizer(custom_categories=[
            {"name": "Factures", "extensions": [".pdf"], "target": "Factures", "name_patterns": ["facture*.pdf"]},
        ])
        self._write("facture_2026.pdf", "%PDF-1.4 vrai contenu pdf")
        result = o.plan()
        moved = [m for m in result.moves if Path(m.source).name == "facture_2026.pdf"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].category, "Factures")

    def test_a_broad_name_pattern_never_bypasses_a_genuine_signature_mismatch(self):
        # Regression trouvee a l'audit : un motif large et parfaitement
        # plausible comme "*.pdf" (router TOUS ses PDF vers "Factures")
        # routait aussi, sans le moindre avertissement, un executable
        # renomme en ".pdf" - la toute chose que la quarantaine
        # extension/signature existe pour detecter. Le motif de nom ne doit
        # jamais dispenser de cette verification quand une incoherence
        # REELLE existe entre l'extension et le contenu.
        o = self._organizer(custom_categories=[
            {"name": "Factures", "extensions": [".pdf"], "target": "Factures", "name_patterns": ["*.pdf"]},
        ])
        self._write("invoice.pdf", "MZ" + "\x00" * 100)  # signature d'executable Windows (PE), pas un PDF
        result = o.plan()
        moved = [m for m in result.moves if Path(m.source).name == "invoice.pdf"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].category, org.OLD_FILES_TARGET)
        self.assertIn("incoherente", moved[0].reason)

    def test_name_pattern_matching_is_case_insensitive(self):
        o = self._organizer(custom_categories=[
            {"name": "Factures", "extensions": [".pdf"], "target": "Factures", "name_patterns": ["FACTURE*.pdf"]},
        ])
        self._write("Facture_Client.pdf", "%PDF-1.4 contenu")
        result = o.plan()
        moved = [m for m in result.moves if Path(m.source).name == "Facture_Client.pdf"]
        self.assertEqual(moved[0].category, "Factures")

    def test_custom_category_without_name_patterns_behaves_exactly_as_before(self):
        # ".mid" plutot que ".mp3" : voir
        # test_custom_category_routes_matching_files_without_code_changes.
        o = self._organizer(custom_categories=[
            {"name": "Musique", "extensions": [".mid"], "target": "Musique"},
        ])
        self.assertEqual(o.categories["Musique"].get("name_patterns"), [])
        self._write("chanson.mid")
        result = o.plan()
        moved = [m for m in result.moves if m.category == "Musique"]
        self.assertEqual(len(moved), 1)

    def test_is_valid_custom_categories_rejects_non_string_name_patterns(self):
        self.assertFalse(org._is_valid_custom_categories([
            {"name": "X", "extensions": [".x"], "target": "X", "name_patterns": [123]},
        ]))

    def test_is_valid_custom_categories_accepts_missing_name_patterns(self):
        self.assertTrue(org._is_valid_custom_categories([
            {"name": "X", "extensions": [".x"], "target": "X"},
        ]))

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

    def test_custom_category_with_absolute_windows_target_is_rejected_at_validation(self):
        # Trouve lors d'un audit : Path(base) / 'C:/Windows/System32' produit
        # silencieusement 'C:/Windows/System32' (l'operateur / de pathlib
        # jette la partie gauche face a un chemin absolu) - le garde-fou
        # doit intercepter ceci a la validation, avant meme d'atteindre plan().
        config = copy.deepcopy(org.DEFAULT_CONFIG)
        config["custom_categories"] = [
            {"name": "Backup", "extensions": [".bak"], "target": r"C:\Windows\System32"},
        ]
        self.assertFalse(org._is_valid_custom_categories(config["custom_categories"]))

    def test_custom_category_with_dot_dot_target_is_rejected_at_validation(self):
        self.assertFalse(org._is_valid_custom_categories([
            {"name": "Evil", "extensions": [".evil"], "target": "../../ailleurs"},
        ]))

    def test_custom_category_with_rooted_but_driveless_target_is_caught_at_plan_time(self):
        # '/Windows' (ou '\\Windows') n'est PAS detecte par Path.is_absolute()
        # sous Windows (chemin "enracine" sans lecteur), mais produit quand
        # meme un chemin hors de base_target_dir une fois combine via `/` -
        # d'ou le garde-fou complementaire _is_within_base verifie dans plan().
        config = copy.deepcopy(self.config)
        config["custom_categories"] = [
            {"name": "Evil", "extensions": [".evil"], "target": "/Windows"},
        ]
        o = self._organizer(custom_categories=config["custom_categories"])
        self._write("fichier.evil")
        result = o.plan()
        self.assertEqual(result.moves, [])
        self.assertTrue(result.errors)
        self.assertIn("sort du dossier de destination racine", result.errors[0][1])

    def test_custom_category_targeting_a_sensitive_system_path_is_rejected_at_plan_time(self):
        windows_dir = os.environ.get("SystemRoot") or os.environ.get("windir")
        if not windows_dir:
            self.skipTest("SystemRoot/windir non defini dans cet environnement")
        # Un target relatif "normal" en apparence (une seule remontee ".."
        # rejetee par la validation ; ici on simule plutot un base_target_dir
        # place directement dans un sous-dossier systeme pour verifier le
        # deuxieme filet de securite independamment du premier).
        sensitive_target = self.tmp / "FakeSystemRoot"
        sensitive_target.mkdir()
        config = copy.deepcopy(self.config)
        config["base_target_dir"] = str(sensitive_target)
        config["custom_categories"] = [
            {"name": "Musique", "extensions": [".mp3"], "target": "Musique"},
        ]
        o = self._organizer(base_target_dir=str(sensitive_target), custom_categories=config["custom_categories"])
        # Reroute directement _is_sensitive_system_path pour simuler un
        # dossier cible de categorie qui serait, lui, sensible (sans devoir
        # dependre du vrai C:\Windows de la machine de test).
        original_check = org.DownloadOrganizer._is_sensitive_system_path
        def fake_check(path):
            if path.name == "Musique":
                return "un dossier systeme (simule)"
            return original_check(path)
        org.DownloadOrganizer._is_sensitive_system_path = staticmethod(fake_check)
        try:
            self._write("chanson.mp3")
            result = o.plan()
        finally:
            org.DownloadOrganizer._is_sensitive_system_path = staticmethod(original_check)
        self.assertEqual(result.moves, [])
        self.assertTrue(result.errors)
        self.assertIn("dossier systeme", result.errors[0][1])

    def test_is_within_base_accepts_normal_subdirectories_and_rejects_escapes(self):
        base = self.target
        self.assertTrue(org.DownloadOrganizer._is_within_base(base / "Documents" / "PDF", base))
        self.assertTrue(org.DownloadOrganizer._is_within_base(base, base))
        self.assertFalse(org.DownloadOrganizer._is_within_base(base / ".." / "ailleurs", base))
        self.assertFalse(org.DownloadOrganizer._is_within_base(Path(base.anchor) / "Windows", base))

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

    def test_execute_checkpointing_survives_compaction_triggered_mid_batch(self):
        # Cas limite du correctif A1 : si le plafond MAX_HISTORY_BATCHES est
        # deja atteint au moment ou execute() ecrit son tout premier
        # checkpoint (lot encore vide, avant le premier deplacement reel),
        # append_batch_to_history() declenche un compactage complet qui
        # reecrit l'integralite du fichier JSONL - invalidant l'offset
        # initialement calcule pour ce lot. execute() doit malgre tout
        # continuer a checkpointer correctement ce lot jusqu'au bout (offset
        # recalcule en consequence), sans corrompre l'historique.
        original_max = org.MAX_HISTORY_BATCHES
        org.MAX_HISTORY_BATCHES = 2
        try:
            org.append_batch_to_history({"timestamp": "ancien-1", "simulated": False, "moves": []})
            org.append_batch_to_history({"timestamp": "ancien-2", "simulated": False, "moves": []})

            self._write("a.pdf")
            self._write("b.pdf")
            self._write("c.pdf")
            o = self._organizer()
            result = o.plan()
            batch = o.execute(result, simulate=False)

            self.assertNotIn("history_error", batch)
            moved = [m for m in batch["moves"] if m["status"] == "deplace"]
            self.assertEqual(len(moved), 3)

            history = org.load_history()
            # Le plafond a bien retire les 2 lots les plus anciens, ne
            # laissant que le lot reel fraichement execute.
            self.assertEqual(len(history), 2)
            self.assertFalse(history[-1].get("simulated"))
            self.assertEqual(
                len([m for m in history[-1]["moves"] if m["status"] == "deplace"]), 3
            )
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

    def test_history_survives_a_hard_interruption_mid_batch(self):
        # Regression audit A1 (Critique) : avant le correctif, execute()
        # n'ecrivait l'historique qu'une seule fois, apres la fin complete
        # de la boucle de deplacement. Un arret brutal du processus (fin de
        # tache forcee, coupure de courant, plantage) en plein milieu d'un
        # gros lot laissait alors les fichiers deja physiquement deplaces
        # SANS AUCUNE trace dans l'historique, ni possibilite d'annulation.
        #
        # On simule cet arret brutal en interrompant shutil.move par une
        # exception non geree par execute() (pas un simple OSError, deja
        # capture proprement comme un echec de fichier individuel isole) au
        # milieu d'un lot de 5 fichiers, apres 2 deplacements physiques deja
        # reussis - execute() ne doit donc jamais retourner de resultat.
        import unittest.mock as mock
        for index in range(5):
            self._write(f"doc{index}.pdf", content=f"contenu-{index}")
        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 5)

        real_move = org.shutil.move
        moved_before_crash = []

        def crashing_move(src, dst, *args, **kwargs):
            # *args/**kwargs : execute() appelle desormais shutil.move avec
            # copy_function=_copy_via_temp_file (audit A10) - ce mock
            # remplace shutil.move dans son integralite, il doit donc
            # accepter (et ignorer) ce kwarg supplementaire pour continuer a
            # intercepter chaque appel comme avant.
            if len(moved_before_crash) >= 2:
                raise RuntimeError("arret brutal simule (ex: fin de tache forcee)")
            real_move(src, dst)
            moved_before_crash.append(src)

        with mock.patch.object(org.shutil, "move", side_effect=crashing_move):
            with self.assertRaises(RuntimeError):
                o.execute(result, simulate=False)

        self.assertEqual(len(moved_before_crash), 2)

        # Les fichiers deplaces AVANT l'interruption doivent malgre tout
        # etre retrouves, complets, dans l'historique persiste sur disque -
        # execute() n'a pourtant jamais eu l'occasion de retourner (donc
        # jamais atteint l'ancien site d'ecriture unique en fin de boucle).
        history = org.load_history()
        self.assertEqual(len(history), 1)
        batch = history[0]
        self.assertFalse(batch.get("simulated"))
        moved_entries = [m for m in batch["moves"] if m["status"] == "deplace"]
        moved_names = {Path(p).name for p in moved_before_crash}
        self.assertEqual({Path(m["source"]).name for m in moved_entries}, moved_names)
        self.assertEqual(len(moved_entries), 2)
        # Les 3 fichiers restants (jamais atteints avant l'interruption) ne
        # doivent pas apparaitre comme deplaces dans ce checkpoint.
        self.assertEqual(len(batch["moves"]), 2)

        # Et donc bien annulables, comme l'exige l'audit : undo_last_batch()
        # doit pouvoir les remettre a leur emplacement d'origine.
        out = o.undo_last_batch()
        self.assertEqual(len(out["undone"]), 2)
        self.assertEqual(out["errors"], [])
        for name in moved_names:
            self.assertTrue((self.downloads / name).exists())
            self.assertFalse((self.target / "Documents" / "PDF" / name).exists())

    # -- audit A10 : pas de fichier tronque en cas d'arret brutal pendant --
    # -- une copie inter-volume (shutil.move retombe sur copy_function) ----

    def test_inter_volume_move_uses_copy_function_and_completes_normally(self):
        # Regression audit A10 : verifie que le copy_function personnalise
        # (_copy_via_temp_file) est bien utilise sur le chemin "inter-volume"
        # simule ici (os.rename force a echouer, comme un vrai deplacement
        # entre deux disques) et qu'un deplacement normal, non interrompu,
        # aboutit toujours a un fichier complet et correct a destination.
        import unittest.mock as mock
        content = "contenu du fichier deplace inter-volume" * 100
        self._write("gros.pdf", content)
        o = self._organizer()
        result = o.plan()

        real_rename = org.os.rename

        def failing_rename(src, dst, *args, **kwargs):
            # Simule l'echec typique d'un renommage inter-disque
            # (WinError 17 sous Windows), qui fait retomber shutil.move sur
            # copy_function.
            raise OSError("simulation : renommage impossible entre deux volumes distincts")

        with mock.patch.object(org.os, "rename", side_effect=failing_rename):
            batch = o.execute(result, simulate=False)

        moved = [m for m in batch["moves"] if m["status"] == "deplace"]
        self.assertEqual(len(moved), 1)
        destination = self.target / "Documents" / "PDF" / "gros.pdf"
        self.assertTrue(destination.exists())
        self.assertEqual(destination.read_text(), content)
        # La source a bien ete supprimee (deplacement complet, pas une copie).
        self.assertFalse((self.downloads / "gros.pdf").exists())
        # Aucun fichier temporaire ".partial" residuel une fois l'operation
        # terminee avec succes.
        leftovers = list(destination.parent.glob("*.partial*"))
        self.assertEqual(leftovers, [])

    def test_inter_volume_move_interrupted_mid_copy_leaves_no_truncated_file_under_final_name(self):
        # Coeur de la regression A10 : simule un arret brutal DU PROCESSUS
        # pendant la phase de copie elle-meme (pas juste une erreur OSError
        # propre deja geree ailleurs) - avant le correctif, un shutil.move
        # standard aurait ecrit directement sous le nom final, laissant un
        # fichier tronque indiscernable d'un fichier complet en cas
        # d'interruption a cet instant precis. Avec le correctif, la copie
        # passe par un nom temporaire : le nom final n'existe alors JAMAIS
        # dans un etat partiel.
        import unittest.mock as mock
        self._write("gros.pdf", "contenu original complet")
        o = self._organizer()
        result = o.plan()

        def failing_rename(src, dst, *args, **kwargs):
            raise OSError("simulation : renommage impossible entre deux volumes distincts")

        def crashing_copy2(src, dst, *args, **kwargs):
            # La copie elle-meme est interrompue brutalement (ex: cable USB
            # debranche, coupure secteur) - jamais un simple retour normal.
            raise RuntimeError("arret brutal simule pendant la copie inter-volume")

        final_destination = self.target / "Documents" / "PDF" / "gros.pdf"
        with mock.patch.object(org.os, "rename", side_effect=failing_rename), \
                mock.patch.object(org.shutil, "copy2", side_effect=crashing_copy2):
            with self.assertRaises(RuntimeError):
                o.execute(result, simulate=False)

        # Le nom de destination FINAL ne doit jamais exister dans un etat
        # partiel/tronque : soit il n'existe pas du tout, soit (si jamais
        # cree par un autre mecanisme) il serait complet - ici, il ne doit
        # simplement pas exister.
        self.assertFalse(final_destination.exists())
        # Le fichier temporaire eventuellement cree avant l'interruption a
        # ete nettoye par _copy_via_temp_file lui-meme.
        leftovers = list(final_destination.parent.glob("*.partial*")) if final_destination.parent.exists() else []
        self.assertEqual(leftovers, [])
        # La source, elle, n'a jamais ete touchee (shutil.move ne supprime
        # la source qu'APRES un copy_function reussi).
        self.assertTrue((self.downloads / "gros.pdf").exists())
        self.assertEqual((self.downloads / "gros.pdf").read_text(), "contenu original complet")

    def test_copy_via_temp_file_cleans_up_temp_file_on_failure(self):
        # Test unitaire cible de _copy_via_temp_file elle-meme (sans passer
        # par execute()) : une copie qui echoue en cours de route ne doit
        # jamais laisser de fichier temporaire orphelin ni de fichier sous
        # le nom final.
        import unittest.mock as mock
        source = self.downloads / "source.bin"
        source.write_bytes(b"donnees")
        destination = self.target / "sous_dossier" / "destination.bin"

        with mock.patch.object(org.shutil, "copy2", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                org._copy_via_temp_file(str(source), str(destination))

        self.assertFalse(destination.exists())
        leftovers = list(destination.parent.glob("*")) if destination.parent.exists() else []
        self.assertEqual(leftovers, [], "un fichier temporaire orphelin a ete laisse a la destination")

    def test_copy_via_temp_file_produces_a_complete_file_at_the_final_name(self):
        source = self.downloads / "source.bin"
        content = b"contenu binaire de test" * 50
        source.write_bytes(content)
        destination = self.target / "sous_dossier" / "destination.bin"

        org._copy_via_temp_file(str(source), str(destination))

        self.assertTrue(destination.exists())
        self.assertEqual(destination.read_bytes(), content)
        # La source n'est PAS supprimee par _copy_via_temp_file elle-meme
        # (c'est le role de l'appelant, shutil.move, une fois copy_function
        # revenu avec succes) : elle doit donc rester intacte ici.
        self.assertTrue(source.exists())
        leftovers = [p for p in destination.parent.glob("*") if p != destination]
        self.assertEqual(leftovers, [])

    # -- audit 2026-07-28, findings eleves : TOCTOU inter-volume, symlinks, --
    # -- copy_function manquant a l'annulation --------------------------------

    def test_copy_via_temp_file_refuses_to_overwrite_destination_appeared_mid_copy(self):
        # Regression finding eleve organizer.py:311 : simule une destination
        # qui apparait PENDANT la copie (course inter-volume) - avant le
        # correctif, os.replace l'aurait ecrasee silencieusement.
        import unittest.mock as mock
        source = self.downloads / "source.bin"
        source.write_bytes(b"contenu source")
        destination = self.target / "sous_dossier" / "destination.bin"
        destination.parent.mkdir(parents=True)

        real_copy2 = org.shutil.copy2

        def copy2_then_race(src, dst, *args, **kwargs):
            # Une destination legitime (un autre processus, un telechargement
            # concurrent) apparait juste apres la fin de la copie, avant le
            # renommage final.
            real_copy2(src, dst, *args, **kwargs)
            destination.write_bytes(b"fichier legitime apparu entre-temps")

        with mock.patch.object(org.shutil, "copy2", side_effect=copy2_then_race):
            with self.assertRaises(org._DestinationAppearedError):
                org._copy_via_temp_file(str(source), str(destination))

        # Le fichier legitime n'a pas ete ecrase.
        self.assertEqual(destination.read_bytes(), b"fichier legitime apparu entre-temps")
        # Aucun fichier temporaire orphelin.
        leftovers = [p for p in destination.parent.glob("*") if p != destination]
        self.assertEqual(leftovers, [])

    def test_execute_does_not_delete_legitimate_file_on_destination_race(self):
        # Complement du test ci-dessus au niveau execute() : le nettoyage de
        # fichier partiel (sur OSError) ne doit PAS supprimer un fichier qui
        # n'est pas le notre.
        import unittest.mock as mock
        self._write("gros.pdf", "contenu original")
        o = self._organizer()
        result = o.plan()

        destination = self.target / "Documents" / "PDF" / "gros.pdf"

        def failing_rename(src, dst, *args, **kwargs):
            raise OSError("simulation : renommage impossible entre deux volumes distincts")

        real_copy2 = org.shutil.copy2

        def copy2_then_race(src, dst, *args, **kwargs):
            real_copy2(src, dst, *args, **kwargs)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fichier legitime apparu entre-temps")

        with mock.patch.object(org.os, "rename", side_effect=failing_rename), \
                mock.patch.object(org.shutil, "copy2", side_effect=copy2_then_race):
            batch = o.execute(result, simulate=False)

        entry = batch["moves"][0]
        self.assertEqual(entry["status"], "erreur")
        # Le fichier apparu pendant la course n'a pas ete supprime par le
        # nettoyage de fichier partiel.
        self.assertTrue(destination.exists())
        self.assertEqual(destination.read_bytes(), b"fichier legitime apparu entre-temps")

    def test_plan_skips_symlinks_instead_of_dereferencing_them(self):
        # Regression finding eleve organizer.py:1105 : un lien symbolique
        # vers un fichier hors du dossier Telechargements ne doit jamais etre
        # traite comme un fichier ordinaire (shutil.move deplacerait/
        # supprimerait alors le contenu POINTE, pas juste le lien).
        target_file = self.tmp / "hors_downloads.pdf"
        target_file.write_text("contenu externe")
        link = self.downloads / "lien.pdf"
        try:
            link.symlink_to(target_file)
        except OSError:
            self.skipTest("symlinks non supportes sans privilege sur cette machine")

        o = self._organizer()
        result = o.plan()

        self.assertEqual(result.moves, [])
        self.assertEqual(len(result.skipped_symlinks), 1)
        self.assertEqual(result.skipped_symlinks[0], link)
        # Le fichier cible n'a jamais ete touche.
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(), "contenu externe")

    def test_undo_inter_volume_uses_copy_function_and_leaves_no_truncated_file(self):
        # Regression finding eleve organizer.py:1545 : _attempt_restore_entry
        # utilisait shutil.move() nu, sans le copy_function anti-troncature
        # qu'utilise execute() - une annulation inter-volume interrompue en
        # cours de copie aurait pu laisser un fichier tronque a la source
        # d'origine.
        import unittest.mock as mock
        self._write("gros.pdf", "contenu original complet")
        o = self._organizer()
        result = o.plan()
        o.execute(result, simulate=False)

        def failing_rename(src, dst, *args, **kwargs):
            raise OSError("simulation : renommage impossible entre deux volumes distincts")

        with mock.patch.object(org.os, "rename", side_effect=failing_rename):
            out = o.undo_last_batch()

        self.assertEqual(len(out["undone"]), 1)
        restored = self.downloads / "gros.pdf"
        self.assertTrue(restored.exists())
        self.assertEqual(restored.read_text(), "contenu original complet")
        leftovers = list(restored.parent.glob("*.partial*"))
        self.assertEqual(leftovers, [])

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

    def test_export_html_report_reflects_real_status_of_a_simulated_batch(self):
        # Un lot simule ne doit jamais afficher "Deplace" (statut reel) :
        # le libelle doit refleter le vrai statut de chaque entree,
        # y compris pour un lot re-exporte depuis le detail de l'historique.
        self._write("a.pdf")
        o = self._organizer()
        batch = o.execute(o.plan(), simulate=True)
        report_path = self.tmp / "rapport_simule.html"
        org.export_html_report(batch, report_path)
        content = report_path.read_text(encoding="utf-8")
        self.assertIn("Simule", content)
        self.assertNotIn("<td>Deplace</td>", content)

    def test_export_html_report_reflects_real_status_of_an_undone_batch(self):
        self._write("a.pdf", "contenu")
        o = self._organizer()
        o.execute(o.plan(), simulate=False)
        o.undo_last_batch()
        history = org.load_history()
        report_path = self.tmp / "rapport_annule.html"
        org.export_html_report(history[0], report_path)
        content = report_path.read_text(encoding="utf-8")
        self.assertIn("Annule (undo)", content)

    def test_export_html_report_tolerates_a_move_entry_missing_the_source_field(self):
        # Regression trouvee a l'audit : un acces par crochets (m["source"])
        # faisait planter tout l'export d'un lot malforme/edite a la main
        # pour une seule entree incomplete, au lieu de degrader proprement
        # comme le fait deja _attempt_restore_entry pour ce meme genre
        # d'entree (voir test_undo_with_malformed_history_entry_does_not_crash).
        batch = {
            "timestamp": "2026-01-01 12:00:00",
            "simulated": False,
            "moves": [{"category": "PDF", "reason": "extension .pdf", "status": "deplace"}],
        }
        report_path = self.tmp / "rapport_source_manquant.html"
        org.export_html_report(batch, report_path)  # ne doit pas lever
        self.assertTrue(report_path.exists())

    # -- rappel de nettoyage pour dossiers anciens ------------------------

    def _age_file(self, path: Path, days: float):
        old_time = time.time() - days * 86400
        os.utime(path, (old_time, old_time))

    def test_scan_stale_review_folders_reports_old_files_in_a_verifier(self):
        old_verifier = self.target / org.OLD_FILES_TARGET
        old_verifier.mkdir(parents=True)
        stale = old_verifier / "vieux.bin"
        stale.write_text("x" * 2048)
        self._age_file(stale, 200)

        o = self._organizer()
        result = o.scan_stale_review_folders()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["folder"], org.OLD_FILES_TARGET)
        self.assertEqual(result[0]["count"], 1)
        self.assertEqual(result[0]["total_bytes"], 2048)

    def test_scan_stale_review_folders_reports_old_files_in_doublons(self):
        doublons = self.target / org.DUPLICATES_TARGET
        doublons.mkdir(parents=True)
        stale = doublons / "copie.pdf"
        stale.write_text("x")
        self._age_file(stale, 400)

        o = self._organizer()
        result = o.scan_stale_review_folders()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["folder"], org.DUPLICATES_TARGET)

    def test_scan_stale_review_folders_ignores_recent_files(self):
        old_verifier = self.target / org.OLD_FILES_TARGET
        old_verifier.mkdir(parents=True)
        recent = old_verifier / "recent.bin"
        recent.write_text("x")
        # Pas de _age_file : mtime reste "maintenant", bien sous le seuil.

        o = self._organizer()
        result = o.scan_stale_review_folders()
        self.assertEqual(result, [])

    def test_scan_stale_review_folders_returns_empty_list_when_folders_absent(self):
        o = self._organizer()
        result = o.scan_stale_review_folders()
        self.assertEqual(result, [])

    def test_scan_stale_review_folders_respects_custom_threshold(self):
        old_verifier = self.target / org.OLD_FILES_TARGET
        old_verifier.mkdir(parents=True)
        stale = old_verifier / "vieux.bin"
        stale.write_text("x")
        self._age_file(stale, 10)

        o = self._organizer(old_file_threshold_days=5)
        result = o.scan_stale_review_folders()
        self.assertEqual(len(result), 1)

        o_high_threshold = self._organizer(old_file_threshold_days=365)
        self.assertEqual(o_high_threshold.scan_stale_review_folders(), [])

    def test_scan_stale_review_folders_sums_both_folders_independently(self):
        old_verifier = self.target / org.OLD_FILES_TARGET
        old_verifier.mkdir(parents=True)
        doublons = self.target / org.DUPLICATES_TARGET
        doublons.mkdir(parents=True)
        for name, folder in (("a.bin", old_verifier), ("b.bin", old_verifier), ("c.pdf", doublons)):
            f = folder / name
            f.write_text("x")
            self._age_file(f, 150)

        o = self._organizer()
        result = {entry["folder"]: entry["count"] for entry in o.scan_stale_review_folders()}
        self.assertEqual(result[org.OLD_FILES_TARGET], 2)
        self.assertEqual(result[org.DUPLICATES_TARGET], 1)

    # -- pre-filtre par hash partiel (optimisation doublons) ----------------

    def test_partial_hash_prefilter_does_not_change_duplicate_detection(self):
        # Plusieurs fichiers de MEME TAILLE mais de contenu different (le cas
        # qui declenchait un hash complet couteux pour chacun avant le
        # pre-filtre par hash partiel), plus une vraie paire de doublons de
        # meme taille egalement : le resultat doit rester exactement celui
        # d'une comparaison par hash complet, sans aucun faux positif ni
        # faux negatif introduit par le pre-filtre.
        same_size_content = [
            "capture-ecran-001", "capture-ecran-002", "capture-ecran-003",
            "capture-ecran-004", "capture-ecran-005",
        ]
        for index, content in enumerate(same_size_content):
            self._write(f"img{index}.png", content=content)
        # Vrai doublon, meme taille que les fichiers ci-dessus n'est pas
        # necessaire : on ajoute une paire de doublons de taille identique
        # entre eux (mais differente du groupe precedent) pour verifier que
        # la detection fonctionne toujours au sein d'un sous-groupe distinct.
        self._write("facture.pdf", content="facture-identique-xyz")
        self._write("facture (1).pdf", content="facture-identique-xyz")

        o = self._organizer()
        result = o.plan()
        categories = sorted(m.category for m in result.moves)
        # 5 images uniques + 1 PDF garde + 1 PDF doublon.
        self.assertEqual(categories.count("Doublons"), 1)
        by_name = {m.source.name: m for m in result.moves}
        self.assertEqual(by_name["facture.pdf"].category, "Doublons")
        self.assertEqual(by_name["facture (1).pdf"].category, "PDF")
        for index in range(len(same_size_content)):
            self.assertEqual(by_name[f"img{index}.png"].category, "Images")

    def test_partial_hash_matches_but_middle_of_file_differs_is_not_a_duplicate(self):
        # Meme taille, meme debut, meme fin, mais milieu different : le
        # hash partiel (debut+fin uniquement) concorderait a tort si on lui
        # faisait confiance seul - seul le hash SHA-256 complet doit
        # trancher, jamais le hash partiel.
        sample_size = org.PARTIAL_HASH_SAMPLE_SIZE
        head = b"A" * sample_size
        tail = b"B" * sample_size
        content_a = head + b"MILIEU-UN--" + tail
        content_b = head + b"MILIEU-DEUX" + tail
        self.assertEqual(len(content_a), len(content_b))
        (self.downloads / "a.pdf").write_bytes(content_a)
        (self.downloads / "b.pdf").write_bytes(content_b)

        o = self._organizer()
        result = o.plan()
        self.assertEqual(len(result.moves), 2)
        self.assertTrue(all(m.category == "PDF" for m in result.moves))

    def test_partial_hash_prefilter_reduces_full_hash_calls(self):
        # Verifie empiriquement que le pre-filtre reduit bien le nombre de
        # lectures completes (_file_hash) quand beaucoup de fichiers
        # partagent une taille mais pas un contenu.
        import unittest.mock as mock
        for index in range(30):
            self._write(f"doc{index}.pdf", content=f"contenu-unique-{index:03d}")
        o = self._organizer()
        original_file_hash = org.DownloadOrganizer._file_hash
        calls = []

        def counting_file_hash(path, cache=None):
            calls.append(path)
            return original_file_hash(path, cache)

        with mock.patch.object(org.DownloadOrganizer, "_file_hash", staticmethod(counting_file_hash)):
            o.plan()
        # Aucun de ces 30 fichiers de meme taille n'est un doublon : le
        # pre-filtre par hash partiel doit eliminer tous les candidats avant
        # tout hash complet.
        self.assertEqual(len(calls), 0)


# ---------------------------------------------------------------------------
# CLI : tolerance a l'encodage de la console (A12)
# ---------------------------------------------------------------------------

class CLIUnicodeEncodingTestCase(unittest.TestCase):
    """Verrouille la regression A12 : sur une console dont l'encodage de
    sortie ne peut pas representer un caractere (emoji, etc.) present dans un
    nom de fichier - le cas le plus courant etant cp1252/cp850, le defaut
    historique de cmd.exe sur Windows - le CLI ne doit plus jamais planter
    avec un UnicodeEncodeError brut. On simule cette console etroite en
    remplacant sys.stdout par un vrai io.TextIOWrapper configure en
    encoding="cp1252", errors="strict" (le comportement par defaut d'un
    print() non protege).

    N'herite PAS de OrganizerTestCase (contrairement a une premiere version
    de ce fichier) : le faire executerait une seconde fois, sous ce nouveau
    nom de classe, tous les tests deja herites de OrganizerTestCase - une
    isolation minimale dupliquee ici (meme pattern que HistoryJsonlTestCase
    plus bas) suffit et evite ce doublon."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
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

    def _narrow_stdout(self):
        buffer = io.BytesIO()
        return buffer, io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")

    def test_print_plan_raises_unicode_error_on_narrow_console_without_fix(self):
        # Documente le bug d'origine : un print() nu (sans le correctif
        # _ensure_console_encoding_tolerant) plante bel et bien des qu'un nom
        # de fichier contient un emoji et que la console ne peut pas
        # l'encoder - c'est exactement ce que _print_plan faisait avant
        # correctif, et ce qu'elle ferait de nouveau si le correctif etait
        # retire.
        emoji_path = self.downloads / "resume final \U0001F600 2024.pdf"
        move = org.PlannedMove(
            source=emoji_path, destination=self.target / "Documents" / "PDF" / emoji_path.name,
            category="PDF", reason="extension .pdf",
        )
        result = org.OrganizeResult(moves=[move])
        buffer, narrow_stdout = self._narrow_stdout()
        old_stdout = sys.stdout
        sys.stdout = narrow_stdout
        try:
            with self.assertRaises(UnicodeEncodeError):
                org._print_plan(result)
        finally:
            sys.stdout = old_stdout
            narrow_stdout.close()

    def test_print_plan_does_not_crash_on_narrow_console_after_fix(self):
        emoji_path = self.downloads / "resume final \U0001F600 2024.pdf"
        move = org.PlannedMove(
            source=emoji_path, destination=self.target / "Documents" / "PDF" / emoji_path.name,
            category="PDF", reason="extension .pdf",
        )
        result = org.OrganizeResult(moves=[move])
        buffer, narrow_stdout = self._narrow_stdout()
        old_stdout = sys.stdout
        sys.stdout = narrow_stdout
        try:
            org._ensure_console_encoding_tolerant()
            org._print_plan(result)  # ne doit lever aucune exception
            narrow_stdout.flush()
            # Lu AVANT la fermeture du TextIOWrapper : fermer narrow_stdout
            # ferme aussi le BytesIO sous-jacent (buffer.getvalue() leverait
            # "I/O operation on closed file" apres coup).
            output = buffer.getvalue().decode("cp1252", errors="replace")
        finally:
            sys.stdout = old_stdout
            narrow_stdout.close()
        # Le nom de fichier reste identifiable (repli \uXXXX lisible) plutot
        # que de faire disparaitre completement l'information.
        self.assertIn("resume final", output)
        self.assertIn("2024.pdf", output)

    def test_ensure_console_encoding_tolerant_is_a_no_op_without_reconfigure(self):
        # Un flux mocke/redirige sans .reconfigure() (certains environnements
        # de test/CI non interactifs) ne doit jamais faire planter l'appel -
        # le correctif se contente de ne rien faire dans ce cas.
        class _StreamWithoutReconfigure:
            pass

        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = _StreamWithoutReconfigure()
        sys.stderr = _StreamWithoutReconfigure()
        try:
            org._ensure_console_encoding_tolerant()  # ne doit lever aucune exception
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def test_main_cli_simulation_does_not_crash_on_emoji_filename(self):
        # Reproduction bout-en-bout du crash CLI original (A12) : "python
        # organizer.py" en mode simulation, sur un dossier contenant un
        # fichier dont le nom contient un emoji, avec une console dont
        # l'encodage de sortie ne supporte pas les emojis. Avant correctif,
        # ceci levait un UnicodeEncodeError non intercepte et un code de
        # sortie 1 sans qu'aucun plan ne soit affiche.
        self._write("resume final \U0001F600 2024.pdf", "contenu")
        org.save_config(self.config)  # downloads_dir/base_target_dir de test (voir setUp)
        buffer, narrow_stdout = self._narrow_stdout()
        old_stdout = sys.stdout
        sys.stdout = narrow_stdout
        try:
            exit_code = org.main([])
            narrow_stdout.flush()
            output = buffer.getvalue().decode("cp1252", errors="replace")
        finally:
            sys.stdout = old_stdout
            narrow_stdout.close()
        self.assertEqual(exit_code, 0)
        self.assertIn("resume final", output)
        # Le fichier ne doit pas avoir ete deplace (mode simulation par defaut).
        self.assertTrue((self.downloads / "resume final \U0001F600 2024.pdf").exists())


# ---------------------------------------------------------------------------
# Historique au format JSONL (append-only) et migration depuis l'ancien
# format history.json
# ---------------------------------------------------------------------------

class HistoryJsonlTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self._orig_app_dir = org.APP_DIR
        self._orig_history_file = org.HISTORY_FILE
        self._orig_cache = dict(org._history_count_cache)
        org.APP_DIR = self.tmp / "appdata"
        org.HISTORY_FILE = org.APP_DIR / "history.json"
        org._history_count_cache = {"path": None, "count": None}
        for handler in list(org.logger.handlers):
            handler.close()
            org.logger.removeHandler(handler)

    def _cleanup(self):
        for handler in list(org.logger.handlers):
            handler.close()
            org.logger.removeHandler(handler)
        org.APP_DIR = self._orig_app_dir
        org.HISTORY_FILE = self._orig_history_file
        org._history_count_cache = self._orig_cache

    def test_append_writes_jsonl_file_not_legacy_json(self):
        org.append_batch_to_history({"index": 1})
        jsonl_path = org._history_jsonl_path()
        self.assertTrue(jsonl_path.exists())
        self.assertFalse(org.HISTORY_FILE.exists())
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["index"], 1)

    def test_append_is_a_real_append_not_a_full_rewrite(self):
        # Ajoute plusieurs lots et verifie que chacun se retrouve, dans
        # l'ordre, comme une ligne independante du fichier JSONL.
        for i in range(10):
            org.append_batch_to_history({"index": i})
        jsonl_path = org._history_jsonl_path()
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual([json.loads(l)["index"] for l in lines], list(range(10)))
        self.assertEqual([b["index"] for b in org.load_history()], list(range(10)))

    def test_append_batch_to_history_returns_offset_usable_for_in_place_update(self):
        # append_batch_to_history() renvoie desormais (offset, longueur) en
        # octets de la ligne fraichement ecrite, condition necessaire pour
        # que execute() puisse checkpointer la progression d'un lot reel en
        # reecrivant cette meme ligne EN PLACE au fil des deplacements
        # (correctif audit A1), plutot que d'attendre la fin de la boucle.
        offset, length = org.append_batch_to_history({"index": 0, "moves": []})
        self.assertEqual(offset, 0)
        jsonl_path = org._history_jsonl_path()
        self.assertEqual(length, jsonl_path.stat().st_size)

        new_length = org.update_batch_checkpoint(offset, length, {"index": 0, "moves": ["a"]})
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["moves"], ["a"])
        self.assertEqual(new_length, jsonl_path.stat().st_size)

    def test_update_batch_checkpoint_never_touches_previous_lines(self):
        # La mise a jour en place d'un checkpoint ne doit reecrire QUE la
        # ligne visee (par offset), jamais relire ni retoucher les lots
        # precedents deja presents dans le fichier - sinon le cout de
        # checkpointer un gros lot reintroduirait le cout quadratique que
        # l'optimisation JSONL append-only (commit 7834cb5) avait elimine.
        org.append_batch_to_history({"index": 0, "moves": ["intact"]})
        offset, length = org.append_batch_to_history({"index": 1, "moves": []})
        org.update_batch_checkpoint(offset, length, {"index": 1, "moves": ["x", "y", "z"]})

        history = org.load_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], {"index": 0, "moves": ["intact"]})
        self.assertEqual(history[1]["moves"], ["x", "y", "z"])

    def test_update_batch_checkpoint_shrinking_content_truncates_correctly(self):
        # Le statut d'une entree peut se resumer au fil du lot (ex :
        # "erreur" -> "deplace", chaines de longueurs differentes) : la
        # ligne mise a jour doit rester un JSON valide meme quand elle
        # devient plus COURTE que la version precedente (troncature du
        # fichier au bon endroit, pas de garbage residuel en fin de ligne).
        offset, length = org.append_batch_to_history(
            {"index": 0, "moves": ["une-tres-longue-entree-initiale-de-test"]}
        )
        new_length = org.update_batch_checkpoint(offset, length, {"index": 0, "moves": []})
        self.assertLess(new_length, length)

        jsonl_path = org._history_jsonl_path()
        self.assertEqual(jsonl_path.stat().st_size, offset + new_length)
        history = org.load_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["moves"], [])

    def test_migration_from_legacy_json_array_preserves_all_batches(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        legacy_data = [{"index": i, "moves": []} for i in range(5)]
        org.HISTORY_FILE.write_text(json.dumps(legacy_data), encoding="utf-8")

        history = org.load_history()
        self.assertEqual([b["index"] for b in history], [0, 1, 2, 3, 4])
        # Le fichier legacy est converti puis retire, le nouveau fichier
        # JSONL devient la seule source de verite.
        self.assertFalse(org.HISTORY_FILE.exists())
        self.assertTrue(org._history_jsonl_path().exists())

    def test_migration_then_append_keeps_old_and_new_batches(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        legacy_data = [{"index": 0}, {"index": 1}]
        org.HISTORY_FILE.write_text(json.dumps(legacy_data), encoding="utf-8")

        org.append_batch_to_history({"index": 2})
        history = org.load_history()
        self.assertEqual([b["index"] for b in history], [0, 1, 2])

    def test_corrupted_legacy_json_is_quarantined_not_lost_silently(self):
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.HISTORY_FILE.write_text("pas du JSON du tout", encoding="utf-8")
        self.assertEqual(org.load_history(), [])
        # Le fichier corrompu original est conserve sous un autre nom
        # (quarantaine), jamais supprime silencieusement.
        backups = list(org.APP_DIR.glob("*.corrompu-*.bak"))
        self.assertEqual(len(backups), 1)

    def test_corrupted_single_jsonl_line_only_loses_that_batch(self):
        jsonl_path = org._history_jsonl_path()
        org.append_batch_to_history({"index": 0})
        org.append_batch_to_history({"index": 1})
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write("{ceci n'est pas du JSON valide}\n")
        org.append_batch_to_history({"index": 2})

        history = org.load_history()
        self.assertEqual([b["index"] for b in history], [0, 1, 2])

    def test_save_history_full_rewrite_stays_compatible_with_load_and_purge(self):
        for i in range(4):
            org.append_batch_to_history({"index": i})
        history = org.load_history()
        del history[1]
        org.save_history(history)
        self.assertEqual([b["index"] for b in org.load_history()], [0, 2, 3])

        removed = org.purge_history(keep_last=1)
        self.assertEqual(removed, 2)
        self.assertEqual([b["index"] for b in org.load_history()], [3])

    def test_append_batch_to_history_auto_truncates_at_max_batches_jsonl(self):
        original_max = org.MAX_HISTORY_BATCHES
        org.MAX_HISTORY_BATCHES = 3
        try:
            for i in range(5):
                org.append_batch_to_history({"index": i})
            history = org.load_history()
            self.assertEqual(len(history), 3)
            self.assertEqual([b["index"] for b in history], [2, 3, 4])
        finally:
            org.MAX_HISTORY_BATCHES = original_max

    def test_count_cache_stays_correct_across_migration_purge_and_append(self):
        # Sequence realiste : migration, ajouts, purge, puis nouveaux ajouts
        # - le cache de comptage utilise par append_batch_to_history ne doit
        # jamais deriver de la realite du fichier sur disque.
        org.APP_DIR.mkdir(parents=True, exist_ok=True)
        org.HISTORY_FILE.write_text(json.dumps([{"index": 0}]), encoding="utf-8")
        org.append_batch_to_history({"index": 1})  # declenche la migration
        org.purge_history(keep_last=1)
        org.append_batch_to_history({"index": 2})
        org.append_batch_to_history({"index": 3})
        history = org.load_history()
        self.assertEqual([b["index"] for b in history], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()

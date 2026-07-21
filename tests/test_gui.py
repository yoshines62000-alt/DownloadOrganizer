"""Tests de regression pour gui.py (interface graphique Tkinter).

Ces tests pilotent une VRAIE fenetre OrganizerGUI (vrai Tk, vrais widgets) :
seuls tkinter.messagebox/filedialog/simpledialog sont mockes quand
necessaire, jamais la logique metier elle-meme - meme convention que le
reste de cette suite de projets.
"""

import copy
import os
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import organizer as org
import gui


class GuiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)

        # Isole completement les tests du vrai ~/.download_organizer de
        # l'utilisateur (meme technique que tests/test_organizer.py).
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

        cfg = copy.deepcopy(org.DEFAULT_CONFIG)
        cfg["downloads_dir"] = str(self.downloads)
        cfg["base_target_dir"] = str(self.target)
        org.save_config(cfg)

        self.app = gui.OrganizerGUI()
        self.addCleanup(self._destroy_app)
        # __init__ programme deja une verification automatique des dossiers
        # "A verifier"/"Doublons" 200ms apres le demarrage (voir
        # self._stale_review_after_id) : on l'annule ici pour que les tests
        # qui appellent _check_stale_review_folders() explicitement ne
        # s'executent pas en double avec cet appel automatique.
        if self.app._stale_review_after_id is not None:
            self.app.after_cancel(self.app._stale_review_after_id)
            self.app._stale_review_after_id = None

    def _destroy_app(self):
        try:
            # Meme chemin de fermeture que le vrai bouton/croix de fenetre :
            # annule proprement le polling du scan de demarrage s'il est
            # encore en cours, plutot que de detruire la fenetre brutalement.
            self.app._on_close()
        except Exception:
            pass

    def _cleanup(self):
        for handler in list(org.logger.handlers):
            handler.close()
            org.logger.removeHandler(handler)
        org.APP_DIR = self._orig_app_dir
        org.HISTORY_FILE = self._orig_history_file
        org.CONFIG_FILE = self._orig_config_file
        org.LOG_FILE = self._orig_log_file

    def _age_file(self, path: Path, days: float):
        old_time = time.time() - days * 86400
        os.utime(path, (old_time, old_time))

    # -- item 2 : l'intervalle de veille est bien collecte/persiste --------

    def test_collect_config_includes_watch_interval(self):
        self.app.watch_interval_var.set("45")
        config = self.app._collect_config()
        self.assertIsNotNone(config)
        self.assertEqual(config["watch_interval_seconds"], 45)

    def test_save_config_without_activating_watch_persists_new_interval(self):
        # Regression trouvee a l'audit : modifier l'intervalle de veille puis
        # cliquer "Enregistrer la configuration" (Ctrl+S) SANS jamais cliquer
        # "Activer la veille" perdait silencieusement la nouvelle valeur,
        # puisque seul _start_watch l'ecrivait auparavant dans config_data.
        self.app.watch_interval_var.set("77")
        with mock.patch.object(gui, "messagebox") as mocked_messagebox:
            self.app._save_config()
            mocked_messagebox.showerror.assert_not_called()

        reloaded = org.load_config()
        self.assertEqual(reloaded["watch_interval_seconds"], 77)

    def test_collect_config_rejects_invalid_watch_interval(self):
        self.app.watch_interval_var.set("2")  # sous le minimum de 5 secondes
        with mock.patch.object(gui, "messagebox") as mocked_messagebox:
            config = self.app._collect_config()
            self.assertIsNone(config)
            mocked_messagebox.showerror.assert_called_once()

    # -- item 4 : le scan des dossiers "A verifier"/"Doublons" ne bloque --
    # -- jamais le thread principal Tk (thread + queue.Queue + after) -----

    def test_check_stale_review_folders_does_not_block_the_ui_thread(self):
        old_verifier = self.target / org.OLD_FILES_TARGET
        old_verifier.mkdir(parents=True)
        stale_file = old_verifier / "vieux.bin"
        stale_file.write_text("x" * 2048)
        self._age_file(stale_file, 200)

        original_scan = org.DownloadOrganizer.scan_stale_review_folders

        def slow_scan(self_org, *args, **kwargs):
            time.sleep(0.5)
            return original_scan(self_org, *args, **kwargs)

        shown = {}

        def fake_showinfo(title, message, **kwargs):
            shown["title"] = title
            shown["message"] = message

        with mock.patch.object(org.DownloadOrganizer, "scan_stale_review_folders", slow_scan), \
                mock.patch.object(gui.messagebox, "showinfo", side_effect=fake_showinfo):
            start = time.perf_counter()
            self.app._check_stale_review_folders()
            elapsed = time.perf_counter() - start
            # Avant le correctif, cet appel executait le scan (0.5s de
            # sleep simule) de maniere synchrone sur le thread principal :
            # cette assertion aurait echoue (elapsed >= 0.5).
            self.assertLess(elapsed, 0.2)

            # Laisse le thread de fond se terminer et le polling (self.after)
            # traiter le resultat via la vraie boucle d'evenements Tk.
            deadline = time.time() + 5
            while "title" not in shown and time.time() < deadline:
                self.app.update()
                time.sleep(0.02)

        self.assertIn("title", shown)
        self.assertIn("A verifier", shown["message"])
        self.assertIn("1 fichier(s)", shown["message"])

    def test_check_stale_review_folders_reports_nothing_when_folders_are_empty(self):
        shown = {"called": False}

        def fake_showinfo(*args, **kwargs):
            shown["called"] = True

        with mock.patch.object(gui.messagebox, "showinfo", side_effect=fake_showinfo):
            self.app._check_stale_review_folders()
            deadline = time.time() + 1.5
            while time.time() < deadline:
                self.app.update()
                time.sleep(0.02)

        self.assertFalse(shown["called"])

    def test_check_stale_review_folders_survives_scan_exception(self):
        # Le thread de fond ne doit jamais laisser le polling tourner
        # indefiniment si le scan echoue de maniere inattendue.
        def failing_scan(self_org, *args, **kwargs):
            raise RuntimeError("panne simulee")

        with mock.patch.object(org.DownloadOrganizer, "scan_stale_review_folders", failing_scan), \
                mock.patch.object(gui.messagebox, "showinfo") as mocked_showinfo:
            self.app._check_stale_review_folders()
            deadline = time.time() + 1.5
            while time.time() < deadline:
                self.app.update()
                time.sleep(0.02)

        mocked_showinfo.assert_not_called()

    # -- Phase 2 : plan()/execute() ne bloquent plus le thread principal Tk -
    # -- (Simuler, Ranger les fichiers, Mode Veille), meme pattern (thread +
    # -- queue.Queue + after()) que le scan des dossiers "A verifier" ------

    def _make_slow_plan(self, delay: float = 0.5):
        """Remplace DownloadOrganizer.plan par une version qui dort `delay`
        secondes avant de deleguer au vrai plan() - simule le gel de ~20s
        mesure a l'audit sur un gros volume de fichiers, sans avoir a
        generer reellement des milliers de fichiers dans chaque test."""
        original_plan = org.DownloadOrganizer.plan

        def slow_plan(self_org, *args, **kwargs):
            time.sleep(delay)
            return original_plan(self_org, *args, **kwargs)

        return slow_plan

    def _pump_until(self, predicate, timeout: float = 5.0):
        deadline = time.time() + timeout
        while not predicate() and time.time() < deadline:
            self.app.update()
            time.sleep(0.02)
        return predicate()

    def test_simulate_does_not_block_the_ui_thread_and_disables_buttons_while_running(self):
        (self.downloads / "photo.jpg").write_bytes(b"x" * 100)

        with mock.patch.object(org.DownloadOrganizer, "plan", self._make_slow_plan(0.5)), \
                mock.patch.object(gui, "messagebox") as mocked_messagebox:
            start = time.perf_counter()
            self.app._simulate()
            elapsed = time.perf_counter() - start
            # Avant le correctif, _simulate() executait plan() de maniere
            # synchrone sur le thread principal : cette assertion aurait
            # echoue (elapsed >= 0.5).
            self.assertLess(elapsed, 0.2)

            # Les boutons declencheurs sont desactives pendant le traitement,
            # pour eviter un double-declenchement (F5/clic pendant que le
            # premier plan() tourne encore sur son thread).
            self.assertEqual(str(self.app.simulate_btn.cget("state")), "disabled")
            self.assertEqual(str(self.app.run_btn.cget("state")), "disabled")
            self.assertTrue(self.app._busy)

            finished = self._pump_until(lambda: not self.app._busy)
            self.assertTrue(finished, "la simulation ne s'est jamais terminee")
            mocked_messagebox.showerror.assert_not_called()

        self.assertEqual(str(self.app.simulate_btn.cget("state")), "normal")
        self.assertEqual(str(self.app.run_btn.cget("state")), "normal")
        self.assertEqual(len(self.app.preview_tree.get_children()), 1)
        self.assertIn("Simulation", self.app.status_var.get())
        # Aucun fichier reellement deplace par une simulation.
        self.assertTrue((self.downloads / "photo.jpg").exists())

    def test_simulate_ignores_a_second_trigger_while_already_running(self):
        (self.downloads / "photo.jpg").write_bytes(b"x" * 100)
        call_count = {"n": 0}
        original_plan = org.DownloadOrganizer.plan

        def counting_slow_plan(self_org, *args, **kwargs):
            call_count["n"] += 1
            time.sleep(0.4)
            return original_plan(self_org, *args, **kwargs)

        with mock.patch.object(org.DownloadOrganizer, "plan", counting_slow_plan), \
                mock.patch.object(gui, "messagebox"):
            self.app._simulate()
            # Simule un second declenchement (double-clic, ou raccourci F5)
            # pendant que le premier plan() tourne encore : ne doit jamais
            # lancer un second traitement en parallele.
            self.app._simulate()
            self.assertTrue(self._pump_until(lambda: not self.app._busy))

        self.assertEqual(call_count["n"], 1)

    def test_run_real_does_not_block_the_ui_thread_and_moves_files_via_background_thread(self):
        (self.downloads / "photo.jpg").write_bytes(b"x" * 100)

        with mock.patch.object(org.DownloadOrganizer, "plan", self._make_slow_plan(0.5)), \
                mock.patch.object(gui, "messagebox") as mocked_messagebox:
            mocked_messagebox.askyesno.return_value = True
            start = time.perf_counter()
            self.app._run_real()
            elapsed = time.perf_counter() - start
            # Avant le correctif, _run_real() executait plan() (et execute())
            # de maniere synchrone sur le thread principal.
            self.assertLess(elapsed, 0.2)
            self.assertEqual(str(self.app.run_btn.cget("state")), "disabled")
            self.assertEqual(str(self.app.simulate_btn.cget("state")), "disabled")

            finished = self._pump_until(lambda: not self.app._busy)
            self.assertTrue(finished, "le rangement reel ne s'est jamais termine")
            mocked_messagebox.showerror.assert_not_called()

        self.assertEqual(str(self.app.run_btn.cget("state")), "normal")
        self.assertEqual(str(self.app.simulate_btn.cget("state")), "normal")
        self.assertFalse((self.downloads / "photo.jpg").exists())
        self.assertIsNotNone(self.app.last_real_batch)
        self.assertIn("1 fichier(s) deplace", self.app.status_var.get())
        # self.organizer n'est reaffecte qu'apres la fin complete du
        # traitement, depuis le thread principal.
        self.assertEqual(self.app.organizer.config["downloads_dir"], str(self.downloads))

    def test_watch_tick_does_not_block_the_ui_thread_nor_disable_manual_buttons(self):
        with mock.patch.object(org.DownloadOrganizer, "plan", self._make_slow_plan(0.5)), \
                mock.patch.object(gui, "messagebox"):
            self.app.watch_active = True
            self.app._watch_last_signature = None
            start = time.perf_counter()
            self.app._watch_tick()
            elapsed = time.perf_counter() - start
            # Avant le correctif, _watch_tick() executait plan() de maniere
            # synchrone sur le thread principal, y compris en Mode Veille -
            # cense pourtant etre une tache de fond non intrusive.
            self.assertLess(elapsed, 0.2)

            # Contrairement a Simuler/Ranger, un cycle de veille ne doit
            # jamais desactiver les boutons manuels : la veille reste une
            # tache de fond non intrusive.
            self.assertEqual(str(self.app.simulate_btn.cget("state")), "normal")
            self.assertEqual(str(self.app.run_btn.cget("state")), "normal")
            self.assertFalse(self.app._busy)
            self.assertTrue(self.app._watch_busy)

            finished = self._pump_until(lambda: not self.app._watch_busy)
            self.assertTrue(finished, "le cycle de veille ne s'est jamais termine")

        self.app.watch_active = False

    def test_watch_tick_skips_a_cycle_while_a_manual_action_is_running(self):
        call_count = {"n": 0}
        original_plan = org.DownloadOrganizer.plan

        def counting_plan(self_org, *args, **kwargs):
            call_count["n"] += 1
            return original_plan(self_org, *args, **kwargs)

        with mock.patch.object(org.DownloadOrganizer, "plan", counting_plan):
            self.app._busy = True
            try:
                self.app.watch_active = True
                self.app._watch_tick()
                # Doit se reprogrammer sans jamais appeler plan() tant qu'une
                # action manuelle est en cours.
                self.assertFalse(self.app._watch_busy)
                self.assertEqual(call_count["n"], 0)
                self.assertIsNotNone(self.app._watch_after_id)
            finally:
                if self.app._watch_after_id is not None:
                    self.app.after_cancel(self.app._watch_after_id)
                    self.app._watch_after_id = None
                self.app.watch_active = False
                self.app._busy = False

    # -- fenetre par defaut : contenu essentiel visible sans redimensionner -

    def test_default_window_shows_action_buttons_tab_and_status_bar(self):
        # Regression trouvee a l'audit "Phase 1" : au premier lancement, la
        # fenetre s'ouvrait en 880x600 (minsize 760x520) alors que le contenu
        # reclame ~1123x1100px reels (winfo_reqwidth()/winfo_reqheight()) -
        # aucun bouton d'action, aucun onglet Apercu/Historique, ni la barre
        # de statut n'etaient visibles, seuls les champs de configuration du
        # haut l'etaient. Ce test pilote la VRAIE fenetre Tkinter, avec sa
        # taille par defaut reelle (aucune geometry() forcee ici), et mesure
        # les positions/tailles reelles des widgets essentiels pour verifier
        # qu'ils rentrent bien dans la fenetre telle qu'ouverte.
        self.app.update()
        win_w = self.app.winfo_width()
        win_h = self.app.winfo_height()

        # Un bouton d'action cle (Ranger les fichiers) doit etre visible.
        run_btn = self.app.run_btn
        self.assertTrue(run_btn.winfo_ismapped())
        run_btn_bottom = (run_btn.winfo_rooty() - self.app.winfo_rooty()) + run_btn.winfo_height()
        self.assertGreater(run_btn.winfo_height(), 5, "le bouton 'Ranger les fichiers' est ecrase (hauteur quasi nulle)")
        self.assertLessEqual(run_btn_bottom, win_h, "le bouton 'Ranger les fichiers' deborde de la fenetre par defaut")

        # Le notebook (onglets Apercu/Historique) doit avoir une hauteur
        # utile reelle, pas juste sa barre d'onglets ecrasee a quelques px.
        from tkinter import ttk
        notebooks = [c for c in self.app.winfo_children() if isinstance(c, ttk.Notebook)]
        self.assertEqual(len(notebooks), 1)
        notebook = notebooks[0]
        self.assertTrue(notebook.winfo_ismapped())
        notebook_top = notebook.winfo_rooty() - self.app.winfo_rooty()
        notebook_bottom = notebook_top + notebook.winfo_height()
        self.assertLessEqual(notebook_top, win_h, "l'onglet Apercu/Historique n'est pas visible")
        self.assertGreater(notebook.winfo_height(), 60, "l'onglet visible est ecrase a une hauteur inutilisable")
        self.assertLessEqual(notebook_bottom, win_h)

        # La barre de statut (bas de fenetre) doit etre visible avec une
        # hauteur reelle, pas ecrasee a 1px par le notebook packe avant elle
        # (autre regression trouvee a l'audit : ordre de pack() incorrect).
        status_label = None
        for frame in self.app.winfo_children():
            if not isinstance(frame, ttk.Frame) or isinstance(frame, ttk.LabelFrame):
                continue
            for child in frame.winfo_children():
                if isinstance(child, ttk.Label) and child.cget("textvariable") == str(self.app.status_var):
                    status_label = child
                    break
        self.assertIsNotNone(status_label, "impossible de retrouver le label de la barre de statut")
        self.assertTrue(status_label.winfo_ismapped())
        self.assertGreater(status_label.winfo_height(), 5, "la barre de statut est ecrasee (hauteur quasi nulle)")
        status_bottom = (status_label.winfo_rooty() - self.app.winfo_rooty()) + status_label.winfo_height()
        self.assertLessEqual(status_bottom, win_h, "la barre de statut deborde de la fenetre par defaut")


if __name__ == "__main__":
    unittest.main()

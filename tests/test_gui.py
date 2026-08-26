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
import opl_theme
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

    # -- audit F2 : opt-out de la verification de mise a jour au demarrage --

    def test_check_updates_enabled_by_default(self):
        self.assertTrue(self.app.check_updates_var.get())
        config = self.app._collect_config()
        self.assertTrue(config["check_for_updates"])

    def test_unchecking_check_updates_persists_the_opt_out(self):
        self.app.check_updates_var.set(False)
        with mock.patch.object(gui, "messagebox") as mocked_messagebox:
            self.app._save_config()
            mocked_messagebox.showerror.assert_not_called()

        reloaded = org.load_config()
        self.assertFalse(reloaded["check_for_updates"])

    def test_start_update_check_is_skipped_when_disabled_in_config(self):
        # Reconstruit une fenetre a partir d'une config existante ou l'opt-out
        # est deja enregistre - simule un redemarrage de l'application apres
        # avoir decoche la case au tour precedent.
        cfg = org.load_config()
        cfg["check_for_updates"] = False
        org.save_config(cfg)

        with mock.patch.object(gui.update_checker, "start_update_check") as mocked_start:
            app2 = gui.OrganizerGUI()
            try:
                mocked_start.assert_not_called()
                self.assertIn("desactivee", app2.update_status_var.get().lower())
            finally:
                app2._on_close()

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

    # -- correctif audit A14 : un declenchement manuel (Simuler / Ranger les
    # -- fichiers) doit etre refuse tant qu'un cycle de Mode Veille est en
    # -- cours (_watch_busy), symetriquement a ce que _watch_tick fait deja
    # -- vis-a-vis d'une action manuelle en cours (_busy) ci-dessus.

    def test_simulate_is_deferred_while_a_watch_cycle_is_busy(self):
        (self.downloads / "photo.jpg").write_bytes(b"x" * 100)
        call_count = {"n": 0}
        original_plan = org.DownloadOrganizer.plan

        def counting_plan(self_org, *args, **kwargs):
            call_count["n"] += 1
            return original_plan(self_org, *args, **kwargs)

        with mock.patch.object(org.DownloadOrganizer, "plan", counting_plan):
            self.app._watch_busy = True
            try:
                self.app._simulate()
                # Ne doit jamais demarrer de second plan() tant qu'un cycle
                # de veille est en cours, et ne doit pas non plus faire
                # progresser _busy/desactiver les boutons (un simple retour
                # refuse, ce n'est pas un traitement qui demarre).
                self.assertEqual(call_count["n"], 0)
                self.assertFalse(self.app._busy)
                self.assertEqual(str(self.app.simulate_btn.cget("state")), "normal")
                self.assertIn("Mode Veille", self.app.status_var.get())
            finally:
                self.app._watch_busy = False

        # Une fois le cycle de veille termine, le meme appel fonctionne a nouveau.
        with mock.patch.object(gui, "messagebox") as mocked_messagebox:
            self.app._simulate()
            self.assertTrue(self._pump_until(lambda: not self.app._busy))
            mocked_messagebox.showerror.assert_not_called()
        self.assertEqual(len(self.app.preview_tree.get_children()), 1)

    def test_run_real_is_deferred_while_a_watch_cycle_is_busy(self):
        (self.downloads / "photo.jpg").write_bytes(b"x" * 100)
        call_count = {"n": 0}
        original_plan = org.DownloadOrganizer.plan

        def counting_plan(self_org, *args, **kwargs):
            call_count["n"] += 1
            return original_plan(self_org, *args, **kwargs)

        with mock.patch.object(org.DownloadOrganizer, "plan", counting_plan):
            self.app._watch_busy = True
            try:
                self.app._run_real()
                self.assertEqual(call_count["n"], 0)
                self.assertFalse(self.app._busy)
                self.assertEqual(str(self.app.run_btn.cget("state")), "normal")
                self.assertIn("Mode Veille", self.app.status_var.get())
            finally:
                self.app._watch_busy = False

        # Le fichier n'a pas ete touche par la tentative refusee.
        self.assertTrue((self.downloads / "photo.jpg").exists())

    def test_manual_run_does_not_double_move_a_file_while_watch_batch_is_executing(self):
        # Reproduction bout-en-bout du risque decrit par l'audit A14 : sans
        # le correctif, rien n'empechait un "Ranger les fichiers" manuel de
        # demarrer un second plan()/execute() en parallele pendant qu'un lot
        # de Mode Veille deja confirme par l'utilisateur (_watch_busy=True,
        # positionne par _prompt_watch_batch) est en train de deplacer
        # physiquement les memes fichiers - avec un risque de traitement en
        # double du meme fichier par les deux threads. Ce test verrouille
        # qu'avec le correctif, la tentative manuelle est refusee et qu'un
        # seul deplacement reel a bien lieu.
        (self.downloads / "photo.jpg").write_bytes(b"x" * 100)

        worker_organizer = org.DownloadOrganizer(copy.deepcopy(self.app.organizer.config))
        result = worker_organizer.plan()
        self.assertEqual(len(result.moves), 1)

        original_execute = org.DownloadOrganizer.execute
        execute_calls = {"n": 0}

        def slow_execute(self_org, *args, **kwargs):
            execute_calls["n"] += 1
            time.sleep(0.4)
            return original_execute(self_org, *args, **kwargs)

        original_plan = org.DownloadOrganizer.plan
        plan_calls = {"n": 0}

        def counting_plan(self_org, *args, **kwargs):
            plan_calls["n"] += 1
            return original_plan(self_org, *args, **kwargs)

        with mock.patch.object(org.DownloadOrganizer, "execute", slow_execute), \
                mock.patch.object(org.DownloadOrganizer, "plan", counting_plan), \
                mock.patch.object(gui, "messagebox") as mocked_messagebox:
            mocked_messagebox.askyesno.return_value = True

            # Comme le ferait _watch_tick apres confirmation de l'utilisateur :
            # positionne _watch_busy et demarre execute() sur un thread de fond.
            self.app.watch_active = True
            self.app._prompt_watch_batch(worker_organizer, result)
            self.assertTrue(self.app._watch_busy)

            # Tentative de declenchement manuel PENDANT que ce lot de veille
            # est encore en train de s'executer (execute() dort encore 0.4s).
            self.app._run_real()
            self.assertFalse(self.app._busy)
            self.assertIn("Mode Veille", self.app.status_var.get())
            self.assertEqual(plan_calls["n"], 0)

            finished = self._pump_until(lambda: not self.app._watch_busy)
            self.assertTrue(finished, "le lot de veille ne s'est jamais termine")

            self.app.watch_active = False

        # execute() n'a ete appele qu'une seule fois (par le Mode Veille) :
        # aucun second plan()/execute() manuel n'a demarre en parallele.
        self.assertEqual(execute_calls["n"], 1)
        self.assertEqual(plan_calls["n"], 0)
        # Le fichier n'a ete deplace qu'une seule fois, pas de double-traitement.
        self.assertFalse((self.downloads / "photo.jpg").exists())
        self.assertTrue((self.target / "Images" / "photo.jpg").exists())

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
        notebooks = [c for c in self.app.winfo_children() if isinstance(c, opl_theme.Rail)]
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

    # -- D1/D3 : priorite donnee a la zone de resultats (Apercu/Historique) -

    def test_default_window_apercu_table_has_generous_vertical_space(self):
        # Regression D1 (audit) : a la taille par defaut, les sections de
        # reglages (Exclusions personnalisees, Categories personnalisees,
        # Mode Veille) occupaient ~700px de hauteur au-dessus du Notebook,
        # ne laissant que 4-5 lignes visibles dans le tableau "Apercu" meme
        # apres une simulation en ayant planifie beaucoup plus. Ces sections
        # sont desormais regroupees dans leur propre onglet "Reglages
        # avances", ce qui doit liberer nettement plus d'espace vertical
        # pour l'apercu par defaut (onglet actif a l'ouverture).
        self.app.update()
        self.assertTrue(self.app.preview_tree.winfo_ismapped())
        self.assertGreater(
            self.app.preview_tree.winfo_height(), 300,
            "le tableau Apercu reste fortement comprime a la taille par defaut",
        )

        from tkinter import ttk
        notebooks = [c for c in self.app.winfo_children() if isinstance(c, opl_theme.Rail)]
        self.assertEqual(len(notebooks), 1)
        notebook = notebooks[0]
        tab_texts = [notebook.tab(tab_id, "text") for tab_id in notebook.tabs()]
        self.assertIn("Reglages avances", tab_texts)
        # L'apercu doit rester l'onglet actif par defaut a l'ouverture -
        # c'est l'ecran le plus important, il ne doit jamais etre relegue
        # derriere l'onglet de reglages.
        selected = notebook.select()
        self.assertEqual(notebook.tab(selected, "text"), "Apercu")

    def test_settings_widgets_still_reachable_from_reglages_avances_tab(self):
        # Les reglages deplaces dans leur propre onglet (D1) doivent rester
        # entierement fonctionnels : les memes attributs/widgets existent
        # toujours et pointent vers des donnees coherentes, seul leur parent
        # visuel a change.
        self.assertTrue(self.app.categories_tree.winfo_exists())
        self.assertTrue(self.app.watch_btn.winfo_exists())
        self.app.excl_ext_var.set(".bak, .old")
        config = self.app._collect_config()
        self.assertIsNotNone(config)
        self.assertEqual(config["exclusions"]["extensions"], [".bak", ".old"])

    # -- K3 : le Notebook Apercu/Historique est accessible en attribut ------

    def test_notebook_is_stored_as_an_instance_attribute(self):
        # Regression K3 (audit) : auparavant une simple variable locale de
        # _build_widgets, self.notebook doit pointer vers le VRAI Notebook
        # de la fenetre (celui qui contient les onglets Apercu/Historique/
        # Reglages avances), accessible depuis l'exterieur sans avoir a
        # parcourir manuellement l'arbre de widgets.
        from tkinter import ttk
        self.assertTrue(hasattr(self.app, "notebook"))
        self.assertIsInstance(self.app.notebook, opl_theme.Rail)
        notebooks = [c for c in self.app.winfo_children() if isinstance(c, opl_theme.Rail)]
        self.assertEqual(len(notebooks), 1)
        self.assertIs(self.app.notebook, notebooks[0])
        tab_texts = [self.app.notebook.tab(t, "text") for t in self.app.notebook.tabs()]
        self.assertEqual(tab_texts, ["Apercu", "Historique", "Reglages avances"])

    def test_window_at_minsize_keeps_result_notebook_visible(self):
        # Regression D3 (audit) : redimensionner la fenetre a sa taille
        # minimale declaree (minsize) faisait disparaitre ENTIEREMENT le
        # Notebook Apercu/Historique (hauteur ecrasee a 0/quasi-0), alors
        # que minsize est censee rester une taille utilisable. Les reglages
        # avances etant desormais dans leur propre onglet defilable (voir
        # _make_scrollable_frame), le contenu au-dessus du Notebook est
        # nettement plus court et ne doit plus, a lui seul, epuiser toute la
        # hauteur minimale de la fenetre.
        self.app.update()
        min_w, min_h = self.app.minsize()
        self.app.geometry(f"{min_w}x{min_h}")
        self.app.update()

        from tkinter import ttk
        notebooks = [c for c in self.app.winfo_children() if isinstance(c, opl_theme.Rail)]
        notebook = notebooks[0]
        self.assertTrue(notebook.winfo_ismapped(), "le Notebook Apercu/Historique a disparu a la taille minimale")
        self.assertGreater(
            notebook.winfo_height(), 100,
            "le Notebook Apercu/Historique est ecrase a une hauteur inutilisable a la taille minimale",
        )

    # -- D2 : infobulle sur la colonne "Raison" (texte tronque a l'affichage) -

    def _hover_column(self, tooltip, tree, item, column):
        bbox = tree.bbox(item, column)
        self.assertTrue(bbox, f"cellule '{column}' introuvable/hors ecran (bbox vide)")
        x, y, w, h = bbox
        import types
        fake_event = types.SimpleNamespace(x=x + 5, y=y + h // 2)
        tooltip._on_motion(fake_event)
        return fake_event

    def test_preview_raison_tooltip_shows_full_text_on_hover(self):
        long_reason = (
            "extension '.pdf' incoherente avec le contenu reel du fichier "
            "(signature detectee : Installateurs) - verification manuelle recommandee"
        )
        move = org.PlannedMove(
            source=self.downloads / "facture_suspecte.pdf",
            destination=self.target / "A verifier" / "facture_suspecte.pdf",
            category="A verifier", reason=long_reason,
        )
        result = org.OrganizeResult(moves=[move])
        self.app._fill_preview(result)
        self.app.update()

        item = self.app.preview_tree.get_children()[0]
        tooltip = self.app._preview_reason_tooltip
        self._hover_column(tooltip, self.app.preview_tree, item, "raison")

        self.assertIsNotNone(tooltip._tooltip, "aucune infobulle n'est apparue au survol de la colonne Raison")
        # Regression trouvee en ecrivant ce test : _show() appelait l'ancien
        # _hide() (qui reinitialise _last_key), annulant aussitot le
        # _last_key que _on_motion venait de positionner. Verrouille ici
        # explicitement pour ne pas la reintroduire.
        row_id = item
        col_id = "#4"  # "raison" est la 4e colonne declaree du preview_tree
        self.assertEqual(tooltip._last_key, (row_id, col_id))

        label = tooltip._tooltip.winfo_children()[0]
        self.assertEqual(label.cget("text"), long_reason)
        # Largeur de colonne (160px a l'origine, elargie a 260px) : le texte
        # integral affiche par l'infobulle doit rester plus long que ce que
        # la colonne peut afficher, sinon ce test ne verrouille plus rien.
        self.assertGreater(len(long_reason), self.app.preview_tree.column("raison", "width") // 6)

    def test_preview_raison_tooltip_reuses_same_window_while_hovering_same_cell(self):
        # Survoler deux fois de suite EXACTEMENT la meme cellule (ce qui
        # arrive en continu avec de vrais evenements <Motion> pendant que la
        # souris reste immobile) ne doit ni detruire ni recreer l'infobulle -
        # directement du au correctif du bug _last_key ci-dessus.
        move = org.PlannedMove(
            source=self.downloads / "a.pdf", destination=self.target / "PDF" / "a.pdf",
            category="PDF", reason="raison suffisamment longue pour ce test de non-regression",
        )
        result = org.OrganizeResult(moves=[move])
        self.app._fill_preview(result)
        self.app.update()

        item = self.app.preview_tree.get_children()[0]
        tooltip = self.app._preview_reason_tooltip
        self._hover_column(tooltip, self.app.preview_tree, item, "raison")
        first_window = tooltip._tooltip
        self.assertIsNotNone(first_window)

        self._hover_column(tooltip, self.app.preview_tree, item, "raison")
        self.assertIs(tooltip._tooltip, first_window)

    def test_preview_raison_tooltip_hides_on_leave(self):
        move = org.PlannedMove(
            source=self.downloads / "a.pdf", destination=self.target / "PDF" / "a.pdf",
            category="PDF", reason="raison suffisamment longue pour ce test de non-regression",
        )
        result = org.OrganizeResult(moves=[move])
        self.app._fill_preview(result)
        self.app.update()

        item = self.app.preview_tree.get_children()[0]
        tooltip = self.app._preview_reason_tooltip
        self._hover_column(tooltip, self.app.preview_tree, item, "raison")
        self.assertIsNotNone(tooltip._tooltip)

        tooltip._hide()
        self.assertIsNone(tooltip._tooltip)
        self.assertIsNone(tooltip._last_key)

    # -- D4 : dialogue "Detail du lot" - toutes les colonnes doivent tenir --

    def test_batch_detail_dialog_shows_all_declared_columns_including_erreur(self):
        # Regression D4 (audit) : les 6 colonnes declarees (fichier,
        # categorie, destination, raison, statut, erreur) totalisaient
        # 940px alors que le dialogue ne faisait que 820px de large - la
        # colonne "Erreur" (la plus a droite, celle qui explique POURQUOI
        # un deplacement a echoue) n'etait pas du tout visible sans
        # redimensionnement manuel.
        long_error = (
            "[WinError 32] Le processus ne peut pas acceder au fichier car ce fichier "
            "est utilise par un autre processus: 'C:\\\\Users\\\\test\\\\Downloads\\\\photo.jpg' "
            "(fichier partiel nettoye a la destination)"
        )
        batch = {
            "timestamp": "2026-07-22 08:00:00",
            "simulated": False,
            "moves": [
                {
                    "source": str(self.downloads / "photo.jpg"),
                    "destination": str(self.target / "Images" / "photo.jpg"),
                    "category": "Images", "reason": "extension .jpg", "status": "erreur",
                    "error": long_error,
                },
                {
                    "source": str(self.downloads / "a.pdf"),
                    "destination": str(self.target / "PDF" / "a.pdf"),
                    "category": "PDF", "reason": "extension .pdf", "status": "deplace",
                },
            ],
        }
        org.append_batch_to_history(batch)
        self.app._refresh_history_view()
        children = self.app.history_tree.get_children()
        self.assertEqual(len(children), 1)
        self.app.history_tree.selection_set(children[0])

        before = {w for w in self.app.winfo_children() if w.winfo_class() == "Toplevel"}
        self.app._show_batch_detail()
        self.addCleanup(self._close_stray_toplevels, before)
        self.app.update()
        after = [w for w in self.app.winfo_children() if w.winfo_class() == "Toplevel"]
        new_toplevels = [w for w in after if w not in before]
        self.assertEqual(len(new_toplevels), 1, "le dialogue 'Detail du lot' ne s'est pas ouvert")
        dialog = new_toplevels[0]
        dialog.update()

        from tkinter import ttk

        def find_treeview(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Treeview):
                    return child
                found = find_treeview(child)
                if found is not None:
                    return found
            return None

        tree = find_treeview(dialog)
        self.assertIsNotNone(tree, "Treeview du dialogue 'Detail du lot' introuvable")

        columns = tree["columns"]
        self.assertIn("erreur", columns)
        total_width = sum(tree.column(c, "width") for c in columns)
        dialog_width = dialog.winfo_width()
        self.assertLessEqual(
            total_width, dialog_width,
            f"les colonnes ({total_width}px) depassent la largeur du dialogue ({dialog_width}px)",
        )

        error_item = None
        for item in tree.get_children():
            if tree.set(item, "erreur"):
                error_item = item
                break
        self.assertIsNotNone(error_item, "aucune ligne avec une erreur trouvee")
        self.assertEqual(tree.set(error_item, "erreur"), long_error)
        erreur_bbox = tree.bbox(error_item, "erreur")
        self.assertTrue(erreur_bbox, "la cellule 'Erreur' n'est pas visible (bbox vide)")
        ex, ey, ew, eh = erreur_bbox
        self.assertLessEqual(ex + ew, tree.winfo_width() + 2, "la cellule 'Erreur' deborde du Treeview visible")

    def _close_stray_toplevels(self, before):
        for w in [c for c in self.app.winfo_children() if c.winfo_class() == "Toplevel"]:
            if w not in before:
                try:
                    w.destroy()
                except Exception:
                    pass

    # -- D8/E1 : icone de fenetre/executable + AppUserModelID --------------

    def test_resolve_icon_path_finds_the_bundled_icon_when_running_from_source(self):
        icon_path = gui._resolve_icon_path()
        self.assertIsNotNone(icon_path, "assets/icon.ico introuvable en lancant depuis le code source")
        self.assertTrue(icon_path.is_file())
        self.assertEqual(icon_path.name, "icon.ico")

    def test_resolve_icon_path_uses_meipass_when_frozen_by_pyinstaller(self):
        # Simule le mode "gele" PyInstaller (onefile) : sys._MEIPASS pointe
        # alors vers le dossier temporaire d'extraction - assets/icon.ico
        # doit y etre cherche plutot qu'a cote du script source (audit D8/E1).
        fake_meipass = self.tmp / "meipass_simule"
        (fake_meipass / "assets").mkdir(parents=True)
        (fake_meipass / "assets" / "icon.ico").write_bytes(b"\x00\x00\x01\x00")
        sys_module = gui.sys
        had_meipass = hasattr(sys_module, "_MEIPASS")
        original = getattr(sys_module, "_MEIPASS", None)
        sys_module._MEIPASS = str(fake_meipass)
        try:
            icon_path = gui._resolve_icon_path()
        finally:
            if had_meipass:
                sys_module._MEIPASS = original
            else:
                del sys_module._MEIPASS
        self.assertEqual(icon_path, fake_meipass / "assets" / "icon.ico")

    def test_resolve_icon_path_returns_none_when_the_file_is_missing(self):
        sys_module = gui.sys
        fake_meipass = self.tmp / "meipass_sans_icone"
        fake_meipass.mkdir()
        had_meipass = hasattr(sys_module, "_MEIPASS")
        original = getattr(sys_module, "_MEIPASS", None)
        sys_module._MEIPASS = str(fake_meipass)
        try:
            icon_path = gui._resolve_icon_path()
        finally:
            if had_meipass:
                sys_module._MEIPASS = original
            else:
                del sys_module._MEIPASS
        self.assertIsNone(icon_path)

    def test_set_app_user_model_id_does_not_raise(self):
        gui._set_app_user_model_id()  # ne doit jamais lever, purement cosmetique

    def test_apply_window_icon_does_not_raise_when_the_icon_cannot_be_resolved(self):
        # Une icone introuvable/illisible ne doit jamais empecher la fenetre
        # de s'ouvrir (correctif ecrit avec un try/except tk.TclError autour
        # de self.iconbitmap - ce test verrouille le chemin "icone absente")
        # ni de rester utilisable ensuite.
        with mock.patch.object(gui, "_resolve_icon_path", return_value=None):
            self.app._apply_window_icon()  # ne doit lever aucune exception
        self.app.update()
        self.assertTrue(self.app.winfo_exists())

    # -- Recherche/filtre dans les tableaux Apercu et Historique -----------

    def _preview_result(self):
        return org.OrganizeResult(moves=[
            org.PlannedMove(
                source=self.downloads / "rapport_annuel.pdf",
                destination=self.target / "PDF" / "rapport_annuel.pdf",
                category="PDF", reason="extension .pdf",
            ),
            org.PlannedMove(
                source=self.downloads / "vacances.jpg",
                destination=self.target / "Images" / "vacances.jpg",
                category="Images", reason="extension .jpg",
            ),
            org.PlannedMove(
                source=self.downloads / "archive.zip",
                destination=self.target / "Archives" / "archive.zip",
                category="Archives", reason="extension .zip",
            ),
        ])

    def _row_values(self, tree):
        return [tuple(tree.item(iid, "values")) for iid in tree.get_children()]

    def test_preview_search_filters_rows_in_real_time_on_any_column(self):
        self.app._fill_preview(self._preview_result())
        self.app.update()
        self.assertEqual(len(self.app.preview_tree.get_children()), 3)

        # Match sur la colonne "fichier" (insensible a la casse).
        self.app.preview_search_var.set("VACANCES")
        self.app.update()
        rows = self._row_values(self.app.preview_tree)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "vacances.jpg")
        self.assertEqual(self.app.preview_count_var.get(), "1/3 resultats")

        # Match sur la colonne "categorie".
        self.app.preview_search_var.set("images")
        self.app.update()
        self.assertEqual(len(self.app.preview_tree.get_children()), 1)

        # Un terme absent ne renvoie aucune ligne.
        self.app.preview_search_var.set("introuvable-xyz")
        self.app.update()
        self.assertEqual(len(self.app.preview_tree.get_children()), 0)
        self.assertEqual(self.app.preview_count_var.get(), "0/3 resultats")

        # Vider le champ reaffiche tout.
        self.app.preview_search_var.set("")
        self.app.update()
        self.assertEqual(len(self.app.preview_tree.get_children()), 3)
        self.assertEqual(self.app.preview_count_var.get(), "3 ligne(s)")

    def test_history_search_filters_and_preserves_iids_for_undo(self):
        for i, cat in enumerate(("Images", "PDF")):
            org.append_batch_to_history({
                "timestamp": f"2026-07-2{i} 08:00:00",
                "simulated": False,
                "moves": [{
                    "source": str(self.downloads / f"fichier_{cat}.dat"),
                    "destination": str(self.target / cat / f"fichier_{cat}.dat"),
                    "category": cat, "reason": f"extension {cat}", "status": "deplace",
                }],
            })
        self.app._refresh_history_view()
        self.app.update()
        self.assertEqual(len(self.app.history_tree.get_children()), 2)

        # iid = index absolu du lot : le second lot ajoute a l'index 1.
        expected_iid = "1"
        all_iids = set(self.app.history_tree.get_children())
        self.assertIn(expected_iid, all_iids)

        # Filtre sur le detail (nom de fichier du lot PDF).
        self.app.history_search_var.set("fichier_pdf")
        self.app.update()
        filtered = self.app.history_tree.get_children()
        self.assertEqual(len(filtered), 1)
        # L'iid absolu est preserve malgre le filtrage : indispensable pour que
        # l'annulation/le detail agissent sur le bon lot.
        self.assertEqual(filtered[0], expected_iid)
        self.assertEqual(self.app.history_count_var.get(), "1/2 resultats")

        # Vider reaffiche les deux lots, sans indicateur de resultats.
        self.app.history_search_var.set("")
        self.app.update()
        self.assertEqual(len(self.app.history_tree.get_children()), 2)
        self.assertEqual(self.app.history_count_var.get(), "")


if __name__ == "__main__":
    unittest.main()

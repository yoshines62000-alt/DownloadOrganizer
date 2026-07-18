"""Interface graphique (Tkinter) pour le Nettoyeur intelligent du dossier Telechargements."""

from __future__ import annotations

import csv
import io
import json
import os
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

DONATE_URL = "https://ko-fi.com/yoshines62000"

from organizer import (
    DownloadOrganizer,
    load_config,
    save_config,
    export_config,
    import_config,
    load_history,
    export_html_report,
    APP_DIR,
    DUPLICATES_TARGET,
    DEFAULT_WATCH_INTERVAL_SECONDS,
)


def _stat_signature(path: Path):
    """(taille, date de modification) d'un fichier, utilise par le mode Veille
    pour detecter si un fichier est encore en cours d'ecriture (telechargement)."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime)
    except OSError:
        return (None, None)


class OrganizerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Nettoyeur intelligent - Telechargements")
        self.geometry("880x600")
        self.minsize(760, 520)

        self.config_data = load_config()
        self.organizer = DownloadOrganizer(self.config_data)
        self.last_real_batch = None

        # Etat du mode Veille : jamais active automatiquement au demarrage,
        # meme si un intervalle est enregistre - l'utilisateur doit l'activer
        # explicitement a chaque session, pour eviter toute surprise.
        self.watch_active = False
        self._watch_after_id = None
        self._watch_last_signature = None

        self._build_widgets()
        self._refresh_history_view()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Construction de l'UI
    # ------------------------------------------------------------------

    def _build_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Dossier Telechargements :").grid(row=0, column=0, sticky="w")
        self.downloads_var = tk.StringVar(value=self.config_data["downloads_dir"])
        ttk.Entry(top, textvariable=self.downloads_var, width=60).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(top, text="Parcourir...", command=self._browse_downloads).grid(row=0, column=2)

        ttk.Label(top, text="Dossier de destination racine :").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.base_target_var = tk.StringVar(value=self.config_data["base_target_dir"])
        ttk.Entry(top, textvariable=self.base_target_var, width=60).grid(row=1, column=1, sticky="we", padx=5, pady=(6, 0))
        ttk.Button(top, text="Parcourir...", command=self._browse_base_target).grid(row=1, column=2, pady=(6, 0))

        ttk.Label(top, text="Anciennete (jours) pour 'A verifier' :").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.age_var = tk.StringVar(value=str(self.config_data.get("old_file_threshold_days", 90)))
        ttk.Spinbox(top, from_=1, to=3650, textvariable=self.age_var, width=8).grid(row=2, column=1, sticky="w", padx=5, pady=(6, 0))

        top.columnconfigure(1, weight=1)

        # Exclusions
        excl_frame = ttk.LabelFrame(self, text="Exclusions personnalisees", padding=10)
        excl_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(excl_frame, text="Extensions (separees par des virgules, ex: .tmp,.log)").grid(row=0, column=0, sticky="w")
        self.excl_ext_var = tk.StringVar(value=", ".join(self.config_data["exclusions"].get("extensions", [])))
        ttk.Entry(excl_frame, textvariable=self.excl_ext_var, width=40).grid(row=0, column=1, sticky="we", padx=5)

        ttk.Label(excl_frame, text="Noms de fichiers exacts (separes par des virgules)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.excl_names_var = tk.StringVar(value=", ".join(self.config_data["exclusions"].get("filenames", [])))
        ttk.Entry(excl_frame, textvariable=self.excl_names_var, width=40).grid(row=1, column=1, sticky="we", padx=5, pady=(4, 0))

        ttk.Label(
            excl_frame,
            text="Motifs additionnels (glob, ex: *.bak) - s'ajoutent aux protections integrees (*.crdownload, *.part, *.tmp, desktop.ini, *.download), qui restent toujours actives",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.excl_patterns_var = tk.StringVar(value=", ".join(self.config_data["exclusions"].get("patterns", [])))
        ttk.Entry(excl_frame, textvariable=self.excl_patterns_var, width=40).grid(row=2, column=1, sticky="we", padx=5, pady=(4, 0))

        excl_frame.columnconfigure(1, weight=1)

        # Mode Veille (surveillance optionnelle)
        watch_frame = ttk.LabelFrame(self, text="Mode Veille (optionnel)", padding=10)
        watch_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(watch_frame, text="Verifier le dossier toutes les").grid(row=0, column=0, sticky="w")
        self.watch_interval_var = tk.StringVar(
            value=str(self.config_data.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS))
        )
        ttk.Spinbox(watch_frame, from_=5, to=3600, textvariable=self.watch_interval_var, width=6).grid(
            row=0, column=1, sticky="w", padx=5
        )
        ttk.Label(watch_frame, text="secondes").grid(row=0, column=2, sticky="w")
        self.watch_btn = ttk.Button(watch_frame, text="Activer la veille", command=self._toggle_watch)
        self.watch_btn.grid(row=0, column=3, sticky="w", padx=(20, 0))
        self.watch_status_var = tk.StringVar(value="Veille inactive.")
        ttk.Label(watch_frame, textvariable=self.watch_status_var, foreground="#555").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            watch_frame,
            text=(
                "La veille surveille le dossier en arriere-plan mais ne deplace jamais rien "
                "automatiquement : une confirmation groupee vous est toujours demandee des que "
                "de nouveaux fichiers sont detectes et stables (plus aucun telechargement en cours)."
            ),
            wraplength=760,
            foreground="#555",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # Boutons d'action, repartis sur deux rangees : un seul rang de 6
        # boutons deborde de la fenetre par defaut (largeur fixe 880px) et
        # tronque/cache les derniers boutons plutot que de les faire passer
        # a la ligne (ttk.Frame + pack(side="left") ne wrap jamais).
        btn_frame_1 = ttk.Frame(self, padding=(10, 0))
        btn_frame_1.pack(fill="x")
        btn_frame_2 = ttk.Frame(self, padding=(10, 4))
        btn_frame_2.pack(fill="x")

        ttk.Button(btn_frame_1, text="Enregistrer la configuration (Ctrl+S)", command=self._save_config).pack(side="left")
        ttk.Button(btn_frame_1, text="Simuler (F5)", command=self._simulate).pack(side="left", padx=6)
        self.run_btn = ttk.Button(btn_frame_1, text="Ranger les fichiers (Ctrl+Entree)", command=self._run_real)
        self.run_btn.pack(side="left")
        ttk.Button(btn_frame_1, text="Annuler le dernier rangement (Ctrl+Z)", command=self._undo).pack(side="left", padx=6)

        ttk.Button(btn_frame_2, text="Ouvrir le dossier de destination", command=self._open_target_dir).pack(side="left")
        self.export_btn = ttk.Button(btn_frame_2, text="Exporter le rapport (HTML)", command=self._export_report, state="disabled")
        self.export_btn.pack(side="left", padx=6)
        ttk.Button(btn_frame_2, text="Exporter la configuration...", command=self._export_config_dialog).pack(side="left", padx=6)
        ttk.Button(btn_frame_2, text="Importer une configuration...", command=self._import_config_dialog).pack(side="left")

        # Notebook: apercu + historique
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        preview_frame = ttk.Frame(notebook)
        notebook.add(preview_frame, text="Apercu")

        columns = ("fichier", "categorie", "destination", "raison")
        self.preview_tree = ttk.Treeview(preview_frame, columns=columns, show="headings", height=15)
        for col, label, width in [
            ("fichier", "Fichier", 200),
            ("categorie", "Categorie", 110),
            ("destination", "Destination", 320),
            ("raison", "Raison", 160),
        ]:
            self.preview_tree.heading(col, text=label)
            self.preview_tree.column(col, width=width, anchor="w")
        self.preview_tree.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview_tree.yview)
        self.preview_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(fill="y", side="right")

        history_frame = ttk.Frame(notebook)
        notebook.add(history_frame, text="Historique")

        hist_columns = ("date", "mode", "nb", "detail")
        self.history_tree = ttk.Treeview(history_frame, columns=hist_columns, show="headings", height=15)
        for col, label, width in [
            ("date", "Date", 160),
            ("mode", "Mode", 100),
            ("nb", "Fichiers", 80),
            ("detail", "Detail", 400),
        ]:
            self.history_tree.heading(col, text=label)
            self.history_tree.column(col, width=width, anchor="w")
        self.history_tree.pack(fill="both", expand=True, side="left")
        hscroll = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=hscroll.set)
        hscroll.pack(fill="y", side="right")

        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Pret.")
        ttk.Label(bottom_bar, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            fill="x", side="left", expand=True
        )
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground="#0645AD", cursor="hand2")
        donate_label.pack(side="right", padx=8)
        donate_label.bind("<Button-1>", lambda event: webbrowser.open(DONATE_URL))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _browse_downloads(self):
        path = filedialog.askdirectory(title="Choisir le dossier Telechargements", initialdir=self.downloads_var.get())
        if path:
            self.downloads_var.set(path)

    def _browse_base_target(self):
        path = filedialog.askdirectory(title="Choisir le dossier de destination racine", initialdir=self.base_target_var.get())
        if path:
            self.base_target_var.set(path)

    def _collect_config(self):
        """Retourne la config a jour, ou None (et affiche une erreur) si un champ est invalide."""
        def split_csv(value: str) -> list:
            # Utilise le module csv pour permettre des valeurs entre guillemets
            # contenant elles-memes une virgule (ex: "a,b.txt").
            reader = csv.reader(io.StringIO(value), skipinitialspace=True)
            items = next(reader, [])
            return [v.strip() for v in items if v.strip()]

        try:
            age_days = int(self.age_var.get().strip())
            if age_days < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Valeur invalide",
                "Le champ 'Anciennete (jours)' doit contenir un nombre entier positif.",
            )
            return None

        self.config_data["downloads_dir"] = self.downloads_var.get().strip()
        self.config_data["base_target_dir"] = self.base_target_var.get().strip()
        self.config_data["old_file_threshold_days"] = age_days
        self.config_data["exclusions"] = {
            "extensions": split_csv(self.excl_ext_var.get()),
            "filenames": split_csv(self.excl_names_var.get()),
            "patterns": split_csv(self.excl_patterns_var.get()),
        }
        return self.config_data

    def _save_config(self):
        config = self._collect_config()
        if config is None:
            return
        save_config(config)
        self.organizer = DownloadOrganizer(config)
        self.status_var.set("Configuration enregistree.")

    def _apply_config_to_ui(self, config: dict) -> None:
        self.downloads_var.set(config["downloads_dir"])
        self.base_target_var.set(config["base_target_dir"])
        self.age_var.set(str(config.get("old_file_threshold_days", 90)))
        exclusions = config.get("exclusions", {})
        self.excl_ext_var.set(", ".join(exclusions.get("extensions", [])))
        self.excl_names_var.set(", ".join(exclusions.get("filenames", [])))
        self.excl_patterns_var.set(", ".join(exclusions.get("patterns", [])))
        self.watch_interval_var.set(str(config.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS)))
        self.config_data = config
        self.organizer = DownloadOrganizer(config)

    def _export_config_dialog(self):
        config = self._collect_config()
        if config is None:
            return
        path = filedialog.asksaveasfilename(
            title="Exporter la configuration", initialfile="download_organizer_config.json",
            defaultextension=".json", filetypes=[("Fichier JSON", "*.json")],
        )
        if not path:
            return
        try:
            export_config(config, Path(path))
        except OSError as exc:
            messagebox.showerror("Erreur", f"Impossible d'exporter la configuration : {exc}")
            return
        self.status_var.set(f"Configuration exportee : {path}")

    def _import_config_dialog(self):
        path = filedialog.askopenfilename(
            title="Importer une configuration", filetypes=[("Fichier JSON", "*.json")],
        )
        if not path:
            return
        try:
            config = import_config(Path(path))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            messagebox.showerror("Erreur", f"Impossible d'importer cette configuration : {exc}")
            return
        self._apply_config_to_ui(config)
        save_config(config)
        self.status_var.set(f"Configuration importee depuis {Path(path).name} et enregistree.")

    def _fill_preview(self, result):
        self.preview_tree.delete(*self.preview_tree.get_children())
        for move in result.moves:
            self.preview_tree.insert("", "end", values=(move.source.name, move.category, str(move.destination), move.reason))

    def _simulate(self):
        config = self._collect_config()
        if config is None:
            return
        # Intentionnellement non persiste : "Simuler" sert a tester des valeurs
        # sans les enregistrer definitivement. Utilisez "Enregistrer la
        # configuration" ou "Ranger les fichiers" pour persister.
        #
        # Important : on utilise un organizer temporaire, sans toucher a
        # self.organizer. Si le Mode Veille tourne en arriere-plan, il
        # continue d'utiliser la configuration active (sauvegardee) - un
        # simple "Simuler" ne doit jamais changer silencieusement son
        # comportement.
        preview_organizer = DownloadOrganizer(config)

        result = preview_organizer.plan()
        if result.errors:
            messagebox.showerror("Erreur", "\n".join(f"{p}: {e}" for p, e in result.errors))
            return

        self._fill_preview(result)
        preview_organizer.execute(result, simulate=True)
        duplicates = [m for m in result.moves if m.category == DUPLICATES_TARGET]
        extra = f", {len(result.skipped_dirs)} sous-dossier(s) non parcouru(s)" if result.skipped_dirs else ""
        dup_note = f", dont {len(duplicates)} doublon(s) de contenu detecte(s)" if duplicates else ""
        self.status_var.set(
            f"Simulation : {len(result.moves)} fichier(s) seraient deplaces{dup_note}, "
            f"{len(result.excluded)} ignore(s){extra}. Aucun fichier n'a ete modifie."
        )

    def _run_real(self):
        config = self._collect_config()
        if config is None:
            return
        save_config(config)
        self.organizer = DownloadOrganizer(config)

        result = self.organizer.plan()
        if result.errors:
            messagebox.showerror("Erreur", "\n".join(f"{p}: {e}" for p, e in result.errors))
            return

        if not result.moves:
            messagebox.showinfo("Rien a faire", "Aucun fichier a ranger pour le moment.")
            return

        self._fill_preview(result)

        duplicates = [m for m in result.moves if m.category == DUPLICATES_TARGET]
        dup_line = (
            f"Dont {len(duplicates)} doublon(s) de contenu identique, ranges a part dans "
            f"'{DUPLICATES_TARGET}' (rien n'est jamais supprime).\n"
            if duplicates else ""
        )
        confirm = messagebox.askyesno(
            "Confirmer le rangement",
            f"{len(result.moves)} fichier(s) vont etre deplaces (aucune suppression).\n"
            f"{dup_line}"
            "Vous pourrez annuler ce lot via le bouton 'Annuler le dernier rangement'.\n\n"
            "Continuer ?",
        )
        if not confirm:
            return

        batch = self.organizer.execute(result, simulate=False)
        errors = [m for m in batch["moves"] if m["status"] == "erreur"]
        moved = [m for m in batch["moves"] if m["status"] == "deplace"]
        self.last_real_batch = batch
        self.export_btn.configure(state="normal")
        self.status_var.set(f"{len(moved)} fichier(s) deplace(s), {len(errors)} erreur(s).")
        if errors:
            messagebox.showwarning(
                "Terminee avec des erreurs",
                "\n".join(f"{e['source']}: {e.get('error', '')}" for e in errors),
            )
        if batch.get("history_error"):
            messagebox.showwarning(
                "Historique non enregistre",
                "Les fichiers ont bien ete deplaces, mais l'historique n'a pas pu etre "
                f"enregistre ({batch['history_error']}) : l'annulation ne sera pas "
                "disponible pour ce lot.",
            )
        self._refresh_history_view()

    def _open_target_dir(self):
        target = self.base_target_var.get().strip()
        if not target or not Path(target).exists():
            messagebox.showerror("Dossier introuvable", "Le dossier de destination racine est introuvable.")
            return
        os.startfile(target)  # nosec - ouverture Explorateur Windows d'un dossier local choisi par l'utilisateur

    def _export_report(self):
        if not self.last_real_batch:
            messagebox.showinfo("Aucun rapport", "Effectuez d'abord un rangement reel pour generer un rapport.")
            return
        reports_dir = APP_DIR / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"rapport-{time.strftime('%Y%m%d-%H%M%S')}.html"
        path = reports_dir / filename
        base_dir = Path(self.base_target_var.get().strip()) if self.base_target_var.get().strip() else None
        export_html_report(self.last_real_batch, path, base_dir=base_dir)
        self.status_var.set(f"Rapport exporte : {path}")
        if messagebox.askyesno("Rapport exporte", f"Rapport enregistre dans :\n{path}\n\nL'ouvrir maintenant ?"):
            os.startfile(str(path))

    # -- Mode Veille (surveillance optionnelle, sans deplacement automatique) --

    def _toggle_watch(self):
        if self.watch_active:
            self._stop_watch("Veille desactivee.")
        else:
            self._start_watch()

    def _start_watch(self):
        config = self._collect_config()
        if config is None:
            return
        try:
            interval = int(self.watch_interval_var.get().strip())
            if interval < 5:
                raise ValueError
        except ValueError:
            messagebox.showerror("Valeur invalide", "L'intervalle de veille doit etre un nombre entier d'au moins 5 secondes.")
            return
        config["watch_interval_seconds"] = interval
        save_config(config)
        self.organizer = DownloadOrganizer(config)

        self.watch_active = True
        self._watch_last_signature = None
        self.watch_btn.configure(text="Desactiver la veille")
        self.watch_status_var.set("Veille active : premiere verification en cours...")
        self._watch_tick()

    def _stop_watch(self, status_message: str = "Veille desactivee."):
        self.watch_active = False
        if self._watch_after_id is not None:
            try:
                self.after_cancel(self._watch_after_id)
            except (ValueError, tk.TclError):
                pass
            self._watch_after_id = None
        self.watch_btn.configure(text="Activer la veille")
        self.watch_status_var.set(status_message)

    def _watch_tick(self):
        if not self.watch_active:
            return

        try:
            result = self.organizer.plan()
        except Exception as exc:  # noqa: BLE001 - garde-fou de la boucle de fond :
            # toute exception inattendue doit arreter proprement la veille et
            # le signaler, plutot que de laisser un self.after() jamais
            # reprogramme geler silencieusement "Veille active" pour toujours.
            self._stop_watch(f"Veille interrompue (erreur inattendue) : {exc}")
            return

        if result.errors:
            self._stop_watch(
                "Veille interrompue : " + "; ".join(f"{p}: {e}" for p, e in result.errors)
            )
            return

        # "Signature" du lot en attente (fichier + taille + date de modification) :
        # si elle est identique a celle observee au tour precedent, aucun fichier
        # n'a change depuis -> les telechargements sont consideres termines et on
        # propose le rangement. Sinon on attend encore (fichiers en cours d'ecriture).
        signature = tuple(sorted(
            (str(m.source), *(_stat_signature(m.source)))
            for m in result.moves
        ))

        if result.moves and signature == self._watch_last_signature:
            self._watch_last_signature = None
            self._prompt_watch_batch(result)
        else:
            self._watch_last_signature = signature
            if result.moves:
                self.watch_status_var.set(
                    f"Veille active : {len(result.moves)} fichier(s) detecte(s), "
                    "stabilisation en cours (attente de fin de telechargement)..."
                )
            else:
                self.watch_status_var.set("Veille active : aucun nouveau fichier a ranger pour le moment.")

        if self.watch_active:
            try:
                interval = int(self.watch_interval_var.get().strip())
                if interval < 5:
                    raise ValueError
            except ValueError:
                # Champ modifie en cours de route avec une valeur invalide : on
                # garde la derniere cadence connue plutot que d'interrompre la veille.
                interval = self.organizer.config.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS)
            self._watch_after_id = self.after(interval * 1000, self._watch_tick)

    def _prompt_watch_batch(self, result):
        self._fill_preview(result)
        duplicates = [m for m in result.moves if m.category == DUPLICATES_TARGET]
        dup_line = f"Dont {len(duplicates)} doublon(s) de contenu identique.\n" if duplicates else ""
        confirm = messagebox.askyesno(
            "Veille : nouveaux fichiers detectes",
            f"{len(result.moves)} fichier(s) sont prets a etre ranges (aucune suppression).\n"
            f"{dup_line}"
            "Ranger maintenant ?",
        )
        if confirm:
            batch = self.organizer.execute(result, simulate=False)
            errors = [m for m in batch["moves"] if m["status"] == "erreur"]
            moved = [m for m in batch["moves"] if m["status"] == "deplace"]
            self.last_real_batch = batch
            self.export_btn.configure(state="normal")
            self.watch_status_var.set(
                f"Veille active : {len(moved)} fichier(s) deplace(s), {len(errors)} erreur(s). "
                "Surveillance en cours..."
            )
            if errors:
                messagebox.showwarning(
                    "Terminee avec des erreurs",
                    "\n".join(f"{e['source']}: {e.get('error', '')}" for e in errors),
                )
            if batch.get("history_error"):
                messagebox.showwarning(
                    "Historique non enregistre",
                    "Les fichiers ont bien ete deplaces, mais l'historique n'a pas pu etre "
                    f"enregistre ({batch['history_error']}) : l'annulation ne sera pas "
                    "disponible pour ce lot.",
                )
            self._refresh_history_view()
        else:
            self.watch_status_var.set(
                "Veille active : rangement ignore pour ce lot. Il sera reproposé au prochain "
                "controle stable (meme si le dossier ne change pas d'ici la)."
            )

    def _on_close(self):
        self._stop_watch()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _undo(self):
        confirm = messagebox.askyesno(
            "Annuler le dernier rangement",
            "Voulez-vous vraiment annuler le dernier lot de deplacements reels "
            "(les fichiers seront remis dans le dossier Telechargements) ?",
        )
        if not confirm:
            return

        out = self.organizer.undo_last_batch()
        undone = out.get("undone", [])
        errors = out.get("errors", [])

        if not undone and not errors:
            messagebox.showinfo("Rien a annuler", out.get("message", "Aucun lot a annuler."))
            return

        if undone:
            # Le dernier rapport exportable concernait un lot desormais (au
            # moins partiellement) annule : le proposer encore donnerait un
            # rapport trompeur (fichiers listes "deplaces" alors qu'ils ont
            # ete remis dans Telechargements).
            self.last_real_batch = None
            self.export_btn.configure(state="disabled")

        self.status_var.set(f"{len(undone)} fichier(s) restaure(s), {len(errors)} erreur(s).")
        if errors:
            messagebox.showwarning("Annulation partielle", "\n".join(f"{p}: {e}" for p, e in errors))
        self._refresh_history_view()

    def _refresh_history_view(self):
        self.history_tree.delete(*self.history_tree.get_children())
        history = load_history()
        for batch in reversed(history):
            if batch.get("simulated"):
                mode = "Simulation"
            elif batch.get("undone"):
                mode = "Annule"
            elif any(m.get("status") == "annule" for m in batch["moves"]):
                mode = "Partiellement annule"
            else:
                mode = "Reel"
            moved = [m for m in batch["moves"] if m.get("status") in ("deplace", "planifie")]
            detail = ", ".join(Path(m["source"]).name for m in moved[:5])
            if len(moved) > 5:
                detail += f", ... (+{len(moved) - 5})"
            self.history_tree.insert("", "end", values=(batch["timestamp"], mode, len(moved), detail))

    def _bind_shortcuts(self):
        self.bind_all("<Control-s>", lambda e: self._save_config())
        self.bind_all("<F5>", lambda e: self._simulate())
        self.bind_all("<Control-Return>", lambda e: self._run_real())
        self.bind_all("<Control-z>", lambda e: self._undo())


def run_gui():
    app = OrganizerGUI()
    app.mainloop()


if __name__ == "__main__":
    run_gui()

"""Interface graphique (Tkinter) pour le Nettoyeur intelligent du dossier Telechargements."""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

DONATE_URL = "https://ko-fi.com/yoshines62000"
APP_VERSION = "1.1.1"
UPDATE_REPO = "yoshines62000-alt/DownloadOrganizer"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"

# Identifiant unique de l'application aupres de Windows (audit D8/E1) : sans
# lui, l'exe (non signe, sans metadonnees d'editeur) se retrouve regroupe
# dans la barre des taches/l'epinglage sous une icone/processus generique
# Python plutot que sous sa propre icone dediee. Doit etre defini AVANT la
# creation de toute fenetre (voir _set_app_user_model_id, appele en tout
# debut de OrganizerGUI.__init__).
APP_USER_MODEL_ID = "Yoshines.NettoyeurTelechargements.GUI.1"


def _set_app_user_model_id() -> None:
    """Windows uniquement (audit D8/E1) : associe ce processus a un
    identifiant applicatif dedie plutot que l'identifiant generique de
    l'interpreteur Python qui l'execute (pythonw.exe/python.exe) - permet a
    Windows de regrouper correctement les fenetres de l'application dans la
    barre des taches et d'afficher son ICONE PROPRE (celle posee via
    iconbitmap/le .spec PyInstaller, voir _resolve_icon_path) plutot que
    l'icone Python generique, y compris quand l'app est epinglee. Echoue
    silencieusement sur toute plateforme/environnement ou l'appel n'est pas
    disponible (non-Windows, ctypes indisponible...) : purement cosmetique,
    ne doit jamais empecher l'application de demarrer."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (OSError, AttributeError, ValueError):
        pass


def _resolve_icon_path() -> "Path | None":
    """Chemin de assets/icon.ico, en tenant compte du mode "gele" PyInstaller
    (audit D8/E1) : une fois empaquete en onefile, les fichiers listes dans
    `datas` (voir NettoyeurTelechargements.spec) sont extraits a l'execution
    dans un dossier temporaire expose via `sys._MEIPASS` - un chemin relatif
    au script source (utilise quand on lance gui.py directement depuis le
    depot) ne pointerait vers rien de valide dans l'exe gele. Renvoie None
    (plutot que de lever) si le fichier est introuvable dans les deux cas :
    l'absence d'icone ne doit jamais empecher le demarrage de l'application."""
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon_path = base_dir / "assets" / "icon.ico"
    return icon_path if icon_path.is_file() else None

import opl_theme
import opl_contact
import update_checker
from organizer import (
    DownloadOrganizer,
    load_config,
    save_config,
    export_config,
    import_config,
    load_history,
    purge_history,
    export_html_report,
    BATCH_STATUS_LABELS,
    APP_DIR,
    DUPLICATES_TARGET,
    DEFAULT_WATCH_INTERVAL_SECONDS,
    DEFAULT_CATEGORIES,
)


def _stat_signature(path: Path):
    """(taille, date de modification) d'un fichier, utilise par le mode Veille
    pour detecter si un fichier est encore en cours d'ecriture (telechargement)."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime)
    except OSError:
        return (None, None)


class _TreeviewCellTooltip:
    """Infobulle affichant le texte INTEGRAL d'une cellule de Treeview au
    survol, pour une colonne donnee (correctif audit D2/D4) : la colonne
    "Raison" (et, dans le dialogue "Detail du lot", "Erreur") peut contenir
    un message plus long que la largeur de colonne affichee, tronque
    visuellement par Tk sans aucune indication qu'il manque du texte. La
    valeur stockee dans le Treeview reste toujours integrale (c'est un
    simple probleme d'affichage) ; cette infobulle se contente de la
    re-afficher en entier au survol, sans modifier la donnee elle-meme."""

    def __init__(self, tree: "ttk.Treeview", column_names):
        self.tree = tree
        self.column_names = set(column_names)
        self._tooltip = None
        self._last_key = None
        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        # Toute action qui peut faire bouger/disparaitre la cellule survolee
        # (defilement, redimensionnement de colonne) doit aussi faire
        # disparaitre l'infobulle plutot que de la laisser desynchronisee.
        tree.bind("<ButtonPress>", self._hide, add="+")

    def _on_motion(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            self._hide()
            return
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)  # ex: "#4"
        if not row_id or not col_id:
            self._hide()
            return
        try:
            col_index = int(col_id.lstrip("#")) - 1
        except ValueError:
            self._hide()
            return
        columns = self.tree["columns"]
        if not (0 <= col_index < len(columns)) or columns[col_index] not in self.column_names:
            self._hide()
            return
        value = self.tree.set(row_id, columns[col_index])
        if not value:
            self._hide()
            return
        key = (row_id, col_id)
        if key != self._last_key:
            self._last_key = key
            self._show(value)
        self._position(event)

    def _show(self, text: str):
        # Detruit une eventuelle infobulle precedente SANS reinitialiser
        # self._last_key (contrairement a _hide ci-dessous) : _on_motion
        # vient tout juste d'affecter self._last_key juste avant d'appeler
        # _show - le reinitialiser ici l'annulerait aussitot (bug trouve
        # pendant l'ecriture du test de fumee D2 : l'infobulle apparaissait
        # bien un instant, puis self._last_key valait de nouveau None, ce
        # qui la faisait immediatement re-creer/redisparaitre au moindre
        # evenement <Motion> suivant).
        self._destroy_tooltip_window()
        self._tooltip = tk.Toplevel(self.tree)
        self._tooltip.wm_overrideredirect(True)
        try:
            self._tooltip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        ttk.Label(
            self._tooltip, text=text, background=opl_theme.couleur("surbrillance"), foreground=opl_theme.couleur("texte"),
            relief="solid", borderwidth=1, padding=(6, 3), wraplength=520, justify="left",
        ).pack()

    def _position(self, event):
        if self._tooltip is None:
            return
        x = self.tree.winfo_rootx() + event.x + 16
        y = self.tree.winfo_rooty() + event.y + 16
        try:
            self._tooltip.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def _destroy_tooltip_window(self):
        if self._tooltip is not None:
            try:
                self._tooltip.destroy()
            except tk.TclError:
                pass
            self._tooltip = None

    def _hide(self, event=None):
        self._destroy_tooltip_window()
        self._last_key = None


class OrganizerGUI(tk.Tk):
    def __init__(self):
        # Avant toute creation de fenetre (audit D8/E1) : voir la docstring
        # de _set_app_user_model_id pour la raison de cet ordre.
        _set_app_user_model_id()
        super().__init__()
        opl_theme.apply(self, "DownloadOrganizer")
        self.title("Nettoyeur intelligent - Telechargements")
        self._apply_window_icon()
        # Taille par defaut mesuree empiriquement pour que les boutons
        # d'action, au moins un onglet (Apercu/Historique) et la barre de
        # statut soient visibles des le premier lancement sur un ecran
        # 1920x1080 standard - le contenu complet reclame ~1123x1100px
        # (winfo_reqwidth()/winfo_reqheight()), ce qui depasse la hauteur
        # utile d'un ecran 1080p ; 1150x850 affiche tout l'essentiel sans
        # deborder de l'ecran, le reste (categories personnalisees, onglets)
        # restant accessible par ascenseur/redimensionnement. minsize
        # respecte la largeur minimale requise (~1123px) pour eviter que la
        # mise en page horizontale ne perturbe l'agencement vertical.
        self.geometry("1320x850")
        self.minsize(1300, 700)


        self.config_data = load_config()
        self.organizer = DownloadOrganizer(self.config_data)
        self.last_real_batch = None

        # Etat du mode Veille : jamais active automatiquement au demarrage,
        # meme si un intervalle est enregistre - l'utilisateur doit l'activer
        # explicitement a chaque session, pour eviter toute surprise.
        self.watch_active = False
        self._watch_after_id = None
        self._watch_last_signature = None
        self._stale_review_after_id = None
        # _busy : verrou general couvrant tout appel a plan()/execute() en
        # arriere-plan (Simuler, Ranger les fichiers OU un cycle de Mode
        # Veille) - empeche qu'un double-clic ou un raccourci clavier
        # (Ctrl+Entree/F5, non desactives par l'etat des boutons) ne
        # relance un second traitement pendant qu'un premier tourne encore
        # sur son propre thread. _watch_busy est le pendant specifique au
        # Mode Veille : un cycle de veille en cours ne doit jamais faire
        # progresser _busy/desactiver les boutons Simuler/Ranger (la veille
        # doit rester une tache de fond non intrusive), mais un cycle de
        # veille ne doit jamais non plus se chevaucher avec un autre cycle
        # de veille ni avec une action manuelle en cours (les deux
        # pourraient deplacer les memes fichiers en meme temps). Cet
        # invariant est applique dans les deux sens : _watch_tick refuse de
        # demarrer un nouveau cycle si self._busy est actif, ET
        # _simulate/_run_real refusent de demarrer si self._watch_busy est
        # actif (correctif audit A14 - avant ce correctif, seul le premier
        # sens etait garde, un declenchement manuel pendant un cycle de
        # veille en cours n'etait pas bloque).
        self._busy = False
        self._watch_busy = False
        self._action_after_id = None
        self._update_check_after_id = None

        self._build_widgets()
        self._refresh_history_view()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Verification silencieuse au demarrage, jamais bloquante : ne
        # propose jamais de suppression automatique, se contente d'informer
        # si "A verifier"/"Doublons" accumulent des fichiers a trier.
        # L'id est garde dans le meme attribut que celui du polling qui
        # suivra (_poll_stale_review_queue) : a tout instant, cet attribut
        # reflete le prochain callback en attente pour ce flux, ce qui
        # permet a _on_close de l'annuler proprement quel que soit l'etat
        # d'avancement (delai initial ou polling en cours).
        self._stale_review_after_id = self.after(200, self._check_stale_review_folders)

    def _apply_window_icon(self) -> None:
        """Icone de fenetre (audit D8/E1) : `app.wm_iconbitmap()` renvoyait
        auparavant une chaine vide (icone Tk generique par defaut, "plume").
        Purement cosmetique : un echec ici (icone manquante, format non
        supporte...) ne doit jamais empecher la fenetre de s'ouvrir - d'ou le
        `try/except tk.TclError` autour de l'appel."""
        icon_path = _resolve_icon_path()
        if icon_path is None:
            return
        try:
            self.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Construction de l'UI
    # ------------------------------------------------------------------

    def _make_scrollable_frame(self, parent):
        """Enveloppe un Canvas+Scrollbar verticale standard autour d'un
        ttk.Frame interne, retourne (canvas, inner_frame) : `inner_frame` est
        le parent a utiliser pour tout widget devant defiler si son contenu
        depasse la hauteur disponible (voir onglet "Reglages avances",
        correctif audit D3 - a la taille minimale de la fenetre, ces reglages
        ne doivent plus jamais ecraser la zone de resultats, ils doivent
        simplement devenir defilables)."""
        canvas = tk.Canvas(parent, highlightthickness=0)
        vscroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(window_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Molette active uniquement pendant que le curseur survole cette zone
        # precise : eviter un bind_all() permanent qui capturerait aussi la
        # molette au-dessus des autres Treeview (Apercu/Historique).
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        return canvas, inner

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

        # Boutons d'action, repartis sur deux rangees : un seul rang de 6
        # boutons deborde de la fenetre par defaut (largeur fixe 880px) et
        # tronque/cache les derniers boutons plutot que de les faire passer
        # a la ligne (ttk.Frame + pack(side="left") ne wrap jamais). Ces
        # boutons sont packes juste apres les chemins essentiels et AVANT le
        # Notebook de resultats (voir plus bas) : contrairement aux reglages
        # avances (Exclusions/Categories/Veille, deplaces dans leur propre
        # onglet du Notebook - voir correctif audit D1/D3), ils doivent
        # rester visibles en permanence quel que soit l'onglet actif.
        btn_frame_1 = ttk.Frame(self, padding=(10, 0))
        btn_frame_1.pack(fill="x")
        btn_frame_2 = ttk.Frame(self, padding=(10, 4))
        btn_frame_2.pack(fill="x")

        ttk.Button(btn_frame_1, text="Enregistrer la configuration (Ctrl+S)", command=self._save_config).pack(side="left")
        # underline=0 (audit D6) : mnemonique clavier standard Windows
        # (Alt+lettre souligne, sans avoir a passer par la souris) sur les
        # trois boutons d'action les plus importants - voir _bind_shortcuts
        # pour les liaisons Alt+S/Alt+R/Alt+A correspondantes. L'application
        # n'ayant pas de barre de menu, ces trois lettres sont libres de
        # tout conflit.
        self.simulate_btn = ttk.Button(btn_frame_1, text="Simuler (F5)", command=self._simulate, underline=0)
        self.simulate_btn.pack(side="left", padx=6)
        self.run_btn = ttk.Button(
            btn_frame_1, text="Ranger les fichiers (Ctrl+Entree)", command=self._run_real, underline=0
        )
        self.run_btn.pack(side="left")
        ttk.Button(
            btn_frame_1, text="Annuler le dernier rangement (Ctrl+Z)", command=self._undo, underline=0,
        ).pack(side="left", padx=6)
        ttk.Button(btn_frame_1, text="Annulation selective...", command=self._selective_undo_dialog).pack(side="left")

        ttk.Button(btn_frame_2, text="Ouvrir le dossier de destination", command=self._open_target_dir).pack(side="left")
        self.export_btn = ttk.Button(btn_frame_2, text="Exporter le rapport (HTML)", command=self._export_report, state="disabled")
        self.export_btn.pack(side="left", padx=6)
        ttk.Button(btn_frame_2, text="Exporter la configuration...", command=self._export_config_dialog).pack(side="left", padx=6)
        ttk.Button(btn_frame_2, text="Importer une configuration...", command=self._import_config_dialog).pack(side="left")

        # Barre de statut, packee AVANT le notebook (avec side="bottom") pour
        # reserver sa place en bas de la fenetre : le notebook ci-dessous
        # utilise fill="both", expand=True et, si on le packe en premier, il
        # engloutit tout l'espace restant du "cavity" de pack() des que le
        # contenu total depasse la hauteur de la fenetre - ne laissant plus
        # aucune place a un widget packe apres lui, meme avec fill="x" sans
        # expand. Empaqueter la barre de statut en premier lui garantit sa
        # hauteur naturelle quelle que soit la taille de la fenetre (bug
        # trouve a l'audit : la barre de statut restait invisible/ecrasee a
        # 1px meme en agrandissant la fenetre, tant que l'ordre n'etait pas
        # corrige).
        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill="x", side="bottom")
        ttk.Label(bottom_bar, text=f"v{APP_VERSION}", foreground=opl_theme.couleur("texte_doux")).pack(side="left", padx=(8, 0), pady=4)
        self.update_status_var = tk.StringVar(value="")
        self.update_status_label = ttk.Label(bottom_bar, textvariable=self.update_status_var, foreground=opl_theme.couleur("texte_doux"))
        self.update_status_label.pack(side="left", padx=(6, 0), pady=4)
        self.status_var = tk.StringVar(value="Pret.")
        ttk.Label(bottom_bar, textvariable=self.status_var, style="Statut.TLabel", anchor="w").pack(
            fill="x", side="left", expand=True
        )
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground=opl_theme.couleur("lien"), cursor="hand2")
        donate_label.pack(side="right", padx=8)
        donate_label.bind("<Button-1>", lambda event: webbrowser.open(DONATE_URL))

        # Notebook: apercu + historique + reglages avances. Priorite donnee a
        # la zone de resultats (correctif audit D1/D3) : seuls les chemins
        # essentiels et les boutons d'action restent en permanence au-dessus
        # de ce Notebook - les reglages consultes/modifies occasionnellement
        # (Exclusions personnalisees, Categories personnalisees, Mode
        # Veille) sont regroupes dans leur propre onglet "Reglages avances"
        # plutot que d'occuper en permanence ~500px de hauteur au-dessus du
        # tableau d'apercu. Consequence directe : a la taille par defaut, le
        # tableau d'apercu dispose de nettement plus de lignes visibles sans
        # defilement ; a la taille minimale de la fenetre (minsize), le
        # Notebook - et donc l'apercu/l'historique - reste visible au lieu de
        # disparaitre entierement (avant correctif, le contenu au-dessus du
        # Notebook depassait a lui seul la hauteur minimale de la fenetre).
        # UNE SEULE COLONNE a gauche : entete() rend le rail lui-meme —
        # marque en haut, vues au milieu, Theme et Aide en bas.
        notebook = opl_theme.entete(
            self, "Nettoyeur intelligent", "Rangement des telechargements",
            on_contact=lambda: opl_contact.ouvrir(self, app="Nettoyeur intelligent", version=APP_VERSION),
            slug="downloadorganizer", version=APP_VERSION)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        # Conserve en attribut d'instance (audit K3) : sans cela, il etait
        # impossible d'acceder au Notebook (ex: changer d'onglet
        # programmatiquement) depuis l'exterieur de _build_widgets sans
        # parcourir manuellement l'arbre de widgets (winfo_children()
        # recursif) - reduit la testabilite et empeche de futures
        # fonctionnalites simples comme basculer automatiquement vers
        # l'onglet Historique apres un rangement reel.
        self.notebook = notebook

        preview_frame = ttk.Frame(notebook)
        self._preview_frame = preview_frame
        notebook.add(preview_frame, text="Apercu")

        # Barre de recherche : filtre en temps reel les lignes de l'Apercu au
        # fil de la frappe (insensible a la casse, sur toutes les colonnes).
        # Les donnees completes restent conservees dans self._preview_rows ;
        # seul l'affichage est reduit aux lignes correspondantes (voir
        # _apply_preview_filter). Packee AVANT le Treeview pour rester en haut.
        preview_search_bar = ttk.Frame(preview_frame)
        preview_search_bar.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(preview_search_bar, text="Rechercher :").pack(side="left")
        self._preview_rows = []
        self.preview_search_var = tk.StringVar(value="")
        ttk.Entry(preview_search_bar, textvariable=self.preview_search_var).pack(
            side="left", fill="x", expand=True, padx=(6, 6)
        )
        self.preview_count_var = tk.StringVar(value="")
        ttk.Label(
            preview_search_bar, textvariable=self.preview_count_var,
            foreground=opl_theme.couleur("texte_doux"),
        ).pack(side="right")
        self.preview_search_var.trace_add("write", lambda *_: self._apply_preview_filter())

        columns = ("fichier", "categorie", "destination", "raison")
        self.preview_tree = ttk.Treeview(preview_frame, columns=columns, show="headings", height=15)
        for col, label, width in [
            ("fichier", "Fichier", 200),
            ("categorie", "Categorie", 110),
            ("destination", "Destination", 300),
            # Colonne la plus large par defaut (correctif audit D2) : c'est
            # elle qui affiche le message de securite le plus important de
            # l'application ("extension incoherente avec le contenu reel du
            # fichier..."), auparavant la plus etroite (160px) alors qu'elle
            # contient le texte le plus long. Complete par une infobulle au
            # survol (voir _TreeviewCellTooltip ci-dessous) qui affiche le
            # texte integral quelle que soit la largeur de colonne.
            ("raison", "Raison", 260),
        ]:
            self.preview_tree.heading(col, text=label)
            self.preview_tree.column(col, width=width, anchor="w")
        self.preview_tree.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview_tree.yview)
        self.preview_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(fill="y", side="right")
        # Infobulle sur la colonne "Raison" (correctif audit D2) : la donnee
        # stockee dans le Treeview a toujours ete integrale, seul l'affichage
        # etait tronque sans aucune indication qu'il manquait du texte.
        self._preview_reason_tooltip = _TreeviewCellTooltip(self.preview_tree, ["raison"])

        history_frame = ttk.Frame(notebook)
        notebook.add(history_frame, text="Historique")

        hist_toolbar = ttk.Frame(history_frame)
        hist_toolbar.pack(fill="x", padx=4, pady=(4, 0))
        self.history_summary_var = tk.StringVar(value="")
        ttk.Label(hist_toolbar, textvariable=self.history_summary_var, foreground=opl_theme.couleur("texte_doux")).pack(side="left")
        ttk.Button(hist_toolbar, text="Purger l'historique...", command=self._purge_history_dialog).pack(side="right")
        ttk.Button(hist_toolbar, text="Annuler le lot selectionne...", command=self._undo_selected_batch).pack(
            side="right", padx=(0, 6)
        )

        # Barre de recherche de l'Historique : meme principe que l'Apercu.
        # Les lignes completes (avec leur iid = index absolu du lot, essentiel
        # pour l'annulation/le detail) sont conservees dans self._history_rows ;
        # le filtre ne fait que reafficher le sous-ensemble correspondant sans
        # jamais perdre les iid (voir _apply_history_filter).
        hist_search_bar = ttk.Frame(history_frame)
        hist_search_bar.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Label(hist_search_bar, text="Rechercher :").pack(side="left")
        self._history_rows = []
        self.history_search_var = tk.StringVar(value="")
        ttk.Entry(hist_search_bar, textvariable=self.history_search_var).pack(
            side="left", fill="x", expand=True, padx=(6, 6)
        )
        self.history_count_var = tk.StringVar(value="")
        ttk.Label(
            hist_search_bar, textvariable=self.history_count_var,
            foreground=opl_theme.couleur("texte_doux"),
        ).pack(side="right")
        self.history_search_var.trace_add("write", lambda *_: self._apply_history_filter())

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
        self.history_tree.bind("<Double-1>", lambda event: self._show_batch_detail())

        # Onglet "Reglages avances" : regroupe les sections modifiees
        # seulement occasionnellement (Exclusions personnalisees, Categories
        # personnalisees, Mode Veille), enveloppees dans un Canvas defilable
        # (voir _make_scrollable_frame) pour ne jamais ecraser le Notebook
        # meme si leur contenu total depasse la hauteur disponible a la
        # taille minimale de la fenetre.
        settings_frame = ttk.Frame(notebook)
        notebook.add(settings_frame, text="Reglages avances")
        _settings_canvas, settings_inner = self._make_scrollable_frame(settings_frame)

        # Exclusions
        excl_frame = ttk.LabelFrame(settings_inner, text="Exclusions personnalisees", padding=10)
        excl_frame.pack(fill="x", padx=10, pady=(10, 10))

        ttk.Label(excl_frame, text="Extensions (separees par des virgules, ex: .tmp,.log)").grid(row=0, column=0, sticky="w")
        self.excl_ext_var = tk.StringVar(value=", ".join(self.config_data["exclusions"].get("extensions", [])))
        ttk.Entry(excl_frame, textvariable=self.excl_ext_var, width=40).grid(row=0, column=1, sticky="we", padx=5)

        ttk.Label(excl_frame, text="Noms de fichiers exacts (separes par des virgules)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.excl_names_var = tk.StringVar(value=", ".join(self.config_data["exclusions"].get("filenames", [])))
        ttk.Entry(excl_frame, textvariable=self.excl_names_var, width=40).grid(row=1, column=1, sticky="we", padx=5, pady=(4, 0))

        ttk.Label(
            excl_frame,
            text="Motifs additionnels (glob, ex: *.bak) - s'ajoutent aux protections integrees (*.crdownload, "
            "*.part, *.tmp, desktop.ini, *.download, *.opdownload, *.!ut, *.!qb, *.lnk), qui restent toujours actives",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.excl_patterns_var = tk.StringVar(value=", ".join(self.config_data["exclusions"].get("patterns", [])))
        ttk.Entry(excl_frame, textvariable=self.excl_patterns_var, width=40).grid(row=2, column=1, sticky="we", padx=5, pady=(4, 0))

        excl_frame.columnconfigure(1, weight=1)

        # Categories personnalisees (au-dela des categories integrees :
        # PDF, Images, Archives, Installateurs, Videos, Audio - celles-ci
        # gardent leur comportement integre - dont, pour les quatre
        # premieres, la detection par signature de fichier - et ne sont
        # pas modifiables ici)
        cat_frame = ttk.LabelFrame(settings_inner, text="Categories personnalisees", padding=10)
        cat_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.custom_categories = copy.deepcopy(self.config_data.get("custom_categories", []))

        cat_columns = ("name", "extensions", "patterns", "target")
        self.categories_tree = ttk.Treeview(cat_frame, columns=cat_columns, show="headings", height=4)
        for col, label, width in [
            ("name", "Nom", 120), ("extensions", "Extensions", 160),
            ("patterns", "Motifs de nom", 160), ("target", "Dossier cible", 180),
        ]:
            self.categories_tree.heading(col, text=label)
            self.categories_tree.column(col, width=width, anchor="w")
        self.categories_tree.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, 6))
        self._refresh_categories_tree()

        ttk.Label(cat_frame, text="Nom").grid(row=1, column=0, sticky="w")
        self.new_category_name_var = tk.StringVar()
        ttk.Entry(cat_frame, textvariable=self.new_category_name_var, width=16).grid(row=2, column=0, sticky="we", padx=(0, 5))
        ttk.Label(cat_frame, text="Extensions (ex: .mp3,.flac)").grid(row=1, column=1, sticky="w")
        self.new_category_ext_var = tk.StringVar()
        ttk.Entry(cat_frame, textvariable=self.new_category_ext_var, width=20).grid(row=2, column=1, sticky="we", padx=5)
        ttk.Label(cat_frame, text="Motifs de nom (optionnel, ex: facture*.pdf)").grid(row=1, column=2, sticky="w")
        self.new_category_patterns_var = tk.StringVar()
        ttk.Entry(cat_frame, textvariable=self.new_category_patterns_var, width=20).grid(row=2, column=2, sticky="we", padx=5)
        ttk.Label(cat_frame, text="Dossier cible (relatif a la destination racine)").grid(row=1, column=3, sticky="w")
        self.new_category_target_var = tk.StringVar()
        ttk.Entry(cat_frame, textvariable=self.new_category_target_var, width=18).grid(row=2, column=3, sticky="we", padx=5)
        ttk.Button(cat_frame, text="Ajouter", command=self._add_custom_category).grid(row=2, column=4, sticky="w", padx=(5, 0))
        ttk.Button(cat_frame, text="Supprimer la categorie selectionnee", command=self._remove_custom_category).grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            cat_frame,
            text="Un motif de nom (ex: facture*.pdf, Capture*.png) est prioritaire sur l'extension, "
            "y compris celle d'une categorie integree - laissez vide pour un tri par extension seule.",
            foreground=opl_theme.couleur("texte_doux"),
        ).grid(row=4, column=0, columnspan=5, sticky="w", pady=(4, 0))
        cat_frame.columnconfigure(1, weight=1)
        cat_frame.columnconfigure(2, weight=1)
        cat_frame.columnconfigure(3, weight=1)

        # Mode Veille (surveillance optionnelle)
        watch_frame = ttk.LabelFrame(settings_inner, text="Mode Veille (optionnel)", padding=10)
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
        ttk.Label(watch_frame, textvariable=self.watch_status_var, foreground=opl_theme.couleur("texte_doux")).grid(
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
            foreground=opl_theme.couleur("texte_doux"),
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # Mises a jour (audit F2) : l'appel reseau de verification (requete
        # HTTPS GET anonyme vers l'API GitHub, voir update_checker.py)
        # n'etait ni documente ni desactivable - cette case a cocher permet
        # de l'opter-out pour un usage strictement hors-ligne/air-gapped, en
        # plus de la mention explicite dans le README.
        update_frame = ttk.LabelFrame(settings_inner, text="Mises a jour", padding=10)
        update_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.check_updates_var = tk.BooleanVar(value=bool(self.config_data.get("check_for_updates", True)))
        ttk.Checkbutton(
            update_frame,
            text="Verifier les mises a jour au demarrage",
            variable=self.check_updates_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            update_frame,
            text=(
                "Effectue une requete anonyme (aucune donnee personnelle) vers l'API GitHub publique "
                "pour savoir si une nouvelle version est disponible. Decochez pour un usage strictement "
                "hors ligne - le reste de l'application fonctionne entierement sans connexion Internet."
            ),
            wraplength=760,
            foreground=opl_theme.couleur("texte_doux"),
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        # L'apercu reste l'onglet actif par defaut a l'ouverture (deja le cas
        # de fait puisque c'est le premier onglet ajoute, mais explicite ici
        # pour rester correct meme si l'ordre d'ajout des onglets change un
        # jour) : c'est l'ecran le plus important, celui qui doit rester
        # prioritaire (correctif audit D1).
        notebook.select(preview_frame)

        self._update_check_queue = queue.Queue()
        if self.config_data.get("check_for_updates", True):
            update_checker.start_update_check(APP_VERSION, UPDATE_REPO, self._update_check_queue)
            self._update_check_after_id = self.after(500, self._poll_update_check)
        else:
            self.update_status_var.set("Verification desactivee (Reglages avances)")

    def _poll_update_check(self):
        try:
            status, tag = self._update_check_queue.get_nowait()
        except queue.Empty:
            self._update_check_after_id = self.after(500, self._poll_update_check)
            return
        self._update_check_after_id = None
        if status == "update_available":
            self.update_status_var.set(f"Mise a jour disponible : {tag} - Telecharger")
            self.update_status_label.configure(foreground=opl_theme.couleur("lien"), cursor="hand2")
            # Le clic : si la verification est passee par le flux d'Open Projects
            # Lab, le binaire est telecharge ICI et son empreinte SHA-256 verifiee
            # avant d'etre montre (jamais execute) ; sinon, la page GitHub comme
            # avant. Tout vit dans update_checker, identique dans les sept apps.
            self.update_status_label.bind("<Button-1>", lambda event: update_checker.ouvrir_mise_a_jour(
                UPDATE_REPO, RELEASES_URL, self.update_status_var, self.after))
        elif status == "up_to_date":
            self.update_status_var.set("A jour")
            self.update_status_label.configure(foreground=opl_theme.couleur("succes"), cursor="")
        # "check_failed" (hors ligne, GitHub inaccessible...) : on ne
        # revendique rien plutot que d'afficher a tort "a jour".

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _run_worker(
        self, work, on_success, on_error=None, poll_interval_ms=100, track_after_id=None,
        progress_queue: "Optional[queue.Queue]" = None, on_progress=None,
    ):
        """Execute `work` (callable sans argument) sur un thread separe, puis
        rappelle on_success(resultat) - ou on_error(exception) si `work` a
        leve - sur le thread PRINCIPAL Tk, via self.after(). Meme pattern
        que _check_stale_review_folders/_poll_stale_review_queue (deja en
        place pour le scan des dossiers "A verifier"/"Doublons" au
        demarrage) : thread + queue.Queue + polling par self.after(), pour
        ne jamais bloquer l'interface pendant plan()/execute() (qui
        peuvent prendre plusieurs secondes sur de gros volumes, domines
        par la detection de doublons).

        Aucun widget ni etat partage (self.organizer, self.last_real_batch,
        la config...) n'est touche depuis le thread de fond : `work` ne
        doit lire/ecrire que des objets qui lui sont propres (voir les
        organizers "snapshot", construits a partir d'une copie profonde de
        la config, utilises par _simulate/_run_real/_watch_tick) ; seul
        on_success/on_error, execute sur le thread principal apres depot
        du resultat dans la queue, a le droit d'appliquer quoi que ce soit
        aux widgets ou a l'etat de l'application.

        `track_after_id`, si fourni, est rappele a chaque (re)programmation
        d'un self.after() avec l'id courant (ou None une fois le resultat
        traite) : cela permet a l'appelant de stocker cet id dans son
        propre attribut (ex: self._action_after_id, self._watch_after_id)
        afin de pouvoir l'annuler proprement depuis _on_close si la fenetre
        est fermee pendant que le traitement tourne encore.

        `progress_queue`/`on_progress` (correctif audit B2) : si les deux
        sont fournis, chaque tuple depose dans `progress_queue` par `work`
        (execute sur le thread de fond - voir DownloadOrganizer.plan()/
        execute(progress_callback=...)) est relaye a `on_progress(*tuple)`
        sur le thread PRINCIPAL, a chaque tick de polling. Seul le DERNIER
        tuple depose depuis le tick precedent est relaye (la queue est
        entierement videe a chaque poll) : sur un tres gros lot, `work` peut
        deposer une progression bien plus vite que le polling ne la
        consomme - inutile d'empiler puis de rejouer chaque valeur
        intermediaire, seul l'etat le plus recent importe pour un simple
        indicateur visuel."""
        result_queue: "queue.Queue" = queue.Queue()

        def worker():
            try:
                value = work()
            except Exception as exc:  # noqa: BLE001 - garde-fou du thread de
                # fond : une exception inattendue doit remonter au thread
                # principal via la queue (pour y etre affichee proprement),
                # jamais traverser un thread secondaire en silence.
                result_queue.put(("error", exc))
            else:
                result_queue.put(("ok", value))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            if progress_queue is not None and on_progress is not None:
                latest = None
                try:
                    while True:
                        latest = progress_queue.get_nowait()
                except queue.Empty:
                    pass
                if latest is not None:
                    on_progress(*latest)
            try:
                status, payload = result_queue.get_nowait()
            except queue.Empty:
                after_id = self.after(poll_interval_ms, poll)
                if track_after_id is not None:
                    track_after_id(after_id)
                return
            if track_after_id is not None:
                track_after_id(None)
            if status == "error":
                if on_error is not None:
                    on_error(payload)
                else:
                    raise payload
            else:
                on_success(payload)

        after_id = self.after(poll_interval_ms, poll)
        if track_after_id is not None:
            track_after_id(after_id)

    def _set_busy(self, busy: bool, message: str = None):
        """Active/desactive l'indicateur "traitement en cours" pour les
        actions manuelles (Simuler / Ranger les fichiers) : desactive leurs
        boutons declencheurs (pour eviter un double-declenchement pendant
        qu'un plan()/execute() tourne deja sur un thread de fond) et, en
        option, met a jour la barre de statut."""
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.simulate_btn.configure(state=state)
        self.run_btn.configure(state=state)
        if message is not None:
            self.status_var.set(message)

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

        try:
            watch_interval = int(self.watch_interval_var.get().strip())
            if watch_interval < 5:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Valeur invalide",
                "L'intervalle de veille doit etre un nombre entier d'au moins 5 secondes.",
            )
            return None

        self.config_data["downloads_dir"] = self.downloads_var.get().strip()
        self.config_data["base_target_dir"] = self.base_target_var.get().strip()
        self.config_data["old_file_threshold_days"] = age_days
        # Sans cette ligne, modifier l'intervalle de veille puis "Enregistrer la
        # configuration" (Ctrl+S) SANS jamais cliquer "Activer la veille" perdait
        # silencieusement la nouvelle valeur : seul _start_watch l'ecrivait dans
        # config_data (bug trouve a l'audit).
        self.config_data["watch_interval_seconds"] = watch_interval
        self.config_data["exclusions"] = {
            "extensions": split_csv(self.excl_ext_var.get()),
            "filenames": split_csv(self.excl_names_var.get()),
            "patterns": split_csv(self.excl_patterns_var.get()),
        }
        self.config_data["custom_categories"] = copy.deepcopy(self.custom_categories)
        self.config_data["check_for_updates"] = bool(self.check_updates_var.get())
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
        self.custom_categories = copy.deepcopy(config.get("custom_categories", []))
        self._refresh_categories_tree()
        self.check_updates_var.set(bool(config.get("check_for_updates", True)))
        self.config_data = config
        self.organizer = DownloadOrganizer(config)

    def _refresh_categories_tree(self):
        self.categories_tree.delete(*self.categories_tree.get_children())
        for index, cat in enumerate(self.custom_categories):
            self.categories_tree.insert("", "end", iid=str(index), values=(
                cat.get("name", ""), ", ".join(cat.get("extensions", [])),
                ", ".join(cat.get("name_patterns", [])), cat.get("target", ""),
            ))

    def _add_custom_category(self):
        name = self.new_category_name_var.get().strip()
        target = self.new_category_target_var.get().strip()
        extensions = [
            e.strip() if e.strip().startswith(".") else f".{e.strip()}"
            for e in self.new_category_ext_var.get().split(",") if e.strip()
        ]
        name_patterns = [p.strip() for p in self.new_category_patterns_var.get().split(",") if p.strip()]
        if not name or not target or not extensions:
            messagebox.showwarning(
                "Categorie invalide", "Le nom, les extensions et le dossier cible sont tous obligatoires.",
            )
            return
        if name in DEFAULT_CATEGORIES or any(c.get("name") == name for c in self.custom_categories):
            messagebox.showwarning(
                "Categorie invalide",
                f"'{name}' est deja utilise (categorie integree ou deja ajoutee). Choisissez un autre nom.",
            )
            return
        self.custom_categories.append({
            "name": name, "extensions": extensions, "target": target, "name_patterns": name_patterns,
        })
        self.new_category_name_var.set("")
        self.new_category_ext_var.set("")
        self.new_category_patterns_var.set("")
        self.new_category_target_var.set("")
        self._refresh_categories_tree()

    def _remove_custom_category(self):
        selection = self.categories_tree.selection()
        if not selection:
            messagebox.showinfo("Supprimer une categorie", "Selectionnez une categorie d'abord.")
            return
        del self.custom_categories[int(selection[0])]
        self._refresh_categories_tree()

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

    def _montrer_rien_a_ranger(self) -> None:
        """Le dossier est deja range : on le DIT dans la vue, on n'ouvre rien."""
        self._effacer_etat_vide()
        self.preview_tree.delete(*self.preview_tree.get_children())
        cadre = opl_theme.etat_vide(
            self._preview_frame,
            "Rien a ranger pour le moment",
            "Votre dossier Telechargements est deja en ordre : aucun fichier ne "
            "correspond a une categorie a deplacer. Revenez apres quelques "
            "telechargements, ou activez la veille pour que ce soit fait tout seul.")
        cadre.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._etat_vide = cadre

    def _effacer_etat_vide(self) -> None:
        cadre = getattr(self, "_etat_vide", None)
        if cadre is not None:
            cadre.destroy()
            self._etat_vide = None

    def _fill_preview(self, result):
        # Conserve la totalite des lignes en memoire ; l'affichage effectif
        # (eventuellement filtre par la barre de recherche) est delegue a
        # _apply_preview_filter pour ne jamais dupliquer la logique d'insertion.
        self._preview_rows = [
            (move.source.name, move.category, str(move.destination), move.reason)
            for move in result.moves
        ]
        self._apply_preview_filter()

    def _apply_preview_filter(self):
        """Reaffiche l'Apercu en ne gardant que les lignes correspondant au
        texte recherche (insensible a la casse, sur n'importe quelle colonne).
        Champ vide => toutes les lignes."""
        query = self.preview_search_var.get().strip().lower()
        self.preview_tree.delete(*self.preview_tree.get_children())
        shown = 0
        for values in self._preview_rows:
            if not query or query in " ".join(str(v) for v in values).lower():
                self.preview_tree.insert("", "end", values=values)
                shown += 1
        total = len(self._preview_rows)
        if total == 0:
            self.preview_count_var.set("")
        elif query:
            self.preview_count_var.set(f"{shown}/{total} resultats")
        else:
            self.preview_count_var.set(f"{total} ligne(s)")

    def _simulate(self):
        # Garde-fou contre un double-declenchement : le bouton est desactive
        # pendant le traitement, mais le raccourci F5 (bind_all, independant
        # de l'etat du bouton) pourrait sinon relancer un second plan() en
        # parallele du premier.
        if self._busy:
            return
        if self._watch_busy:
            # Un cycle de Mode Veille (plan() ou execute() d'un lot confirme)
            # est en cours. La veille ne desactive jamais les boutons
            # Simuler/Ranger (voir _watch_tick, elle doit rester une tache de
            # fond non intrusive) : sans ce garde-fou explicite, rien
            # n'empecherait un second plan()/execute() de demarrer en
            # parallele sur le meme dossier et de tenter de deplacer les
            # memes fichiers en meme temps (correctif audit A14).
            self.status_var.set(
                "Un cycle de Mode Veille est en cours, reessayez dans quelques instants."
            )
            return
        config = self._collect_config()
        if config is None:
            return
        # Intentionnellement non persiste : "Simuler" sert a tester des valeurs
        # sans les enregistrer definitivement. Utilisez "Enregistrer la
        # configuration" ou "Ranger les fichiers" pour persister.
        #
        # Important : on utilise un organizer temporaire, construit a partir
        # d'une COPIE PROFONDE de la config, sans toucher a self.organizer.
        # Si le Mode Veille tourne en arriere-plan, il continue d'utiliser la
        # configuration active (sauvegardee) - un simple "Simuler" ne doit
        # jamais changer silencieusement son comportement. La copie profonde
        # est indispensable maintenant que plan()/execute() s'executent sur
        # un thread separe : sans elle, ce thread lirait le meme dict que
        # self.config_data, que le thread principal pourrait muter entre
        # temps (Ctrl+S, Activer la veille...) pendant que le calcul tourne
        # encore - une mutation d'etat partage non synchronisee.
        preview_organizer = DownloadOrganizer(copy.deepcopy(config))

        self._set_busy(True, "Simulation en cours...")

        # Indicateur de progression (audit B2) : sans lui, rien ne
        # distinguait a l'ecran un traitement long en cours d'un blocage
        # reel - seuls les boutons desactives le laissaient supposer. Le
        # thread de fond depose ("Analyse"/"Simulation du rangement", fait,
        # total) dans cette queue au fil de plan()/execute() ; le thread
        # principal (voir _run_worker) la relaie dans la barre de statut.
        progress_queue: "queue.Queue" = queue.Queue()

        def work():
            result = preview_organizer.plan(
                progress_callback=lambda done, total: progress_queue.put(("Analyse", done, total))
            )
            if not result.errors:
                preview_organizer.execute(
                    result, simulate=True,
                    progress_callback=lambda done, total: progress_queue.put(("Simulation du rangement", done, total)),
                )
            return result

        def on_progress(label, done, total):
            self.status_var.set(f"{label} en cours... ({done}/{total} fichier(s) traite(s))")

        def on_success(result):
            self._set_busy(False)
            if result.errors:
                messagebox.showerror("Erreur", "\n".join(f"{p}: {e}" for p, e in result.errors))
                return
            self._fill_preview(result)
            duplicates = [m for m in result.moves if m.category == DUPLICATES_TARGET]
            extra = f", {len(result.skipped_dirs)} sous-dossier(s) non parcouru(s)" if result.skipped_dirs else ""
            extra += (
                f", {len(result.skipped_symlinks)} lien(s) symbolique(s) ignore(s)"
                if result.skipped_symlinks
                else ""
            )
            dup_note = f", dont {len(duplicates)} doublon(s) de contenu detecte(s)" if duplicates else ""
            self.status_var.set(
                f"Simulation : {len(result.moves)} fichier(s) seraient deplaces{dup_note}, "
                f"{len(result.excluded)} ignore(s){extra}. Aucun fichier n'a ete modifie."
            )

        def on_error(exc):
            self._set_busy(False)
            messagebox.showerror("Erreur", f"La simulation a echoue : {exc}")

        self._run_worker(
            work, on_success, on_error,
            track_after_id=lambda after_id: setattr(self, "_action_after_id", after_id),
            progress_queue=progress_queue, on_progress=on_progress,
        )

    def _run_real(self):
        # Meme garde-fou que _simulate (raccourci Ctrl+Entree independant de
        # l'etat du bouton).
        if self._busy:
            return
        if self._watch_busy:
            # Meme garde-fou que _simulate contre un plan()/execute() lance
            # en parallele d'un cycle de Mode Veille en cours (correctif
            # audit A14) : voir le commentaire detaille dans _simulate.
            self.status_var.set(
                "Un cycle de Mode Veille est en cours, reessayez dans quelques instants."
            )
            return
        config = self._collect_config()
        if config is None:
            return
        save_config(config)
        # Snapshot isole (copie profonde de la config) pour le thread de
        # fond, meme raison que dans _simulate : le thread ne doit jamais
        # partager de dict mutable avec self.config_data. self.organizer
        # n'est reaffecte que depuis le thread PRINCIPAL, une fois le
        # traitement entierement termine (voir on_execute_success) - jamais
        # depuis le thread de fond.
        worker_organizer = DownloadOrganizer(copy.deepcopy(config))

        self._set_busy(True, "Analyse en cours...")

        # Indicateur de progression (audit B2), meme principe que _simulate :
        # une queue distincte par phase (analyse, puis rangement une fois
        # confirme), chacune relayee via son propre on_progress dans la
        # barre de statut.
        plan_progress_queue: "queue.Queue" = queue.Queue()

        def plan_work():
            return worker_organizer.plan(
                progress_callback=lambda done, total: plan_progress_queue.put((done, total))
            )

        def on_plan_progress(done, total):
            self.status_var.set(f"Analyse en cours... ({done}/{total} fichier(s) traite(s))")

        def on_plan_success(result):
            self._set_busy(False)
            if result.errors:
                messagebox.showerror("Erreur", "\n".join(f"{p}: {e}" for p, e in result.errors))
                return

            if not result.moves:
                # Une fenetre modale pour annoncer qu'il ne s'est RIEN passe
                # est le pire des deux mondes : elle interrompt, elle masque
                # la vue, et il faut la fermer pour retrouver un ecran vide.
                # L'information vit desormais dans la vue elle-meme.
                self._montrer_rien_a_ranger()
                return

            self._effacer_etat_vide()
            self._fill_preview(result)

            duplicates = [m for m in result.moves if m.category == DUPLICATES_TARGET]
            dup_line = (
                f"Dont {len(duplicates)} doublon(s) de contenu identique, ranges a part dans "
                f"'{DUPLICATES_TARGET}' (rien n'est jamais supprime).\n"
                if duplicates else ""
            )
            confirm = opl_theme.dialogue(
                self, "Confirmer le rangement",
                f"{len(result.moves)} fichier(s) vont etre deplaces (aucune suppression).\n"
                f"{dup_line}"
                "Vous pourrez annuler ce lot via le bouton 'Annuler le dernier rangement'.\n\n"
                "Continuer ?",
                confirmer="Ranger ces fichiers")
            if not confirm:
                return

            self._set_busy(True, "Rangement en cours...")

            execute_progress_queue: "queue.Queue" = queue.Queue()

            def execute_work():
                return worker_organizer.execute(
                    result, simulate=False,
                    progress_callback=lambda done, total: execute_progress_queue.put((done, total)),
                )

            def on_execute_progress(done, total):
                self.status_var.set(f"Rangement en cours... ({done}/{total} fichier(s) traite(s))")

            def on_execute_success(batch):
                self._set_busy(False)
                self.organizer = worker_organizer
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

            def on_execute_error(exc):
                self._set_busy(False)
                messagebox.showerror("Erreur", f"Le rangement a echoue : {exc}")

            self._run_worker(
                execute_work, on_execute_success, on_execute_error,
                track_after_id=lambda after_id: setattr(self, "_action_after_id", after_id),
                progress_queue=execute_progress_queue, on_progress=on_execute_progress,
            )

        def on_plan_error(exc):
            self._set_busy(False)
            messagebox.showerror("Erreur", f"L'analyse a echoue : {exc}")

        self._run_worker(
            plan_work, on_plan_success, on_plan_error,
            track_after_id=lambda after_id: setattr(self, "_action_after_id", after_id),
            progress_queue=plan_progress_queue, on_progress=on_plan_progress,
        )

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
        if opl_theme.dialogue(
            self, "Rapport exporte",
            f"Rapport enregistre dans :\n{path}\n\nL'ouvrir maintenant ?",
            confirmer="Ouvrir le rapport"):
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
        self._watch_busy = False
        if self._watch_after_id is not None:
            try:
                self.after_cancel(self._watch_after_id)
            except (ValueError, tk.TclError):
                pass
            self._watch_after_id = None
        self.watch_btn.configure(text="Activer la veille")
        self.watch_status_var.set(status_message)

    def _watch_tick(self):
        """Un cycle de veille : plan() (potentiellement plusieurs secondes
        sur de gros volumes, domine par la detection de doublons) est
        deporte sur un thread separe via _run_worker, exactement comme
        _check_stale_review_folders - le Mode Veille est cense etre une
        tache de fond non intrusive, elle ne doit jamais geler l'interface,
        ni meme desactiver les boutons Simuler/Ranger (self._busy n'est
        jamais touche ici)."""
        if not self.watch_active:
            return

        if self._watch_busy or self._busy:
            # Un cycle de veille precedent (ou une action manuelle Simuler/
            # Ranger) est encore en cours : on ne lance jamais un second
            # plan()/execute() en parallele (deux traitements pourraient
            # deplacer les memes fichiers en meme temps). On se contente de
            # reessayer un peu plus tard, sans jamais bloquer ni interrompre
            # la veille.
            self._watch_after_id = self.after(1000, self._watch_tick)
            return

        self._watch_busy = True
        # Snapshot isole (copie profonde de la config), meme raison que dans
        # _simulate/_run_real : le thread de fond ne doit jamais lire
        # self.organizer.config en direct, potentiellement mute entre-temps
        # par une autre action sur le thread principal (Ctrl+S, "Ranger les
        # fichiers"...).
        worker_organizer = DownloadOrganizer(copy.deepcopy(self.organizer.config))

        def work():
            return worker_organizer.plan()

        def on_success(result):
            self._watch_busy = False
            if not self.watch_active:
                # La veille a ete desactivee (ou la fenetre fermee) pendant
                # que ce cycle tournait encore en arriere-plan : on ignore ce
                # resultat desormais perime plutot que de reactiver un etat
                # ou re-planifier un tick.
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
                self._prompt_watch_batch(worker_organizer, result)
            else:
                self._watch_last_signature = signature
                if result.moves:
                    self.watch_status_var.set(
                        f"Veille active : {len(result.moves)} fichier(s) detecte(s), "
                        "stabilisation en cours (attente de fin de telechargement)..."
                    )
                else:
                    self.watch_status_var.set("Veille active : aucun nouveau fichier a ranger pour le moment.")
                self._schedule_next_watch_tick()

        def on_error(exc):
            self._watch_busy = False
            if not self.watch_active:
                return
            # toute exception inattendue doit arreter proprement la veille et
            # le signaler, plutot que de laisser la veille geler
            # silencieusement pour toujours (plus aucun tick reprogramme).
            self._stop_watch(f"Veille interrompue (erreur inattendue) : {exc}")

        self._run_worker(
            work, on_success, on_error,
            track_after_id=lambda after_id: setattr(self, "_watch_after_id", after_id),
        )

    def _schedule_next_watch_tick(self):
        if not self.watch_active:
            return
        try:
            interval = int(self.watch_interval_var.get().strip())
            if interval < 5:
                raise ValueError
        except ValueError:
            # Champ modifie en cours de route avec une valeur invalide : on
            # garde la derniere cadence connue plutot que d'interrompre la veille.
            interval = self.organizer.config.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS)
        self._watch_after_id = self.after(interval * 1000, self._watch_tick)

    def _prompt_watch_batch(self, worker_organizer, result):
        self._fill_preview(result)
        duplicates = [m for m in result.moves if m.category == DUPLICATES_TARGET]
        dup_line = f"Dont {len(duplicates)} doublon(s) de contenu identique.\n" if duplicates else ""
        confirm = opl_theme.dialogue(
            self, "Veille : nouveaux fichiers detectes",
            f"{len(result.moves)} fichier(s) sont prets a etre ranges (aucune suppression).\n"
            f"{dup_line}"
            "Ranger maintenant ?",
            confirmer="Ranger maintenant")
        if not confirm:
            self.watch_status_var.set(
                "Veille active : rangement ignore pour ce lot. Il sera reproposé au prochain "
                "controle stable (meme si le dossier ne change pas d'ici la)."
            )
            self._schedule_next_watch_tick()
            return

        self._watch_busy = True

        def work():
            return worker_organizer.execute(result, simulate=False)

        def on_success(batch):
            self._watch_busy = False
            if not self.watch_active:
                return
            # Le traitement (plan() + execute()) est entierement termine :
            # c'est le seul moment ou le thread principal reaffecte
            # self.organizer, jamais le thread de fond lui-meme.
            self.organizer = worker_organizer
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
            self._schedule_next_watch_tick()

        def on_error(exc):
            self._watch_busy = False
            if not self.watch_active:
                return
            self._stop_watch(f"Veille interrompue (erreur inattendue) : {exc}")

        self._run_worker(
            work, on_success, on_error,
            track_after_id=lambda after_id: setattr(self, "_watch_after_id", after_id),
        )

    def _check_stale_review_folders(self):
        """Verification silencieuse au demarrage : informe sans jamais
        bloquer ni proposer de suppression si "A verifier"/"Doublons"
        accumulent des fichiers plus vieux que le seuil configure.

        Le scan (parcours recursif + stat() de chaque fichier, voir
        organizer.scan_stale_review_folders) peut prendre plusieurs
        secondes si ces dossiers contiennent des milliers de fichiers :
        execute directement ici, il gelait tout le thread principal Tk
        (donc toute l'appli) au demarrage le temps du scan (bug trouve a
        l'audit). Deporte sur un thread separe, avec le resultat remonte
        via une queue.Queue interrogee par polling depuis self.after() -
        meme pattern que le scan en arriere-plan de PhotoTri - pour ne
        jamais bloquer l'interface."""
        # Le callback self.after() qui a mene a cet appel a deja ete
        # consomme : plus rien n'est en attente tant que le polling
        # ci-dessous n'a pas reprogramme sa propre echeance.
        self._stale_review_after_id = None
        organizer = self.organizer
        result_queue: "queue.Queue" = queue.Queue()

        def worker():
            try:
                stale = organizer.scan_stale_review_folders()
            except Exception:  # noqa: BLE001 - garde-fou du thread de fond :
                # une exception non attrapee ici tuerait le thread sans
                # jamais rien deposer dans la queue, ce qui laisserait le
                # polling ci-dessous tourner indefiniment sans jamais se
                # resoudre. Ce scan est purement informatif au demarrage :
                # en cas d'echec, on se contente de ne rien signaler.
                stale = []
            result_queue.put(stale)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_stale_review_queue(result_queue)

    def _poll_stale_review_queue(self, result_queue: "queue.Queue"):
        try:
            stale = result_queue.get_nowait()
        except queue.Empty:
            self._stale_review_after_id = self.after(150, self._poll_stale_review_queue, result_queue)
            return
        self._stale_review_after_id = None
        self._show_stale_review_result(stale)

    def _show_stale_review_result(self, stale: list) -> None:
        if not stale:
            return
        labels = {"A verifier": "A verifier", "Doublons": "Doublons"}
        lines = []
        for entry in stale:
            size_mo = entry["total_bytes"] / (1024 * 1024)
            lines.append(
                f"- {labels.get(entry['folder'], entry['folder'])} : {entry['count']} fichier(s) "
                f"({size_mo:.1f} Mo)"
            )
        messagebox.showinfo(
            "Fichiers a trier",
            "Ces dossiers contiennent des fichiers anciens a trier manuellement "
            "(aucune suppression automatique n'est effectuee) :\n\n" + "\n".join(lines),
        )

    def _on_close(self):
        self._stop_watch()
        # Annule le polling du scan de demarrage s'il est encore en cours
        # (dossiers "A verifier"/"Doublons" tres volumineux, fermeture
        # rapide de l'appli) : sans cela, self.after() tente encore
        # d'invoquer une methode liee a une fenetre deja detruite.
        if self._stale_review_after_id is not None:
            try:
                self.after_cancel(self._stale_review_after_id)
            except (ValueError, tk.TclError):
                pass
            self._stale_review_after_id = None
        # Meme precaution pour le polling d'une simulation/d'un rangement
        # reel (Simuler/Ranger les fichiers) encore en cours au moment de la
        # fermeture : les threads de fond eux-memes sont daemon (ils
        # s'arretent avec le processus), mais le self.after() qui les
        # interroge doit etre annule explicitement pour ne jamais tenter
        # d'invoquer une methode liee a une fenetre deja detruite.
        if self._action_after_id is not None:
            try:
                self.after_cancel(self._action_after_id)
            except (ValueError, tk.TclError):
                pass
            self._action_after_id = None
        # Meme precaution pour le polling de la verification de mise a jour
        # (queue.Queue interrogee via self.after() tant qu'elle est vide) :
        # sans annulation, ce callback peut se declencher apres la
        # destruction de la fenetre et provoquer une erreur Tcl "invalid
        # command name" sur stderr.
        if self._update_check_after_id is not None:
            try:
                self.after_cancel(self._update_check_after_id)
            except (ValueError, tk.TclError):
                pass
            self._update_check_after_id = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _undo(self):
        confirm = opl_theme.dialogue(
            self, "Annuler le dernier rangement",
            "Voulez-vous vraiment annuler le dernier lot de deplacements reels "
            "(les fichiers seront remis dans le dossier Telechargements) ?",
            confirmer="Annuler le rangement")
        if not confirm:
            return

        out = self.organizer.undo_last_batch()
        self._show_undo_outcome(out)

    def _show_undo_outcome(self, out: dict):
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

    def _undo_selected_batch(self):
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("Annuler un lot", "Selectionnez d'abord un lot dans l'historique.")
            return
        confirm = opl_theme.dialogue(
            self, "Annuler le lot selectionne",
            "Voulez-vous vraiment annuler ce lot de deplacements "
            "(les fichiers seront remis dans le dossier Telechargements) ?",
            confirmer="Annuler ce lot")
        if not confirm:
            return
        # L'iid de la ligne EST l'index absolu du lot dans history.json
        # (voir _refresh_history_view) : les cas invalides (lot simule, deja
        # annule, purge entre-temps) sont refuses par undo_batch_at avec un
        # message, jamais traites silencieusement.
        out = self.organizer.undo_batch_at(int(selection[0]))
        self._show_undo_outcome(out)

    def _show_batch_detail(self):
        """Ouvre le detail d'un lot de l'historique (double-clic) : chaque
        fichier traite avec sa categorie, sa destination, sa raison et son
        statut reel - y compris pour un lot simule ou (partiellement)
        annule, puisque export_html_report accepte deja n'importe quel
        dict de lot et affiche le statut reel de chaque entree plutot que
        de pretendre que tout a ete "Deplace"."""
        selection = self.history_tree.selection()
        if not selection:
            return
        # Meme mapping iid -> index absolu que _undo_selected_batch : la
        # position visuelle ne correspond jamais au bon lot une fois
        # l'affichage inverse/tronque (voir _refresh_history_view).
        history = load_history()
        index = int(selection[0])
        if index < 0 or index >= len(history):
            messagebox.showinfo("Detail du lot", "Ce lot n'existe plus dans l'historique.")
            return
        batch = history[index]

        dialog = tk.Toplevel(self)
        dialog.title(f"Detail du lot - {batch.get('timestamp', '')}")
        dialog.transient(self)
        dialog.grab_set()
        # Correctif audit D4 : les 6 colonnes declarees ci-dessous totalisent
        # 940px, qui, avec la scrollbar verticale (~20px) et les marges du
        # Treeview (padx=10 de chaque cote), depassaient largement les 820px
        # de la geometrie d'origine - la colonne "Erreur" (la plus a droite,
        # celle qui explique POURQUOI un deplacement a echoue) n'etait alors
        # pas du tout visible sans redimensionnement manuel. minsize evite
        # aussi qu'un redimensionnement involontaire ne fasse redisparaitre
        # ces colonnes.
        dialog.geometry("1170x460")
        dialog.minsize(1170, 320)

        tree_frame = ttk.Frame(dialog)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(10, 5))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        columns = ("fichier", "categorie", "destination", "raison", "statut", "erreur")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=15)
        for col, label, width in [
            ("fichier", "Fichier", 140),
            ("categorie", "Categorie", 90),
            ("destination", "Destination", 260),
            ("raison", "Raison", 220),
            ("statut", "Statut", 90),
            ("erreur", "Erreur", 140),
        ]:
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        vscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        vscroll.grid(row=0, column=1, sticky="ns")
        # Scrollbar horizontale (correctif audit D4, filet de securite en
        # plus de l'elargissement du dialogue ci-dessus) : si une future
        # colonne/un futur libelle plus long faisait a nouveau deborder le
        # total des largeurs, l'utilisateur pourrait toujours acceder au
        # contenu masque plutot que de le perdre silencieusement.
        hscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        # Infobulles (correctif audit D2/D4) : "Raison" et "Erreur" peuvent
        # toutes deux contenir un message plus long que leur colonne, meme
        # apres l'elargissement ci-dessus (ex: un message [WinError ...]
        # detaille).
        self._batch_detail_tooltip = _TreeviewCellTooltip(tree, ["raison", "erreur"])

        for move in batch["moves"]:
            status_label = BATCH_STATUS_LABELS.get(move.get("status"), move.get("status", ""))
            # .get() plutot que move["source"] (bug trouve a l'audit -
            # regression du motif deja corrige dans _attempt_restore_entry
            # pour tolerer une entree d'historique malformee/editee a la
            # main) : un acces par crochets faisait planter la boucle en
            # silence (Tk avale l'exception), laissant un dialogue ouvert
            # sans aucun bouton, recuperable uniquement via la croix OS.
            tree.insert("", "end", values=(
                Path(move.get("source", "")).name, move.get("category", ""), move.get("destination", ""),
                move.get("reason", ""), status_label, move.get("error", ""),
            ))

        def export_this_batch():
            reports_dir = APP_DIR / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            timestamp_slug = batch.get("timestamp", time.strftime("%Y%m%d-%H%M%S")).replace(":", "").replace(" ", "-")
            # Le timestamp d'un lot n'a qu'une precision a la seconde : deux
            # lots partageant le meme timestamp (historique edite a la
            # main, ou tres rarement deux lots reels dans la meme seconde)
            # ecraseraient sinon silencieusement le rapport HTML l'un de
            # l'autre (bug releve a l'audit). Suffixe numerote des qu'un
            # fichier de ce nom existe deja.
            path = reports_dir / f"rapport-{timestamp_slug}.html"
            counter = 1
            while path.exists():
                path = reports_dir / f"rapport-{timestamp_slug} ({counter}).html"
                counter += 1
            base_dir = Path(self.base_target_var.get().strip()) if self.base_target_var.get().strip() else None
            export_html_report(batch, path, base_dir=base_dir)
            if opl_theme.dialogue(
                dialog, "Rapport exporte",
                f"Rapport enregistre dans :\n{path}\n\nL'ouvrir maintenant ?",
                confirmer="Ouvrir le rapport"):
                os.startfile(str(path))

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Exporter ce rapport (HTML)", command=export_this_batch).pack(side="left")
        ttk.Button(buttons, text="Fermer", command=dialog.destroy).pack(side="right")

    def _selective_undo_dialog(self):
        history = load_history()
        real_batches = [b for b in history if not b.get("simulated") and not b.get("undone")]
        if not real_batches:
            messagebox.showinfo("Annulation selective", "Aucun lot a annuler.")
            return
        last_batch = real_batches[-1]
        moved_entries = [e for e in last_batch["moves"] if e.get("status") == "deplace"]
        if not moved_entries:
            messagebox.showinfo("Annulation selective", "Aucun fichier du dernier lot n'est disponible pour annulation.")
            return

        dialog = tk.Toplevel(self)
        dialog.title("Annulation selective")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Selectionnez les fichiers a remettre dans Telechargements :").pack(
            anchor="w", padx=10, pady=(10, 5)
        )

        list_frame = ttk.Frame(dialog)
        list_frame.pack(fill="both", expand=True, padx=10)
        listbox = tk.Listbox(list_frame, selectmode="multiple", height=min(15, len(moved_entries)), width=80)
        for entry in moved_entries:
            listbox.insert("end", f"{Path(entry['source']).name}  ->  {entry['destination']}")
        listbox.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        scroll.pack(fill="y", side="right")

        def on_confirm():
            selected_indices = listbox.curselection()
            if not selected_indices:
                messagebox.showinfo("Annulation selective", "Selectionnez au moins un fichier.")
                return
            sources = [moved_entries[i]["source"] for i in selected_indices]
            dialog.destroy()
            out = self.organizer.undo_selected_files(sources)
            undone = out.get("undone", [])
            errors = out.get("errors", [])
            if undone:
                self.last_real_batch = None
                self.export_btn.configure(state="disabled")
            self.status_var.set(f"{len(undone)} fichier(s) restaure(s), {len(errors)} erreur(s).")
            if errors:
                messagebox.showwarning("Annulation partielle", "\n".join(f"{p}: {e}" for p, e in errors))
            self._refresh_history_view()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=10)
        ttk.Button(buttons, text="Restaurer la selection", command=on_confirm).pack(side="left")
        ttk.Button(buttons, text="Annuler", command=dialog.destroy).pack(side="left", padx=6)

    HISTORY_DISPLAY_LIMIT = 200

    def _refresh_history_view(self):
        history = load_history()
        total = len(history)
        # N'affiche que les entrees les plus recentes : au-dela de quelques
        # centaines de lots, peupler tout le Treeview d'un coup ralentirait
        # l'interface pour peu d'interet (l'historique reste consultable en
        # totalite via export/purge, juste pas tout affiche a l'ecran).
        displayed = list(reversed(history))[: self.HISTORY_DISPLAY_LIMIT]
        # Chaque ligne est memorisee sous forme (iid, values) pour que la barre
        # de recherche puisse reafficher un sous-ensemble sans jamais perdre
        # l'iid (index absolu du lot, cf. plus bas) dont dependent l'annulation
        # et l'affichage du detail.
        self._history_rows = []
        for offset, batch in enumerate(displayed):
            if batch.get("simulated"):
                mode = "Simulation"
            elif batch.get("undone"):
                mode = "Annule"
            elif any(m.get("status") == "annule" for m in batch["moves"]):
                mode = "Partiellement annule"
            else:
                mode = "Reel"
            moved = [m for m in batch["moves"] if m.get("status") in ("deplace", "planifie")]
            # .get() plutot que m["source"] (meme correctif que dans
            # _show_batch_detail/export_html_report) : une entree malformee
            # ne doit jamais faire planter le rafraichissement de tout
            # l'onglet Historique.
            detail = ", ".join(Path(m.get("source", "")).name for m in moved[:5])
            if len(moved) > 5:
                detail += f", ... (+{len(moved) - 5})"
            # L'iid de chaque ligne est l'index ABSOLU du lot dans history.json :
            # l'affichage etant inverse et tronque a HISTORY_DISPLAY_LIMIT, une
            # position visuelle ne permettrait jamais de retrouver le bon lot
            # au moment d'agir dessus (annulation, detail).
            self._history_rows.append(
                (str(total - 1 - offset), (batch["timestamp"], mode, len(moved), detail))
            )

        if total > len(displayed):
            self.history_summary_var.set(f"Affichage des {len(displayed)} lots les plus recents sur {total} au total.")
        else:
            self.history_summary_var.set(f"{total} lot(s) dans l'historique.")

        self._apply_history_filter()

    def _apply_history_filter(self):
        """Reaffiche l'Historique en ne gardant que les lots correspondant au
        texte recherche (insensible a la casse, sur n'importe quelle colonne).
        Les iid sont preserves pour ne pas casser annulation/detail."""
        query = self.history_search_var.get().strip().lower()
        self.history_tree.delete(*self.history_tree.get_children())
        shown = 0
        for iid, values in self._history_rows:
            if not query or query in " ".join(str(v) for v in values).lower():
                self.history_tree.insert("", "end", iid=iid, values=values)
                shown += 1
        if query and self._history_rows:
            self.history_count_var.set(f"{shown}/{len(self._history_rows)} resultats")
        else:
            self.history_count_var.set("")

    def _purge_history_dialog(self):
        from tkinter import simpledialog

        total = len(load_history())
        if total == 0:
            messagebox.showinfo("Purger l'historique", "L'historique est deja vide.")
            return
        keep = simpledialog.askinteger(
            "Purger l'historique",
            f"L'historique contient {total} lot(s).\n"
            "Combien des plus recents souhaitez-vous conserver ? (0 pour tout effacer)",
            initialvalue=total, minvalue=0, maxvalue=total, parent=self,
        )
        if keep is None:
            return
        if not opl_theme.dialogue(
            self, "Purger l'historique",
            f"{total - keep} lot(s) seront definitivement retires de l'historique.\n"
            "Les fichiers deja deplaces sur le disque ne sont pas affectes, mais l'annulation de "
            "ces lots ne sera plus possible depuis l'application. Continuer ?",
            confirmer="Purger l'historique", danger=True):
            return
        removed = purge_history(keep_last=keep)
        self._refresh_history_view()
        self.status_var.set(f"{removed} lot(s) retire(s) de l'historique.")

    def _bind_shortcuts(self):
        self.bind_all("<Control-s>", lambda e: self._save_config())
        self.bind_all("<F5>", lambda e: self._simulate())
        self.bind_all("<Control-Return>", lambda e: self._run_real())
        self.bind_all("<Control-z>", lambda e: self._undo())
        # Mnemoniques clavier Alt+lettre (audit D6), en complement des
        # raccourcis globaux ci-dessus - voir underline=0 sur les boutons
        # correspondants dans _build_widgets. Tk ne convertit pas de
        # lui-meme underline= en une liaison clavier active (contrairement a
        # une entree de menu) : sans ce bind explicite, la lettre soulignee
        # ne serait qu'un indice visuel non fonctionnel.
        self.bind_all("<Alt-s>", lambda e: self._simulate())
        self.bind_all("<Alt-r>", lambda e: self._run_real())
        self.bind_all("<Alt-a>", lambda e: self._undo())


def run_gui():
    app = OrganizerGUI()
    app.mainloop()


if __name__ == "__main__":
    run_gui()

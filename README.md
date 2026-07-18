# Nettoyeur intelligent du dossier Téléchargements

Outil qui analyse le dossier `Téléchargements` et range automatiquement les
fichiers par catégorie :

- **PDF** → `Documents/PDF`
- **Images** → `Images`
- **Archives** → `Archives`
- **Installateurs** → `Installateurs`
- **Fichiers anciens** (type non reconnu, plus vieux qu'un seuil configurable) → `A verifier`

Aucune suppression automatique n'est effectuée : uniquement des déplacements,
tous réversibles.

## Démarrage rapide

Double-cliquez sur **[`Lancer.vbs`](Lancer.vbs)** : la fenêtre de l'application
s'ouvre directement, sans terminal ni ligne de commande. C'est le moyen le
plus simple de lancer l'outil au quotidien — voir [Lancement
rapide](#lancement-rapide-double-clic) pour créer un raccourci Bureau.

## Fonctionnalités

- **Mode simulation** : prévisualise les déplacements sans toucher aux fichiers.
- **Historique des déplacements** : chaque lot réel est enregistré dans
  `~/.download_organizer/history.json`.
- **Bouton Annuler** : restaure le dernier lot de déplacements réels vers son
  emplacement d'origine.
- **Exclusions personnalisées** : par extension, par nom de fichier exact, ou
  par motif glob additionnel (ex. `*.bak`).
- **Protections intégrées toujours actives** : `*.crdownload`, `*.part`,
  `*.tmp`, `desktop.ini`, `*.download` sont exclus en permanence, même si le
  champ « Motifs » est vidé.
- **Aucune suppression automatique** : les déplacements ne remplacent jamais
  un fichier déjà présent à la destination (ni à l'aller, ni lors d'une
  annulation) — en cas de conflit, ce fichier est simplement laissé de côté
  et signalé.

## Installation

Aucune dépendance externe : Python 3.9+ avec Tkinter (inclus dans les
installations standard de Python sous Windows).

```bash
python -m venv .venv
.venv\Scripts\activate
```

## Utilisation

### Lancement rapide (double-clic)

Double-cliquez simplement sur **[`Lancer.vbs`](Lancer.vbs)** : la fenêtre de
l'application s'ouvre directement, sans console. Aucune installation ni
commande à taper.

Pour un accès encore plus rapide, créez un raccourci sur le Bureau :

1. Clic droit sur `Lancer.vbs` → **Envoyer vers** → **Bureau (créer un
   raccourci)**.
2. (Optionnel) Renommez le raccourci, par exemple « Nettoyeur Téléchargements ».
3. (Optionnel) Clic droit sur le raccourci → **Propriétés** → **Changer
   d'icône...** pour lui donner une icône personnalisée.

Vous pouvez aussi épingler ce raccourci à la barre des tâches ou au menu
Démarrer pour un accès en un clic.

Si `Lancer.vbs` ne fonctionne pas (Python introuvable, etc.), utilisez
**[`Lancer.bat`](Lancer.bat)** à la place : il ouvre une console qui affiche
les éventuelles erreurs, utile pour diagnostiquer un problème.

### Lancer depuis Python (sans le raccourci)

```bash
python organizer.py --gui
```

Ou directement :

```bash
python gui.py
```

L'interface permet de :

1. Choisir le dossier `Téléchargements` et le dossier de destination racine.
2. Définir le seuil d'ancienneté (en jours) pour les fichiers non reconnus.
3. Définir des exclusions personnalisées.
4. Cliquer sur **Simuler** pour prévisualiser, ou **Ranger les fichiers** pour
   exécuter réellement les déplacements.
5. Cliquer sur **Annuler le dernier rangement** pour tout remettre en place.
6. Consulter l'onglet **Historique** pour voir les lots précédents.

### Ligne de commande

```bash
# Simulation (aucun fichier modifié)
python organizer.py

# Exécution réelle
python organizer.py --run

# Annuler le dernier lot de déplacements réels
python organizer.py --undo

# Spécifier un dossier Téléchargements différent pour cette execution
# uniquement (non enregistre dans la configuration)
python organizer.py --downloads-dir "D:\Autre\Telechargements" --run
```

## Configuration

La configuration (dossiers, seuil d'ancienneté, exclusions) est stockée dans
`~/.download_organizer/config.json` et peut être modifiée directement ou via
l'interface graphique.

## Structure du projet

```
organizer.py   # logique métier + CLI
gui.py         # interface graphique Tkinter
Lancer.vbs     # raccourci de lancement double-clic (sans console)
Lancer.bat     # raccourci de lancement double-clic (avec console, pour debug)
README.md
```

## Soutenir le projet

Cet outil est gratuit et open source. S'il vous fait gagner du temps et que
vous avez envie d'offrir un café, c'est toujours très apprécié :

☕ **[ko-fi.com/yoshines62000](https://ko-fi.com/yoshines62000)**

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

## Fonctionnalités

- **Mode simulation** : prévisualise les déplacements sans toucher aux fichiers.
- **Historique des déplacements** : chaque lot réel est enregistré dans
  `~/.download_organizer/history.json`.
- **Bouton Annuler** : restaure le dernier lot de déplacements réels vers son
  emplacement d'origine.
- **Exclusions personnalisées** : par extension, par nom de fichier exact, ou
  par motif glob (ex. `*.crdownload`, `*.part`).
- **Aucune suppression automatique.**

## Installation

Aucune dépendance externe : Python 3.9+ avec Tkinter (inclus dans les
installations standard de Python sous Windows).

```bash
python -m venv .venv
.venv\Scripts\activate
```

## Utilisation

### Lancement rapide (double-clic)

Double-cliquez simplement sur **`Lancer.vbs`** : la fenêtre de l'application
s'ouvre directement, sans console. Vous pouvez créer un raccourci vers ce
fichier sur le Bureau pour un accès encore plus rapide (clic droit sur
`Lancer.vbs` → Envoyer vers → Bureau (créer un raccourci)).

Si `Lancer.vbs` ne fonctionne pas (Python introuvable, etc.), utilisez
`Lancer.bat` à la place : il ouvre une console qui affiche les éventuelles
erreurs.

### Interface graphique en ligne de commande

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

# Spécifier un dossier Téléchargements différent
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
README.md
```

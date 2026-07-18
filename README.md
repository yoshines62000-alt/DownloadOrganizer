# Nettoyeur intelligent du dossier Téléchargements

Outil qui analyse le dossier `Téléchargements` et range automatiquement les
fichiers par catégorie :

- **PDF** → `Documents/PDF`
- **Images** → `Images`
- **Archives** → `Archives`
- **Installateurs** → `Installateurs`
- **Fichiers anciens** (type non reconnu, plus vieux qu'un seuil configurable) → `A verifier`
- **Doublons de contenu** (détectés par hash, pas seulement par nom) → `Doublons`

Aucune suppression automatique n'est effectuée : uniquement des déplacements,
tous réversibles.

## Démarrage rapide

Double-cliquez sur **[`Lancer.vbs`](Lancer.vbs)** : la fenêtre de l'application
s'ouvre directement, sans terminal ni ligne de commande. C'est le moyen le
plus simple de lancer l'outil au quotidien — voir [Lancement
rapide](#lancement-rapide-double-clic) pour créer un raccourci Bureau.

## Fonctionnalités

- **Mode Veille (optionnel)** : surveille le dossier Téléchargements en
  arrière-plan à intervalle configurable, mais **ne déplace jamais rien
  automatiquement**. Dès que de nouveaux fichiers sont détectés et *stables*
  (taille et date de modification inchangées depuis la dernière vérification
  — donc plus aucun téléchargement en cours), une confirmation groupée vous
  est proposée, exactement comme un rangement manuel. Contrairement à des
  outils comme Hazel ou DropIt qui déplacent les fichiers instantanément et
  silencieusement dès leur détection, vous gardez toujours la main. La veille
  n'est jamais activée automatiquement au démarrage — c'est un choix
  explicite à chaque session.
- **Reconnaissance par signature de fichier (magic bytes)**, en plus de la
  simple extension : l'outil lit les premiers octets de chaque fichier pour
  vérifier son type réel. Deux usages concrets :
  - un fichier sans extension (ou avec une extension inconnue) dont le
    contenu est reconnu (PDF, image, archive, exécutable) est quand même
    classé correctement ;
  - un fichier dont l'**extension ne correspond pas au contenu réel**
    (ex. un `.exe` renommé en `.pdf` — technique classique pour déguiser un
    exécutable) est isolé dans `A verifier` avec une raison explicite, au
    lieu d'être classé aveuglément sur la seule foi de son extension.
- **Détection de doublons par hash de contenu (SHA-256)**, pas seulement par
  nom de fichier : un même document téléchargé deux fois par le navigateur
  (`rapport.pdf` et `rapport (1).pdf`, contenu identique) est reconnu comme
  doublon même si les noms diffèrent. Les doublons sont rangés à part dans
  `Doublons` plutôt que dupliqués sous un nom numéroté — l'outil ne supprime
  jamais rien, à vous de trier ce dossier. La détection couvre deux cas :
  doublons entre fichiers du même lot, et doublon d'un fichier déjà rangé
  lors d'un run précédent (uniquement en cas de collision de nom, pour rester
  rapide même sur un gros dossier déjà trié).
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
  et signalé. Cette garantie est vérifiée par une suite de tests automatisés
  (voir [Tests](#tests)), pas seulement affirmée.
- **Rapport de session exportable (HTML)** : après un rangement réel, un
  bouton permet d'exporter un rapport détaillant chaque fichier traité, sa
  destination et la raison du classement — pour vérifier ou auditer ce que
  l'outil a fait.
- **Robustesse face aux configurations corrompues** : un `config.json` ou
  `history.json` invalide est mis en quarantaine (renommé, pas perdu) plutôt
  que de faire planter l'application.
- **Journal d'activité** (`~/.download_organizer/app.log`) pour diagnostiquer
  un problème.
- **Raccourcis clavier** : `Ctrl+S` (enregistrer), `F5` (simuler),
  `Ctrl+Entrée` (ranger), `Ctrl+Z` (annuler).

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
6. Cliquer sur **Ouvrir le dossier de destination** pour vérifier le résultat
   dans l'Explorateur, ou sur **Exporter le rapport (HTML)** pour garder une
   trace détaillée d'un rangement réel.
7. Activer le **Mode Veille** pour une surveillance périodique du dossier,
   avec confirmation groupée avant chaque rangement (voir
   [Fonctionnalités](#fonctionnalités)).
8. Consulter l'onglet **Historique** pour voir les lots précédents.

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
l'interface graphique. L'historique des lots réels est dans
`~/.download_organizer/history.json`, les rapports exportés dans
`~/.download_organizer/reports/`, et le journal d'activité dans
`~/.download_organizer/app.log`.

## Créer un exécutable autonome (.exe)

Pour distribuer l'outil sans que le destinataire ait besoin d'installer
Python, un exécutable Windows autonome peut être généré avec
[PyInstaller](https://pyinstaller.org/) :

```bash
python -m pip install pyinstaller
python -m PyInstaller NettoyeurTelechargements.spec
```

L'exécutable est produit dans `dist/NettoyeurTelechargements.exe` (~11 Mo,
fichier unique, sans console). Le fichier `.spec` du dépôt fixe la
configuration de build (mode fenêtré, un seul fichier) pour un résultat
reproductible — pas besoin de refaire `pyinstaller gui.py` à la main.

Les dossiers `build/` et `dist/` générés par PyInstaller ne sont pas suivis
par Git (voir `.gitignore`) : à regénérer localement à chaque fois.

## Tests

Une suite de tests automatisés couvre en priorité la garantie centrale de
l'outil (aucun écrasement de fichier, à l'aller comme lors d'une annulation),
ainsi que les cas limites (dossiers manquants/vides, configuration corrompue,
exclusions mal formées, collisions de destination).

```bash
python -m unittest tests.test_organizer -v
```

## Structure du projet

```
organizer.py                     # logique métier + CLI
gui.py                           # interface graphique Tkinter
tests/test_organizer.py          # tests automatises
Lancer.vbs                       # raccourci de lancement double-clic (sans console)
Lancer.bat                       # raccourci de lancement double-clic (avec console, pour debug)
NettoyeurTelechargements.spec    # configuration de build PyInstaller (.exe autonome)
README.md
```

## Soutenir le projet

Cet outil est gratuit et open source. S'il vous fait gagner du temps et que
vous avez envie d'offrir un café, c'est toujours très apprécié :

☕ **[ko-fi.com/yoshines62000](https://ko-fi.com/yoshines62000)**

# Nettoyeur intelligent du dossier Téléchargements

[![Dernière version](https://img.shields.io/github/v/release/yoshines62000-alt/DownloadOrganizer?label=derni%C3%A8re%20version)](https://github.com/yoshines62000-alt/DownloadOrganizer/releases/latest)
[![Téléchargements](https://img.shields.io/github/downloads/yoshines62000-alt/DownloadOrganizer/total?label=t%C3%A9l%C3%A9chargements)](https://github.com/yoshines62000-alt/DownloadOrganizer/releases/latest)

**[⬇️ Télécharger l'exécutable (.exe) — aucune installation requise](https://github.com/yoshines62000-alt/DownloadOrganizer/releases/latest)**

Outil qui analyse le dossier `Téléchargements` et range automatiquement les
fichiers par catégorie :

- **PDF** → `Documents/PDF`
- **Images** → `Images`
- **Archives** → `Archives`
- **Installateurs** → `Installateurs`
- **Vidéos** → `Videos`
- **Audio** → `Audio`
- **Fichiers anciens** (type non reconnu, plus vieux qu'un seuil configurable) → `A verifier`
- **Doublons de contenu** (détectés par hash, pas seulement par nom) → `Doublons`

Aucune suppression automatique n'est effectuée : uniquement des déplacements,
tous réversibles.

## Démarrage rapide

1. [**Téléchargez `NettoyeurTelechargements.exe`**](https://github.com/yoshines62000-alt/DownloadOrganizer/releases/latest)
   depuis la dernière release.
2. Double-cliquez dessus : la fenêtre de l'application s'ouvre directement,
   sans installation, sans Python, sans console.

L'exécutable n'étant pas signé numériquement, Windows SmartScreen peut
afficher un avertissement « éditeur non reconnu » au premier lancement :
cliquez sur **Informations complémentaires** puis **Exécuter quand même**.
Si vous préférez éviter cet avertissement ou vérifier vous-même ce que fait
le code avant de l'exécuter, voir [Lancer depuis le code
source](#lancer-depuis-le-code-source) ci-dessous.

### Vérifier l'intégrité du fichier téléchargé (optionnel)

Chaque release GitHub publie, dans ses notes de version, l'empreinte
**SHA-256** de `NettoyeurTelechargements.exe`. Vous pouvez vérifier que le
fichier téléchargé correspond exactement à celui publié par le développeur
(protection contre une altération en transit, une compromission du dépôt, ou
une confusion entre plusieurs versions) avec PowerShell :

```powershell
Get-FileHash .\NettoyeurTelechargements.exe -Algorithm SHA256
```

Comparez la valeur `Hash` affichée avec celle indiquée dans les notes de la
[release correspondante](https://github.com/yoshines62000-alt/DownloadOrganizer/releases).
Si les deux empreintes ne correspondent pas exactement, ne lancez pas le
fichier et retéléchargez-le depuis la page officielle des releases.

### Mon antivirus signale le fichier

Il peut arriver qu'un antivirus (Windows Defender ou un éditeur tiers)
signale `NettoyeurTelechargements.exe` comme suspect, même s'il n'y a rien
de malveillant dedans. Deux raisons expliquent ce faux positif, bien connu
pour ce type d'outil :

- l'exécutable n'est **pas signé numériquement** (voir SmartScreen
  ci-dessus) — les heuristiques antivirus se méfient davantage des binaires
  non signés ;
- l'outil **lit les premiers octets de nombreux fichiers et en déplace en
  masse** en quelques secondes, un profil comportemental que des
  heuristiques génériques associent parfois (à tort) à un rançongiciel.

Le code source est intégralement public dans ce dépôt (voir [Lancer depuis
le code source](#lancer-depuis-le-code-source) pour l'exécuter sans passer
par l'exécutable), et [Vérifier l'intégrité du fichier
téléchargé](#vérifier-lintégrité-du-fichier-téléchargé-optionnel) permet de
confirmer que le fichier obtenu correspond bien à celui publié. Si vous
voulez un second avis, vous pouvez soumettre le fichier à
[VirusTotal](https://www.virustotal.com/) (plusieurs dizaines de moteurs
antivirus) ou signaler le faux positif directement à l'éditeur de votre
antivirus.

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
  `~/.download_organizer/history.jsonl` (format JSONL — une ligne JSON par
  lot). Un ancien fichier `history.json` est migré automatiquement et de
  façon transparente vers ce nouveau format au premier lancement.
- **Annulation, à trois niveaux de granularité** :
  - **Annuler le dernier rangement** : restaure en un clic tous les fichiers
    du dernier lot réel vers leur emplacement d'origine.
  - **Annulation sélective** : choisissez, fichier par fichier, lesquels du
    dernier lot remettre en place — les autres restent rangés, disponibles
    pour une annulation ultérieure.
  - **Annuler le lot sélectionné** (onglet Historique) : annule n'importe
    quel lot passé, pas seulement le plus récent.
- **Détail d'un lot** (double-clic sur une ligne de l'onglet Historique) :
  liste complète des fichiers traités pour ce lot précis (catégorie,
  destination, raison, statut, détail d'une éventuelle erreur), avec un
  bouton pour réexporter le rapport HTML de ce lot en particulier.
- **Export / import de configuration** : sauvegardez vos réglages (chemins,
  exclusions, catégories personnalisées...) dans un fichier JSON pour les
  transférer vers un autre PC ou les restaurer après une réinstallation.
- **Indicateur de mise à jour disponible** en barre de statut, avec lien
  direct vers la dernière release GitHub (voir « Vérification de mise à
  jour » ci-dessous).
- **Exclusions personnalisées** : par extension, par nom de fichier exact, ou
  par motif glob additionnel (ex. `*.bak`).
- **Protections intégrées toujours actives** : `*.crdownload`, `*.part`,
  `*.tmp`, `desktop.ini`, `*.download`, `*.opdownload`, `*.!ut`, `*.!qb`
  (fichiers de téléchargement en cours de Chrome/Edge/Firefox, Opera,
  uTorrent, qBittorrent) et `*.lnk` (raccourcis Windows, potentiellement
  cassés par un déplacement si leur cible est un chemin relatif) sont exclus
  en permanence, même si le champ « Motifs » est vidé.
- **Aucune suppression automatique** : les déplacements ne remplacent jamais
  un fichier déjà présent à la destination (ni à l'aller, ni lors d'une
  annulation) — en cas de conflit, ce fichier est simplement laissé de côté
  et signalé. Un déplacement sur le même disque est vérifié puis effectué de
  façon atomique (`os.rename`), sans fenêtre de risque. Un déplacement entre
  deux disques revérifie l'absence de destination juste avant le renommage
  final (fenêtre de course résiduelle réduite au strict minimum, pas
  totalement nulle). Ce comportement est couvert par une suite de tests
  automatisés (voir [Tests](#tests)), y compris les cas de course
  inter-volume — pas seulement affirmé.
- **Rapport de session exportable (HTML)** : après un rangement réel, un
  bouton permet d'exporter un rapport détaillant chaque fichier traité, sa
  destination et la raison du classement — pour vérifier ou auditer ce que
  l'outil a fait.
- **Robustesse face aux configurations corrompues** : un `config.json`
  invalide est mis en quarantaine (renommé, pas perdu) plutôt que de faire
  planter l'application. Pour `history.jsonl` (une ligne JSON par lot), une
  ligne isolée corrompue est simplement ignorée sans faire perdre le reste de
  l'historique.
- **Journal d'activité** (`~/.download_organizer/app.log`), avec rotation
  automatique (2 fichiers de sauvegarde de 1 Mo maximum chacun, en plus du
  fichier courant) pour ne jamais grossir indéfiniment.
- **Raccourcis clavier** : `Ctrl+S` (enregistrer), `F5` (simuler),
  `Ctrl+Entrée` (ranger), `Ctrl+Z` (annuler), ainsi que des mnémoniques
  `Alt+S` (Simuler), `Alt+R` (Ranger les fichiers) et `Alt+A` (Annuler le
  dernier rangement) pour une navigation clavier sans souris.

### Vérification de mise à jour

Au démarrage de l'interface graphique, l'application effectue une requête
HTTPS **anonyme** (`GET https://api.github.com/repos/.../releases/latest`,
aucune donnée personnelle ni identifiant machine envoyé) pour savoir si une
nouvelle version est disponible. Un échec (hors ligne, GitHub inaccessible)
est silencieux et ne bloque jamais l'application. C'est le **seul** flux
réseau de toute l'application — voir [Vie privée](#vie-privée). Il peut être
désactivé depuis l'onglet **Réglages avancés → Mises à jour** (case à
cocher « Vérifier les mises à jour au démarrage ») pour un usage strictement
hors ligne/air-gapped.

## Vie privée

Aucune télémétrie, aucun compte, aucune donnée envoyée à un serveur autre
que la vérification de mise à jour décrite ci-dessus (désactivable). La
configuration, l'historique des déplacements et le journal d'activité
restent en clair sur votre disque local, dans `~/.download_organizer/`.

## Mes données — lisibles sans l'application

Rien n'est enfermé : la configuration (`~/.download_organizer/config.json`) et
l'historique des lots (`~/.download_organizer/history.jsonl`, une ligne JSON
par lot) sont en texte clair. Si un jour l'exécutable refuse de démarrer, ces
fichiers s'ouvrent dans n'importe quel éditeur — aucun outil de secours n'est
nécessaire, c'est déjà le format ouvert vers lequel un outil exporterait.

## Prise en main de l'interface

Que vous lanciez l'exécutable ou le code source, l'interface est la même et
permet de :

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

## Lancer depuis le code source

Alternative à l'exécutable : utile si vous préférez éviter l'avertissement
Windows SmartScreen, si vous voulez inspecter le code avant de l'exécuter,
ou si vous contribuez au projet. Nécessite Python 3.9+ avec Tkinter (inclus
dans les installations standard de Python sous Windows) — aucune autre
dépendance.

```bash
git clone https://github.com/yoshines62000-alt/DownloadOrganizer.git
cd DownloadOrganizer
```

### Raccourci double-clic

Double-cliquez sur **[`Lancer.vbs`](Lancer.vbs)** : la fenêtre de
l'application s'ouvre directement, sans console.

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

### Ligne de commande

```bash
python organizer.py --gui   # interface graphique
python gui.py                # equivalent, directement

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
`~/.download_organizer/history.jsonl`, les rapports exportés dans
`~/.download_organizer/reports/`, et le journal d'activité dans
`~/.download_organizer/app.log`.

## Regénérer l'exécutable (.exe)

Pas nécessaire pour un usage normal — voir [Démarrage
rapide](#démarrage-rapide) pour télécharger l'exécutable déjà compilé. Cette
section sert à reconstruire l'exe soi-même après une modification du code,
via [PyInstaller](https://pyinstaller.org/), avec une version **figée**
(`requirements-build.txt`) plutôt qu'un `pip install pyinstaller` sans
contrainte : deux builds de la même version de code à des dates différentes
pourraient sinon produire des binaires légèrement différents (bootloader
PyInstaller différent), ce qui complique le diagnostic d'un bug spécifique
au packaging :

```bash
python -m pip install -r requirements-build.txt
python -m PyInstaller NettoyeurTelechargements.spec
```

L'exécutable est produit dans `dist/NettoyeurTelechargements.exe` (~11 Mo,
fichier unique, sans console). Le fichier `.spec` du dépôt fixe la
configuration de build (mode fenêtré, un seul fichier, icône
`assets/icon.ico`) pour un résultat reproductible — pas besoin de refaire
`pyinstaller gui.py` à la main.

Les dossiers `build/` et `dist/` générés par PyInstaller ne sont pas suivis
par Git (voir `.gitignore`) : à regénérer localement à chaque fois.

### Processus de publication d'une release

Avant de rendre une release GitHub publique, calculer l'empreinte SHA-256
de l'exécutable fraîchement généré :

```powershell
Get-FileHash dist\NettoyeurTelechargements.exe -Algorithm SHA256 | Format-List
```

Coller la valeur `Hash` obtenue dans les notes de la release GitHub (ou
l'attacher en tant qu'asset séparé `NettoyeurTelechargements.exe.sha256`),
afin que tout utilisateur puisse vérifier hors bande l'intégrité du fichier
téléchargé (voir « Vérifier l'intégrité du fichier téléchargé » plus haut).

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
update_checker.py                # vérification de mise à jour (API GitHub)
assets/icon.ico                  # icône de l'application (fenêtre + exécutable)
tests/test_organizer.py          # tests automatises (logique métier/CLI)
tests/test_gui.py                # tests automatises (interface graphique)
tests/test_update_checker.py     # tests automatises (vérification de mise à jour)
Lancer.vbs                       # raccourci de lancement double-clic (sans console)
Lancer.bat                       # raccourci de lancement double-clic (avec console, pour debug)
NettoyeurTelechargements.spec    # configuration de build PyInstaller (.exe autonome)
requirements-build.txt           # version figée de PyInstaller (build uniquement)
.gitignore                       # build/, dist/, __pycache__/, etc. non suivis
LICENSE                          # licence MIT
README.md
```

## Licence

Ce projet est publié sous licence [MIT](LICENSE) : gratuit, open source, et
libre de réutilisation, modification et redistribution.

## ☕ Soutenir le projet

<div align="center">

**Cet outil est gratuit, open source, et le restera toujours.**
Pas de version payante, pas de fonctionnalité cachée derrière un paywall.

S'il vous a fait gagner du temps, évité de trier vos fichiers à la main,
ou simplement rendu votre dossier Téléchargements un peu moins chaotique —
un petit café est toujours très apprécié et aide à financer le temps passé
sur les prochaines fonctionnalités. 🙌

[![Offrez-moi un café sur Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/yoshines62000)

*Chaque contribution, même petite, fait vraiment la différence. Merci !* ✨

</div>

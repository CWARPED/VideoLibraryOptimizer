# VideoLibraryOptimizer

![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)

Scanne une bibliothèque vidéo (films + séries), repère les fichiers **trop lourds
pour leur résolution/durée**, les classe par priorité de réencodage, et permet de
les réencoder en **léger mais qualitatif** (4K/HD *light*) en toute sécurité.

- **Résolution conservée**, **toutes les pistes audio copiées** (sans réencode),
  **tous les sous-titres conservés**, chapitres/métadonnées préservés, sortie **MKV**.
- Codec au choix : **HEVC x265 10-bit** ou **SVT-AV1 10-bit**.
- Profils de qualité **CRF** prédéfinis, classés **du plus qualitatif au plus
  compressé** : **Archive** → **Light** → **Balanced** (tous modifiables).
- **Détection animation/anime** (TMDB + repli mots-clés) avec cible bits/pixel
  dédiée — un anime est jugé « bien encodé » selon des critères d'animation.
- Sélection explicite de films, de **saisons entières** ou d'**épisodes à l'unité**.
- **Encodages en parallèle** (réglable) pour exploiter les CPU multi-cœurs.
- Workflow sûr : le fichier (souvent sur NAS) est **copié en local**, réencodé,
  **validé** (durée, pistes, lisibilité, gain), puis — après **confirmation
  manuelle** — remis dans son dossier d'origine (remplacement atomique).
- **Suivi des fichiers déjà traités** : un fichier réencodé n'est plus reproposé
  et le **gain de place total cumulé** est affiché dans la barre de menu.

> Interface web locale, mono-utilisateur, pensée pour tourner sur la machine qui
> encode (testé sur Ryzen 9 9950X3D).

## Fonctionnalités

- **Scan récursif** d'un chemin (chemins NAS UNC `\\serveur\partage` supportés),
  avec cache (un fichier inchangé n'est pas ré-analysé).
- **Score de priorité composite** : surdébit (bits/pixel réel vs cible par
  résolution **et par type de contenu**) + gain d'espace estimé, recalculé à la
  volée selon le codec/profil choisi. Le cache d'un fichier est **rafraîchi**
  après traitement (re-probe + re-score) pour refléter le nouveau débit.
- **Films** : table triable (surdébit / gain / score), responsive, multi-sélection,
  gain affiché en barre.
- **Séries** : liste triée par gain ; détail par saison avec sélection
  d'épisodes à l'unité, « tout cocher » par saison, et **« Sélectionner les
  candidats »** en un clic.
- **Type de contenu** : badge **Film / Animation / Anime** par fichier,
  corrigeable d'un clic (verrouillé contre le re-scan).
- **Encodages parallèles** : plusieurs jobs simultanés (nombre réglable), chacun
  annulable individuellement.
- **File d'attente** : progression temps réel (WebSocket), validation manuelle
  avant remplacement, **nettoyage** (global + par job), reprise après crash.
- **Déjà traité** : les fichiers réencodés par l'app sont marqués, exclus des
  candidats et regroupés à part ; **compteur de gain total** dans la barre de menu.
- **Nom de sortie** : tag optionnel ajouté au nom de fichier et réécriture
  optionnelle des tokens de codec (x264→x265…). Métadonnées de débit vidéo
  corrigées automatiquement à chaque encode.
- **Logs** intégrés (avec stderr ffmpeg complet en cas d'échec).
- **Réglages** : clé TMDB, mots-clés de repli, tables bits/pixel
  (live action + animation), profils CRF, encodages simultanés, tag/réécriture du
  nom, dossier de travail local, pondérations.

## Prérequis

- **Python ≥ 3.11**
- **ffmpeg / ffprobe** compilés avec `libx265` **et** `libsvtav1`, accessibles via
  le `PATH` (ou configurés via `VLO_FFMPEG_PATH` / `VLO_FFPROBE_PATH`).
  Vérifier : `ffmpeg -hide_banner -encoders | findstr "libx265 libsvtav1"`
  (builds [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) « full » ou
  [BtbN](https://github.com/BtbN/FFmpeg-Builds) sur Windows).
- **Optionnel** : une clé API **TMDB** gratuite ([themoviedb.org](https://www.themoviedb.org/))
  pour la détection automatique animation/anime.

## Installation & lancement

### Le plus simple (Windows)

Double-clic sur **`start.bat`** : il crée l'environnement Python au premier
lancement, installe les dépendances, démarre le serveur et ouvre le navigateur.

### Manuel

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn vlo.main:app --host 127.0.0.1 --port 8077
# puis ouvrir http://127.0.0.1:8077
```

## Utilisation

1. **Scan** : saisir le chemin racine, lancer. (Coche **« Forcer la ré-analyse »**
   pour ré-évaluer des fichiers déjà en cache — nécessaire après avoir changé la
   clé TMDB ou les tables bpp.)
2. **Films / Séries** : les candidats sont triés par priorité. Coche les fichiers,
   saisons ou épisodes voulus.
3. Choisir le **codec** et le **profil**, puis lancer l'encodage.
4. **File d'attente** : suivre la progression. Chaque job réencodé attend ta
   **validation** (rapport : gain, pistes, durée…) avant le remplacement.
   L'original reste intact tant que tu n'as pas validé.

## Comment la priorité est calculée

Pour chaque fichier : `bpp_réel = débit_vidéo / (largeur × hauteur × fps)`, comparé
à une **cible bits/pixel** par palier de résolution (table modifiable, distincte
pour le live action et l'animation). Le **surdébit** (`bpp_réel / bpp_cible`) et le
**gain d'espace estimé** sont combinés en un score unique (pondérations réglables).
Les fichiers déjà efficaces (surdébit < 1.1) et, par défaut, les fichiers
**Dolby Vision** sont exclus.

**Détection du type de contenu** : TMDB (genre Animation pour films et séries,
langue d'origine japonaise → anime). Sans clé, repli sur des **mots-clés** dans le
chemin (anime, animation, dessin animé, manga…). Toujours corrigeable manuellement.

## Architecture

```
backend/vlo/
  config.py            Réglages (env VLO_*)
  core/                Modèles, enums, erreurs
  probe/               ffprobe + parsing (bitrate vidéo, HDR/DV, pistes, couleur)
  scan/                Walk récursif + cache, classifier films/séries
  metadata/            Client TMDB + détection mots-clés
  scoring/             Tables bpp (live/anim), estimation, score composite (pur)
  encode/              Construction args ffmpeg, runner (-progress), validation
  naming.py            Tag de sortie + réécriture des tokens de codec
  jobs/                File parallèle, machine à états, copie/remplacement sûr
  api/                 Endpoints FastAPI + WebSocket
  ws/                  Broadcaster pub/sub
  storage/             SQLite (cache scan, jobs, profils, réglages, métadonnées)
frontend/              UI web (vanilla JS) : Scan / Films / Séries / File / Logs / Réglages
```

## Développement & tests

```powershell
.\.venv\Scripts\python.exe -m pytest                      # tout (unitaires + intégration)
.\.venv\Scripts\python.exe -m pytest -m "not integration" # sans ffmpeg
.\.venv\Scripts\ruff.exe check backend tests              # lint
```

Les tests d'intégration génèrent de vrais clips via `ffmpeg lavfi` et exercent le
pipeline complet (encode x265 **et** AV1, validation, remplacement) — y compris
sous la `SelectorEventLoop` de Windows (mode `--reload`).

## Notes & avertissements

- **Encodage logiciel (CPU)** uniquement — x265/AV1 en preset lent, pour la
  qualité (pas de Quick Sync / NVENC / AMF). Le nombre d'**encodages simultanés**
  est réglable : un seul encode 1080p exploite mal un CPU 16c/32t, plusieurs en
  parallèle saturent mieux les cœurs.
- **HDR10** : métadonnées colorimétriques recopiées. **Dolby Vision** exclu par
  défaut (un réencode CPU casse souvent la métadonnée DV) — activable dans les
  réglages.
- Le remplacement est **direct** après validation (pas de corbeille) ; la
  confirmation manuelle sert de filet de sécurité. **Utilise l'outil à tes
  risques** : il modifie/remplace des fichiers de ta bibliothèque.

## Licence

Distribué sous licence **GNU Affero General Public License v3.0 (AGPL-3.0)** —
voir [`LICENSE`](LICENSE). En particulier, si tu héberges une version modifiée
accessible sur un réseau, tu dois en proposer le code source.

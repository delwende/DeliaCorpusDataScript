# DeliaCorpusDataScript

Pipeline de collecte et de structuration de données linguistiques à partir de MooreBurkina.

> Le pipeline est multi-langue et le nom du dépôt peut encore évoluer sans impact sur l'architecture.

## Objectif

Le pipeline ne se limite pas au téléchargement. Il vise à produire des unités exploitables pour l'IA :

- audio ↔ texte
- langue ↔ français
- langue ↔ anglais
- mot ↔ audio
- mot ↔ image
- texte multilingue et multimodal
- éléments ambigus séparés dans une file `needs_review`

## Structure

```text
src/
  crawler.py
  downloader.py
  app_collector.py
  pairing_engine.py
  export_corpus.py
scripts/
  run_pipeline.sh
requirements.txt
```

## Installation

```bash
git clone https://github.com/delwende/DeliaCorpusDataScript.git
cd DeliaCorpusDataScript
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Pipeline complet

```bash
bash scripts/run_pipeline.sh
```

Sorties générées :

```text
inventory/
data/
  raw/
  apps/
  aligned/
corpus/
```

## Étapes

### 1. Inventaire

```bash
python src/crawler.py --out inventory --max-pages 10000
```

### 2. Téléchargement

```bash
python src/downloader.py --inventory inventory --out data/raw
```

Par défaut, les vidéos, les images et les applications Android (`.apk`) ne sont pas téléchargées : elles ne servent pas au corpus texte/audio et pèsent plusieurs Go. Elles restent listées dans `metadata/external_and_reference_urls.csv` (statut `skipped_type`). `--skip-type <type>` choisit les types ignorés ; `--all-types` télécharge tout.

### 3. Applications interactives IPS

```bash
python src/app_collector.py --inventory inventory --out data/apps --workers 6 --skip-images
```

### 4. Alignement

```bash
python src/pairing_engine.py --corpus data/raw --apps data/apps --out data/aligned
```

Le moteur produit notamment :

- `master_units.csv`
- `master_units.jsonl`
- `speech.csv`
- `parallel_translation.csv`
- `lexicon.csv`
- `multimodal.csv`
- `text_only.csv`
- `needs_review.csv`

### 5. Corpus d'entraînement

```bash
python src/export_corpus.py --aligned data/aligned --raw data/raw --apps data/apps --out corpus
```

Produit `corpus/`, organisé par langue — langues du Burkina, mais aussi le français et l'anglais présents sur le site (les paires de traduction vont toujours de la langue locale vers fr/en) : segments audio ↔ transcription par langue (`speech/<lang>/segments.csv` + `audio/`), mots enregistrés, paires de traduction, lexiques et texte monolingue. Les lignes françaises lues dans les applications de proverbes sont détectées et classées en `fr` (et non dans l'audio local) ; sur les pages strictement alternées, elles deviennent des paires de traduction. Les devinettes sont aussi exportées en enregistrements structurés (`riddles/<lang>.jsonl`). Voir `corpus/README.md` pour les volumes par langue.

## Langues

Le crawler est multi-langue et reconnaît notamment Mooré, Dioula/Jula, Fulfulde et ses variantes, Gulimancema/Gourmantchéma, Lobiri, Lyélé, Nuni, San/Samo, Kassem/Kasem, Ninkare/Farefare, Buli, Gurenɛ, Dagbani, Karaboro, Dagara wulé, Kusaal, Mampruli et Bambara/Bamanan.

## Qualité des alignements

Une ressource téléchargée n'est pas automatiquement considérée comme alignée. Les correspondances explicites sont gardées comme données fortes ; les associations heuristiques sont routées vers `needs_review`.

### Formats d'applications IPS reconnus

- **Reading App Builder (SIL)** : pages avec table de synchronisation `var timings`. Chaque segment produit une ligne `speech.csv` avec `audio`, `audio_start` et `audio_end` (secondes) : extrait audio ↔ transcription au niveau de la phrase.
- **Lexique Pro (SIL)** : pages `lexicon/*.htm`. Une ligne par sens avec `headword`, `part_of_speech`, gloses `translation_fr` / `translation_en`, et dans `translations_json` l'allemand, la phonétique, les dialectes et la catégorie. L'enregistrement de prononciation, s'il existe, est dans `audio`. Les pages d'index inversé sont ignorées (doublons).

- **Pages sans audio** (ex. `mos/ora/prv-v12`) : enregistrements séparés par des lignes vides ; proverbe en langue locale, `Bilgri` (explication), puis en italique la traduction française (`Signification`) et anglaise (`Meaning`). Les variantes `a)` / `b)` donnent une paire chacune. Les pages sans traduction deviennent du texte monolingue ; les pages majoritairement françaises (versions françaises des contes) sont ignorées.
- **Proverbes avec audio** : chaque bloc de lignes locales est apparié au bloc français (en italique ou entre parenthèses) qui le suit ; ces paires sont marquées `needs_review`.

- **Contes avec version française** (ex. `mos/ora/vol5`, `dyu/ora/c04`) : une page en langue locale (avec audio) suivie de sa version française. Chaque conte devient une paire au niveau du conte (`parallel_stories/<lang>-fr.jsonl`). Les versions françaises étant des traductions libres (paragraphes découpés autrement), aucune paire phrase à phrase n'en est tirée.

La langue d'une application est déduite du code de dossier (`mos`, `dyu`, `fuh`, `fra`…), sauf si le nom du fichier audio indique une autre langue.

### Collecte des applications : reprise et vitesse

`app_collector.py` reprend une collecte interrompue : les fichiers déjà présents sont réutilisés sans nouveau téléchargement, et `app_manifest.csv` est réécrit après chaque application. `--workers N` télécharge N fichiers en parallèle par application ; `--skip-images` ignore les images (inutiles pour le corpus texte/audio).

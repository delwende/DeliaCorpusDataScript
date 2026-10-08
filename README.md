# DeliaCorpusDataScript

> **Usage non commercial uniquement.** No commercial use.

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

### 3. Applications interactives IPS

```bash
python src/app_collector.py --inventory inventory --out data/apps
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

## Langues

Le crawler est multi-langue et reconnaît notamment Mooré, Dioula/Jula, Fulfulde et ses variantes, Gulimancema/Gourmantchéma, Lobiri, Lyélé, Nuni, San/Samo, Kassem/Kasem, Ninkare/Farefare, Buli, Gurenɛ, Dagbani, Karaboro, Dagara wulé, Kusaal, Mampruli et Bambara/Bamanan.

## Qualité des alignements

Une ressource téléchargée n'est pas automatiquement considérée comme alignée. Les correspondances explicites sont gardées comme données fortes ; les associations heuristiques sont routées vers `needs_review`.

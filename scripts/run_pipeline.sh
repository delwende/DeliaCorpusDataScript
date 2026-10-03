#!/usr/bin/env bash
set -euo pipefail

INVENTORY_DIR="${1:-inventory}"
RAW_DIR="${2:-data/raw}"
APPS_DIR="${3:-data/apps}"
ALIGNED_DIR="${4:-data/aligned}"
CORPUS_DIR="${5:-corpus}"
MAX_PAGES="${MAX_PAGES:-10000}"

python src/crawler.py --out "$INVENTORY_DIR" --max-pages "$MAX_PAGES"
python src/downloader.py --inventory "$INVENTORY_DIR" --out "$RAW_DIR"
python src/app_collector.py --inventory "$INVENTORY_DIR" --out "$APPS_DIR" --workers 6 --skip-images
python src/pairing_engine.py --corpus "$RAW_DIR" --apps "$APPS_DIR" --out "$ALIGNED_DIR"
python src/export_corpus.py --aligned "$ALIGNED_DIR" --raw "$RAW_DIR" --apps "$APPS_DIR" --out "$CORPUS_DIR"

echo "Pipeline complete"
echo "Inventory: $INVENTORY_DIR"
echo "Raw corpus: $RAW_DIR"
echo "Apps: $APPS_DIR"
echo "Aligned data: $ALIGNED_DIR"
echo "Training corpus: $CORPUS_DIR"

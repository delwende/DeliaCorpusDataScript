# Corpus — langues du Burkina Faso

Généré par `src/export_corpus.py` à partir de `data/aligned/master_units.csv`.

| Langue | Segments audio | Heures | Mots enregistrés | Entrées lexique | Paires → fr | Paires → en | Lignes texte |
|---|---|---|---|---|---|---|---|
| Dioula (`dyu`) | 4638 | 3.56 | 0 | 12375 | 12371 | 12361 | 4287 |
| Fulfulde (`ful`) | 8418 | 9.05 | 6879 | 13581 | 13541 | 13568 | 23436 |
| Gulmancema (`gux`) | 1472 | 3.15 | 0 | 0 | 0 | 0 | 1497 |
| Mooré (`mos`) | 7439 | 8.79 | 0 | 0 | 147 | 0 | 14682 |

- `speech/<lang>/segments.csv` : `audio` (relatif au dossier de la langue), `start`/`end` en secondes, `text` = transcription.
- `speech/<lang>/words.csv` : enregistrement de prononciation d'un mot du dictionnaire.
- `translation/<lang>-<fr|en>.csv` : paires `source` (langue locale) → `target`.
- `lexicon/<lang>.csv` : sens de dictionnaire (gloses fr/en/de, phonétique, dialectes, catégorie).
- `text/<lang>.txt` : phrases uniques, une par ligne.

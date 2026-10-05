# Corpus — langues du Burkina Faso

Généré par `src/export_corpus.py` à partir de `data/aligned/master_units.csv`.

| Langue | Segments audio | Heures | Mots enregistrés | Entrées lexique | Paires → fr | Paires → en | Lignes texte |
|---|---|---|---|---|---|---|---|
| Dioula (`dyu`) | 4614 | 3.55 | 0 | 12375 | 12702 | 12355 | 4323 |
| Fulfulde (`ful`) | 8418 | 9.05 | 6879 | 13581 | 13530 | 13558 | 23501 |
| Gulmancema (`gux`) | 1466 | 3.14 | 0 | 0 | 421 | 0 | 1530 |
| Mooré (`mos`) | 7391 | 8.73 | 0 | 0 | 1475 | 629 | 15660 |

- `speech/<lang>/segments.csv` : `audio` (relatif au dossier de la langue), `start`/`end` en secondes, `text` = transcription.
- `speech/<lang>/words.csv` : enregistrement de prononciation d'un mot du dictionnaire.
- `translation/<lang>-<fr|en>.csv` : paires `source` (langue locale) → `target`.
- `lexicon/<lang>.csv` : sens de dictionnaire (gloses fr/en/de, phonétique, dialectes, catégorie).
- `text/<lang>.txt` : phrases uniques, une par ligne.

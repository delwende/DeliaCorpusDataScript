# Corpus — langues du Burkina Faso (et contenus en français/anglais)

Généré par `src/export_corpus.py` à partir de `data/aligned/master_units.csv`.

| Langue | Segments audio | Heures | Mots enregistrés | Entrées lexique | Paires → fr | Paires → en | Contes ↔ fr | Devinettes | Lignes texte |
|---|---|---|---|---|---|---|---|---|---|
| Dioula (`dyu`) | 4614 | 3.55 | 0 | 12375 | 12702 | 12355 | 14 | 0 | 4323 |
| English (`en`) | 682 | 0.51 | 0 | 0 | 0 | 0 | 0 | 0 | 691 |
| Français (`fr`) | 16761 | 18.09 | 0 | 0 | 0 | 0 | 0 | 0 | 34174 |
| Fulfulde (`ful`) | 8418 | 9.05 | 6879 | 13581 | 13530 | 13558 | 0 | 0 | 23501 |
| Gulmancema (`gux`) | 1466 | 3.14 | 0 | 0 | 421 | 0 | 0 | 0 | 1530 |
| Mooré (`mos`) | 7391 | 8.73 | 0 | 0 | 1475 | 629 | 30 | 289 | 15660 |

- `speech/<lang>/segments.csv` : `audio` (relatif au dossier de la langue), `start`/`end` en secondes, `text` = transcription.
- `speech/<lang>/words.csv` : enregistrement de prononciation d'un mot du dictionnaire.
- `translation/<lang>-<fr|en>.csv` : paires `source` (langue locale) → `target`.
- `lexicon/<lang>.csv` : sens de dictionnaire (gloses fr/en/de, phonétique, dialectes, catégorie).
- `parallel_stories/<lang>-fr.jsonl` : conte entier en langue locale avec sa version française (paragraphes de chaque version, audio de la version locale). Les versions françaises sont des traductions libres : l'alignement est au niveau du conte, pas de la phrase.
- `riddles/<lang>.jsonl` : devinettes structurées (devinette, réplique rituelle, réponse) avec l'audio et les temps de début/fin de la devinette et de la réponse.
- `text/<lang>.txt` : phrases uniques, une par ligne.
- `fr` / `en` : contenus en français ou en anglais (applications françaises, versions françaises des contes, lignes françaises lues dans les applications de proverbes).

# Corpus — langues du Burkina Faso (et contenus en français/anglais)

Généré par `src/export_corpus.py` à partir de `data/aligned/master_units.csv`.

| Langue | Segments audio | Heures | Audio long (h) | Mots enregistrés | Entrées lexique | Paires → fr | Paires → en | Contes ↔ fr | Devinettes | Lignes texte |
|---|---|---|---|---|---|---|---|---|---|---|
| Bambara (`bam`) | 0 | 0.0 | 0.0 | 0 | 24666 | 24634 | 23438 | 0 | 0 | 7 |
| Dioula (`dyu`) | 4614 | 3.55 | 0.0 | 0 | 12520 | 14785 | 14376 | 14 | 0 | 6285 |
| English (`en`) | 682 | 0.51 | 0.0 | 0 | 0 | 0 | 0 | 0 | 0 | 693 |
| Français (`fr`) | 16761 | 18.09 | 0.0 | 0 | 0 | 0 | 0 | 0 | 0 | 34211 |
| Fulfulde (`ful`) | 8418 | 9.05 | 0.0 | 6879 | 13594 | 15150 | 13567 | 0 | 0 | 25117 |
| Gulmancema (`gux`) | 1466 | 3.14 | 0.0 | 0 | 0 | 421 | 0 | 0 | 0 | 1535 |
| Mooré (`mos`) | 7391 | 8.73 | 1.0 | 0 | 14253 | 18873 | 17953 | 30 | 289 | 20245 |

- `speech/<lang>/segments.csv` : `audio` (relatif au dossier de la langue), `start`/`end` en secondes, `text` = transcription.
- `speech/<lang>/long_form.csv` : enregistrement entier avec la transcription complète de la page (pages sans découpage en phrases ; à aligner plus tard si besoin). La colonne `paragraphs` garde les paragraphes de la page (liste JSON), utile pour un alignement par paragraphe.
- `speech/<lang>/words.csv` : enregistrement de prononciation d'un mot du dictionnaire.
- `translation/<lang>-<fr|en>.csv` : paires `source` (langue locale) → `target`.
- `lexicon/<lang>.csv` : sens de dictionnaire (gloses fr/en/de, phonétique, dialectes, catégorie).
- `parallel_stories/<lang>-fr.jsonl` : conte entier en langue locale avec sa version française (paragraphes de chaque version, audio de la version locale). Les versions françaises sont des traductions libres : l'alignement est au niveau du conte, pas de la phrase.
- `riddles/<lang>.jsonl` : devinettes structurées (devinette, réplique rituelle, réponse) avec l'audio et les temps de début/fin de la devinette et de la réponse.
- `text/<lang>.txt` : phrases uniques, une par ligne.
- `fr` / `en` : contenus en français ou en anglais (applications françaises, versions françaises des contes, lignes françaises lues dans les applications de proverbes).

# Sources et droits

Généré par `src/export_corpus.py`. Détail par source : `SOURCES.csv` (ce que chaque source apporte au corpus).

## Constat

- 69 sources contribuent au corpus : les pages et PDF de mooreburkina.com et les applications linguistiques qu'il intègre (media.ipsapps.org, audio sur storage.googleapis.com).
- Aucune de ces sources n'affiche de mention de licence ou de droits d'auteur.
  Sans licence explicite, les droits sont réservés par défaut : statut « unknown » dans `SOURCES.csv`.
- Les seules mentions trouvées sur le site concernent des vidéos bibliques intégrées (non collectées), p. ex. :
  « Bible Text in Gulmancema © 2003 Alliance Biblique du Burkina Faso, Audio ℗ 2007 Hosanna » et
  « © 2005 Wycliffe Bible Translators… Audio ℗ 2016 Hosanna ».
- Bible : l'audio (Faith Comes By Hearing, `fcbhabdm.s3.amazonaws.com`) n'est pas collecté. Le texte des applications bibliques (fuh/osa/bible/, mos2/osa/, 23431 lignes) est conservé dans `text/` par décision du projet ; ses droits appartiennent aux sociétés bibliques (à vérifier avant tout usage commercial).

## Usage

Pour un usage commercial, obtenir l'accord des propriétaires des sources marquées « unknown » (contact : https://mooreburkina.com). `SOURCES.csv` indique ce que chaque source apporte, ce qui permet de retirer une source précise du corpus si un propriétaire le demande.

## Principales sources (par contribution)

| Source | Langues | Audio (h) | Mots enregistrés | Paires fr | Lexique | Lignes texte |
|---|---|---|---|---|---|---|
| media.ipsapps.org/fuh/oda/ | ful | 0.00 | 6879 | 13530 | 13581 | 7 |
| media.ipsapps.org/dyu/oda/ | dyu | 0.00 | 0 | 12364 | 12375 | 8 |
| media.ipsapps.org/fuh/osa/bible/ | ful | 0.00 | 0 | 0 | 0 | 15474 |
| media.ipsapps.org/fra/ora/marage/ | fr | 4.96 | 0 | 0 | 0 | 5183 |
| media.ipsapps.org/fra/ora/bienvenu/ | fr | 1.65 | 0 | 0 | 0 | 10466 |
| media.ipsapps.org/mos2/osa/ | mos | 0.00 | 0 | 0 | 0 | 7957 |
| media.ipsapps.org/fra/ora/richesses/ | en;fr | 1.05 | 0 | 0 | 0 | 4754 |
| media.ipsapps.org/fra/ora/evangile/ | fr | 1.59 | 0 | 0 | 0 | 2109 |
| media.ipsapps.org/mos/ora/vol5/ | fr;mos | 1.46 | 0 | 0 | 0 | 2308 |
| media.ipsapps.org/fra/ora/conseils/ | fr | 1.64 | 0 | 0 | 0 | 1678 |
| media.ipsapps.org/mos/ora/devin/ | fr;mos | 1.73 | 0 | 2 | 0 | 595 |
| media.ipsapps.org/mos/ora/vol3/ | fr;mos | 1.19 | 0 | 0 | 0 | 1621 |
| media.ipsapps.org/fuh/ora/co8/ | ful | 1.11 | 0 | 0 | 0 | 1755 |
| media.ipsapps.org/gux/ora/prov-v2/ | fr;gux | 1.39 | 0 | 172 | 0 | 769 |
| media.ipsapps.org/dyu/ora/c04/ | dyu;fr | 1.00 | 0 | 1 | 0 | 1610 |
| media.ipsapps.org/gux/ora/prov-v3/ | fr;gux | 1.23 | 0 | 149 | 0 | 762 |
| media.ipsapps.org/mos/ora/prv-v10/ | fr;mos | 1.18 | 0 | 271 | 0 | 583 |
| media.ipsapps.org/mos/ora/prv-v11/ | fr;mos | 1.15 | 0 | 240 | 0 | 569 |
| media.ipsapps.org/mos/ora/co-fr1/ | fr;mos | 1.37 | 0 | 0 | 0 | 584 |
| media.ipsapps.org/fuh/ora/co1/ | fr;ful | 1.14 | 0 | 0 | 0 | 885 |
| media.ipsapps.org/mos/ora/vol4/ | fr;mos | 1.22 | 0 | 0 | 0 | 515 |
| media.ipsapps.org/fuh/ora/co7/ | ful | 0.83 | 0 | 0 | 0 | 1261 |
| media.ipsapps.org/fuh/ora/co5/ | ful | 0.85 | 0 | 0 | 0 | 1092 |
| media.ipsapps.org/mos/ora/co-fr2/ | fr;mos | 1.15 | 0 | 0 | 0 | 488 |
| media.ipsapps.org/fuh/ora/co4/ | ful | 0.79 | 0 | 0 | 0 | 950 |

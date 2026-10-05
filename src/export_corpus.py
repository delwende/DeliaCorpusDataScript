#!/usr/bin/env python3
"""Export a training-ready corpus of Burkina Faso languages from the aligned units.

Reads <aligned>/master_units.csv (pairing_engine.py output) and keeps only units whose
language is a local language (French/English/Arabic/German are translation targets, not
corpus languages). Writes, per language:

  speech/<lang>/segments.csv   timed phrase: audio file + start/end seconds + transcript
  speech/<lang>/words.csv      dictionary headword + its pronunciation recording
  speech/<lang>/audio/         copies of exactly the audio files referenced above
  translation/<lang>-<fr|en>.csv   source/target sentence or word pairs
  lexicon/<lang>.csv           dictionary senses (headword, POS, glosses, extra fields)
  text/<lang>.txt              unique monolingual sentences, one per line
  summary.json / README.md     counts and audio hours per language

Usage:
  python src/export_corpus.py --aligned data/aligned --raw data/raw --apps data/apps --out corpus
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from pairing_engine import is_french

# French/English are kept as corpus languages too (speech and text); pairs always go
# local language → fr/en, never fr → fr.
NON_CORPUS_LANGS = {""}
TARGET_LANGS = {"fr", "en", "ar", "de"}
# Timings outside this range are broken (zero/negative length, or one phrase spanning minutes).
MIN_SEGMENT_S, MAX_SEGMENT_S = 0.3, 30.0
LANG_NAMES = {
    "mos": "Mooré", "dyu": "Dioula", "ful": "Fulfulde", "gux": "Gulmancema", "bam": "Bambara",
    "lob": "Lobiri", "xsm": "Kassem", "dag": "Dagbani", "kus": "Kusaal", "maw": "Mampruli",
    "fr": "Français", "en": "English",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("_") or "audio"


class AudioCopier:
    """Copies referenced audio into speech/<lang>/audio/ under stable, collision-free names."""

    def __init__(self, out: Path, roots: list[Path]):
        self.out = out
        self.roots = roots
        self.done: dict[tuple[str, str], str] = {}
        self.used: dict[str, set[str]] = defaultdict(set)

    def locate(self, ref: str) -> Path | None:
        for root in self.roots:
            p = root / ref
            if p.is_file():
                return p
        return None

    def copy(self, lang: str, ref: str) -> str:
        """Return the path relative to speech/<lang>/, or "" when the file is not on disk."""
        key = (lang, ref)
        if key in self.done:
            return self.done[key]
        src = self.locate(ref)
        if src is None:
            self.done[key] = ""
            return ""
        name = safe_name(src.name)
        if name in self.used[lang]:
            # Same basename from a different app: prefix with the app folder id.
            parts = Path(ref).parts
            prefix = parts[1] if len(parts) > 2 and parts[0] == "apps" else str(len(self.used[lang]))
            name = f"{prefix}_{name}"
        self.used[lang].add(name)
        dest = self.out / "speech" / lang / "audio" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
        rel = f"audio/{name}"
        self.done[key] = rel
        return rel


def audio_seconds(path: Path) -> float:
    """Duration via ffprobe; 0 when unavailable."""
    if not shutil.which("ffprobe"):
        return 0.0
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def riddle_records(rows: list[dict]) -> list[dict]:
    """Devinettes as structured records from consecutive timed segments of one page:
    "1 M sũm ne sare." (riddle) / "- M zɩ-a." (traditional reply) / "- Yar-bi …" (answer)."""
    out: list[dict] = []
    cur: dict | None = None
    for r in rows:
        m = re.match(r"^(\d+)\s*[.)]?\s+(.*)$", r["text"])
        if m:
            cur = {"number": m.group(1), "riddle": m.group(2), "reply": "", "answer": "", "audio": r["audio"],
                   "riddle_start": r["start"], "riddle_end": r["end"], "answer_start": "", "answer_end": "",
                   "source_file": r["source_file"]}
            out.append(cur)
            continue
        if cur is None or r["source_file"] != cur["source_file"] or r["audio"] != cur["audio"]:
            cur = None
            continue
        line = re.sub(r"^[-–—]\s*", "", r["text"]).strip()
        if not cur["reply"] and r["text"].lstrip().startswith(("-", "–", "—")) and not cur["answer"]:
            cur["reply"] = line
        else:
            cur["answer"] = f"{cur['answer']} {line}".strip()
            cur["answer_start"] = cur["answer_start"] or r["start"]
            cur["answer_end"] = r["end"]
    return [r for r in out if r["answer"]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--aligned", default="data/aligned")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--apps", default="data/apps")
    ap.add_argument("--out", default="corpus")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    units = [u for u in read_csv(Path(args.aligned) / "master_units.csv") if u["language"] not in NON_CORPUS_LANGS]
    copier = AudioCopier(out, [Path(args.apps), Path(args.raw)])

    segments: dict[str, list[dict]] = defaultdict(list)
    words: dict[str, list[dict]] = defaultdict(list)
    lexicon: dict[str, list[dict]] = defaultdict(list)
    parallel: dict[tuple[str, str], list[dict]] = defaultdict(list)
    text: dict[str, dict[str, None]] = defaultdict(dict)
    stories: dict[str, list[dict]] = defaultdict(list)
    long_form: dict[str, list[dict]] = defaultdict(list)
    missing_audio = 0

    for u in units:
        lang = u["language"]
        src = u["text"].strip()
        if not src:
            continue
        origin = u["evidence"].split("+", 1)[0]
        if origin == "page_audio_transcript":
            rel = copier.copy(lang, u["audio"])
            if rel:
                long_form[lang].append({"audio": rel, "duration": f"{audio_seconds(copier.out / 'speech' / lang / rel):.2f}",
                                        "text": src, "title": u["notes"], "collection": u["collection"],
                                        "source_file": u["source_file"], "app_url": u["app_url"]})
            else:
                missing_audio += 1
            continue
        if origin == "parallel_story":
            # Whole tale + its French version: document-level pair, kept out of the sentence CSVs.
            paras = json.loads(u["translations_json"] or "{}")
            stories[lang].append({
                "title": u["notes"], "tale_number": u["record_order"],
                "local_paragraphs": paras.get("local_paragraphs", []),
                "fr_paragraphs": paras.get("fr_paragraphs", []),
                "audio": f"../speech/{lang}/{copier.copy(lang, u['audio'])}" if u["audio"] and copier.copy(lang, u["audio"]) else "",
                "source_file": u["source_file"], "app_url": u["app_url"],
            })
            continue
        # Timed segments that are only punctuation mark music/pauses, not speech.
        if (u["audio"] and u["audio_start"] and sum(ch.isalpha() for ch in src) >= 2
                and MIN_SEGMENT_S <= float(u["audio_end"]) - float(u["audio_start"]) <= MAX_SEGMENT_S):
            rel = copier.copy(lang, u["audio"])
            if rel:
                segments[lang].append({
                    "audio": rel, "start": u["audio_start"], "end": u["audio_end"],
                    "duration": f"{float(u['audio_end']) - float(u['audio_start']):.2f}",
                    "text": src, "collection": u["collection"], "title": u["notes"],
                    "source_file": u["source_file"], "app_url": u["app_url"],
                })
            else:
                missing_audio += 1
        if origin == "lexique_pro_entry":
            extra = json.loads(u["translations_json"] or "{}")
            lexicon[lang].append({
                "headword": u["headword"], "part_of_speech": u["part_of_speech"],
                "fr": u["translation_fr"], "en": u["translation_en"], "de": extra.get("de", ""),
                "phonetic": extra.get("phonetic", ""), "dialects": extra.get("dialects", ""),
                "category": extra.get("category", ""), "app_url": u["app_url"],
            })
            if u["audio"]:
                rel = copier.copy(lang, u["audio"])
                if rel and not any(w["audio"] == rel for w in words[lang][-3:]):
                    words[lang].append({"audio": rel, "text": u["headword"], "fr": u["translation_fr"],
                                        "en": u["translation_en"], "app_url": u["app_url"]})
                elif not rel:
                    missing_audio += 1
        else:
            text[lang][src] = None
        # A pair needs a local-language source; website navigation tables are not translations.
        pairable = origin != "html_table_row" and lang not in TARGET_LANGS and not is_french(src)
        for col, tgt in (("translation_fr", "fr"), ("translation_en", "en")):
            target = u[col].strip()
            if target and pairable:
                source = src
                if origin in {"adjacent_french_segment", "app_record_translation"}:
                    # Proverb apps number each line and parenthesize the translation.
                    source = re.sub(r"^\d+\s*[.)]?\s+", "", source)
                    target = re.sub(r"^\((.*)\)\s*\.?$", r"\1", target).strip()
                parallel[(lang, tgt)].append({"source": source, "target": target, "origin": origin,
                                              "source_file": u["source_file"]})

    seg_fields = ["audio", "start", "end", "duration", "text", "collection", "title", "source_file", "app_url"]
    for lang, rows in segments.items():
        write_csv(out / "speech" / lang / "segments.csv", rows, seg_fields)
    for lang, rows in long_form.items():
        write_csv(out / "speech" / lang / "long_form.csv", rows,
                  ["audio", "duration", "text", "title", "collection", "source_file", "app_url"])
    for lang, rows in words.items():
        write_csv(out / "speech" / lang / "words.csv", rows, ["audio", "text", "fr", "en", "app_url"])
    for lang, rows in lexicon.items():
        write_csv(out / "lexicon" / f"{lang}.csv", rows,
                  ["headword", "part_of_speech", "fr", "en", "de", "phonetic", "dialects", "category", "app_url"])
    for (lang, tgt), rows in parallel.items():
        write_csv(out / "translation" / f"{lang}-{tgt}.csv", rows, ["source", "target", "origin", "source_file"])
    riddles: dict[str, list[dict]] = {}
    for lang, rows in segments.items():
        recs = riddle_records([r for r in rows if r["collection"] == "riddle" or "/devin" in r["app_url"]])
        if recs:
            riddles[lang] = recs
            p = out / "riddles" / f"{lang}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for lang, rows in stories.items():
        p = out / "parallel_stories" / f"{lang}-fr.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for r in sorted(rows, key=lambda r: int(r["tale_number"]) if r["tale_number"].isdigit() else 0):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for lang, lines in text.items():
        p = out / "text" / f"{lang}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    langs = sorted(set(segments) | set(long_form) | set(words) | set(lexicon) | set(text) | {l for l, _ in parallel})
    summary = {"languages": {}, "missing_audio_refs": missing_audio}
    for lang in langs:
        audio_dir = out / "speech" / lang / "audio"
        summary["languages"][lang] = {
            "name": LANG_NAMES.get(lang, lang),
            "speech_segments": len(segments[lang]),
            "speech_hours": round(sum(float(r["duration"]) for r in segments[lang]) / 3600, 2),
            "long_form_recordings": len(long_form[lang]),
            "long_form_hours": round(sum(float(r["duration"]) for r in long_form[lang]) / 3600, 2),
            "word_recordings": len(words[lang]),
            "audio_files": len(list(audio_dir.iterdir())) if audio_dir.exists() else 0,
            "audio_mb": round(sum(f.stat().st_size for f in audio_dir.iterdir()) / 1048576, 1) if audio_dir.exists() else 0,
            "lexicon_senses": len(lexicon[lang]),
            "translation_pairs": {t: len(parallel[(lang, t)]) for t in ("fr", "en") if parallel[(lang, t)]},
            "text_lines": len(text[lang]),
            "parallel_stories_fr": len(stories[lang]),
            "riddles": len(riddles.get(lang, [])),
        }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Corpus — langues du Burkina Faso (et contenus en français/anglais)", "",
             "Généré par `src/export_corpus.py` à partir de `data/aligned/master_units.csv`.", "",
             "| Langue | Segments audio | Heures | Audio long (h) | Mots enregistrés | Entrées lexique | Paires → fr | Paires → en | Contes ↔ fr | Devinettes | Lignes texte |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for lang, s in summary["languages"].items():
        tp = s["translation_pairs"]
        lines.append(f"| {s['name']} (`{lang}`) | {s['speech_segments']} | {s['speech_hours']} | {s['long_form_hours']} | {s['word_recordings']} | "
                     f"{s['lexicon_senses']} | {tp.get('fr', 0)} | {tp.get('en', 0)} | {s['parallel_stories_fr']} | {s['riddles']} | {s['text_lines']} |")
    lines += ["", "- `speech/<lang>/segments.csv` : `audio` (relatif au dossier de la langue), `start`/`end` en secondes, `text` = transcription.",
              "- `speech/<lang>/long_form.csv` : enregistrement entier avec la transcription complète de la page "
              "(pages sans découpage en phrases ; à aligner plus tard si besoin).",
              "- `speech/<lang>/words.csv` : enregistrement de prononciation d'un mot du dictionnaire.",
              "- `translation/<lang>-<fr|en>.csv` : paires `source` (langue locale) → `target`.",
              "- `lexicon/<lang>.csv` : sens de dictionnaire (gloses fr/en/de, phonétique, dialectes, catégorie).",
              "- `parallel_stories/<lang>-fr.jsonl` : conte entier en langue locale avec sa version française "
              "(paragraphes de chaque version, audio de la version locale). Les versions françaises sont des "
              "traductions libres : l'alignement est au niveau du conte, pas de la phrase.",
              "- `riddles/<lang>.jsonl` : devinettes structurées (devinette, réplique rituelle, réponse) avec l'audio "
              "et les temps de début/fin de la devinette et de la réponse.",
              "- `text/<lang>.txt` : phrases uniques, une par ligne.",
              "- `fr` / `en` : contenus en français ou en anglais (applications françaises, versions françaises "
              "des contes, lignes françaises lues dans les applications de proverbes).", ""]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

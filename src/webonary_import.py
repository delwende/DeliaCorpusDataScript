#!/usr/bin/env python3
"""Import Webonary dictionary pages saved from a browser.

Webonary (www.webonary.org) blocks automated access, so pages are saved by hand (browse or search
results, "Save page as… HTML") into data/webonary/<site>/, e.g. data/webonary/moore/a_1.html.
This script parses every saved page of a site into one entry per headword and, with --audio,
downloads each entry's pronunciation recording (public files on Webonary's cloud storage).

Output per site: data/webonary/<site>/entries.jsonl (+ audio/ when --audio is given).

  python src/webonary_import.py --root data/webonary --audio
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup, Tag

# Webonary site name -> ISO 639-3 code used in the corpus.
SITE_LANGUAGES = {"moore": "mos", "dioula-bf": "dyu", "fulfuldeburkina": "ful", "gulimancema": "gux"}
# Entry-level fields Webonary renders as <span class="..."> (FLEx custom field names, in French).
ENTRY_FIELDS = {"plural": "plural", "singulier": "singular", "varinat": "variants", "variante": "variants",
                "comparez": "compare", "racine": "root", "emprunt": "loan_from",
                "inaccompli": "imperfective", "nominal": "verbal_noun"}
SUPERSCRIPT_RE = re.compile(r"font-size\s*:\s*58%|color\s*:\s*#930", re.I)


def strip_superscripts(node: Tag) -> str:
    """Homonym/sense numbers are small superscripts inside headwords ("a¹", "laarba 1"):
    return them and remove them from the node."""
    nums = []
    for sup in node.find_all("span", style=SUPERSCRIPT_RE):
        nums.append(text_of(sup))
        sup.decompose()
    return nums[0] if nums else ""


def text_of(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def lang_text(node: Tag, lang: str) -> str:
    return " ; ".join(t for t in (text_of(s) for s in node.find_all("span", lang=lang, recursive=False)) if t) or \
        " ; ".join(t for t in (text_of(s) for s in node.find_all("span", lang=lang)) if t)


def parse_entry(div: Tag) -> dict | None:
    head = div.find(class_="mainheadword")
    if head is None:
        return None  # e.g. "minorentryvariant" cross-references, which carry no translation
    homonym = strip_superscripts(head)
    for target in div.select(".configtarget .headword"):
        strip_superscripts(target)
    entry = {"guid": div.get("id", ""), "headword": text_of(head), "audio_url": "", "tones": "",
             "part_of_speech": "", "senses": []}
    if homonym:
        entry["homonym"] = homonym
    src = div.select_one(".lexemeform audio source[src]")
    if src:
        entry["audio_url"] = src["src"]
    entry["tones"] = text_of(div.select_one(".pronunciations .form"))
    entry["part_of_speech"] = text_of(div.select_one(".sharedgrammaticalinfo .partofspeech"))
    for cls, key in ENTRY_FIELDS.items():
        node = div.find("span", class_=cls, recursive=False)
        if node:
            entry[key] = text_of(node)
    sci = div.select_one(".scientificname")
    if sci:
        entry["scientific_name"] = text_of(sci)
    for content in div.select(".sensecontent"):
        sense_node = content.find(class_="sense")
        if sense_node is None:
            continue
        gloss = sense_node.find(class_="definitionorgloss")
        sense = {"fr": lang_text(gloss, "fr") if gloss else "", "en": lang_text(gloss, "en") if gloss else "",
                 "pos": text_of(content.select_one(".partofspeech")) or entry["part_of_speech"],
                 "domains": [text_of(d.select_one(".name span[lang=fr]")) or text_of(d.select_one(".name"))
                             for d in sense_node.select(".semanticdomain")],
                 "relations": {}, "examples": []}
        for ref in sense_node.select(".lexsensereference"):
            kind = text_of(ref.find(class_="ownertype_name")).lower()
            targets = [text_of(t.find("a")) for t in ref.select(".configtarget .headword")]
            if kind:
                sense["relations"].setdefault(kind, []).extend(t for t in targets if t)
        for ex in sense_node.select(".examplescontent"):
            local = text_of(ex.select_one(".example"))
            tr = ex.select_one(".translation")
            if local:
                sense["examples"].append({"local": local, "fr": lang_text(tr, "fr") if tr else "",
                                          "en": lang_text(tr, "en") if tr else ""})
        if sense["fr"] or sense["en"] or sense["examples"]:
            entry["senses"].append(sense)
    return entry if entry["headword"] and entry["senses"] else None


def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    return [e for e in (parse_entry(div) for div in soup.select("div.entry")) if e]


def audio_name(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


def import_site(site_dir: Path, audio: bool, delay: float) -> dict:
    entries: dict[str, dict] = {}
    for page in sorted(site_dir.glob("*.htm*")):
        for e in parse_page(page.read_text(encoding="utf-8", errors="replace")):
            entries.setdefault(e["guid"] or e["headword"], e)  # the same entry can appear on several pages
    stats = {"pages": len(list(site_dir.glob("*.htm*"))), "entries": len(entries), "audio_downloaded": 0,
             "audio_missing": 0}
    if audio:
        session = requests.Session()
        (site_dir / "audio").mkdir(exist_ok=True)
        for e in entries.values():
            if not e["audio_url"]:
                continue
            dest = site_dir / "audio" / audio_name(e["audio_url"])
            if not dest.exists():
                try:
                    resp = session.get(e["audio_url"], timeout=60)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
                    stats["audio_downloaded"] += 1
                    time.sleep(delay)
                except requests.RequestException:
                    stats["audio_missing"] += 1
                    continue
            e["audio"] = dest.as_posix()
    with (site_dir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for e in entries.values():
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default="data/webonary", help="Folder with one sub-folder of saved pages per site")
    ap.add_argument("--audio", action="store_true", help="Download each entry's pronunciation recording")
    ap.add_argument("--delay", type=float, default=0.3, help="Seconds between audio downloads")
    args = ap.parse_args()
    root = Path(args.root)
    for site_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        print(site_dir.name, json.dumps(import_site(site_dir, args.audio, args.delay)))


if __name__ == "__main__":
    main()

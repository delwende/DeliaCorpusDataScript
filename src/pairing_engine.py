#!/usr/bin/env python3
"""Build aligned multilingual/speech units from a downloaded MooreBurkina corpus.

This engine is intentionally conservative: it emits high-confidence aligned rows when the
source structure explicitly supports the relationship (same JSON object, same HTML table row,
same DOM container, exact basename/id match). Weaker order-based associations are retained
with lower confidence and routed to needs_review.csv rather than silently treated as gold data.

Supported evidence sources:
  * HTML tables and repeated DOM records
  * <audio>/<source>/<img> elements with nearby text
  * JSON records with text/translation/audio/image fields
  * JavaScript object literals with recognizable fields
  * XML records with recognizable child tags
  * exact filename/stem matching across downloaded assets
  * conservative ordered pairing when counts and numbering agree

Outputs:
  master_units.csv / master_units.jsonl
  speech.csv
  parallel_translation.csv
  lexicon.csv
  multimodal.csv
  text_only.csv
  needs_review.csv
  summary.json

Examples:
  python mooreburkina_pairing_engine.py --corpus MooreBurkinaCorpus --apps MooreBurkinaApps --out MooreBurkinaAligned

Requirements:
  pip install beautifulsoup4
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from itertools import groupby
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup, Tag

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".3gp"}
TEXT_SOURCE_EXTS = {".html", ".htm", ".json", ".js", ".xml", ".txt", ".csv"}

LANG_CANON = {
    "moore": "mos", "mooré": "mos", "mos": "mos",
    "dioula": "dyu", "jula": "dyu", "dyula": "dyu", "dyu": "dyu",
    "fulfulde": "ful", "ful": "ful", "fub": "ful",
    "gulimancema": "gux", "gulmanchema": "gux", "gurmancema": "gux", "gux": "gux",
    "french": "fr", "français": "fr", "francais": "fr", "fr": "fr",
    "english": "en", "anglais": "en", "en": "en",
    "arabic": "ar", "arabe": "ar", "ar": "ar",
    "bambara": "bam", "bamanan": "bam", "bam": "bam",
    "lobiri": "lob", "lob": "lob",
    "kassem": "xsm", "kasem": "xsm", "xsm": "xsm",
    "dagbani": "dag", "dag": "dag",
    "kusaal": "kus", "kus": "kus",
    "mampruli": "maw", "maw": "maw",
    # ISO 639-3 folder codes used on media.ipsapps.org.
    "fuh": "ful", "fra": "fr", "eng": "en",
}

# SIL Reading App Builder pages: Popcorn.js timing table + one element per timed phrase.
TIMING_RE = re.compile(r'label:\s*"([^"]+)"\s*,\s*start:\s*([\d.]+)\s*,\s*end:\s*([\d.]+)')
# Bundled third-party libraries carry no language data.
VENDOR_JS_RE = re.compile(r"(jquery|popcorn|tooltipster|bootstrap|modernizr|\.min\.js$|^sw\.js$|^pwa-main\.js$)", re.I)
# Detecting French lines inside local-language apps (proverb apps read each proverb, then its
# French translation). Orthography is the strongest signal: Burkina orthographies use letters
# French never does, while é/è/à/ç and elisions (l', qu') do not occur in them. Function words
# shared with local languages (a, de, du, si, on…) are deliberately excluded.
FRENCH_WORDS = {
    "le", "la", "les", "des", "et", "est", "un", "une", "il", "elle", "que", "qui", "pas", "dans", "pour",
    "sur", "au", "aux", "ce", "cette", "son", "sa", "ses", "avec", "mais", "plus", "tout", "vous", "nous",
    "ils", "sont", "ont", "leur", "être", "fait", "comme", "quand", "je", "tu", "mon", "ton", "votre",
    "notre", "très", "bien", "peut", "faut", "celui", "jamais", "rien", "autre", "même", "sans", "chez",
    "par", "où", "ne", "se", "à",
}
LOCAL_LETTERS_RE = re.compile("[ɛɔŋɲɓɗƴʋɩãẽĩõũƐƆŊƝƁƊƳ]")
FRENCH_ACCENT_RE = re.compile("[éèêàçùâîôûœ]")
# Longest local/French block (in timed lines) paired as one proverb/translation.
MAX_PAIR_LINES = 4
FRENCH_ELISION_RE = re.compile(r"\b(l|d|qu|c|n|s|j|m|t|jusqu|lorsqu)['’]\w", re.I)
# A language named in a media filename beats the app folder code (e.g. English audio under /fra/).
FILENAME_LANG_RE = re.compile(r"(english|anglais|fran[cç]ais|french|moor[eé]|dioula|jula|fulfulde)", re.I)

GENERIC_UI_RE = re.compile(
    r"^(play|pause|stop|next|previous|suivant|précédent|precedent|écouter|ecouter|audio|"
    r"cliquez ici|menu|accueil|home|download|télécharger|telecharger|volume|vol\.?\s*\d*)$",
    re.I,
)
NUMBER_PREFIX_RE = re.compile(r"^\s*(\d{1,4})\s*[.)\-:–—]?\s*(.+?)\s*$")

TEXT_KEYS = {
    "text", "texte", "content", "sentence", "phrase", "proverb", "proverbe", "story", "conte",
    "word", "headword", "entry", "lemma", "title", "titre", "mos", "moore", "mooré", "dyu", "dioula",
    "ful", "fulfulde", "gux", "gulimancema", "gulmanchema",
}
FR_KEYS = {"fr", "french", "francais", "français", "translation_fr", "traduction_fr", "traduction", "explication_fr", "explication"}
EN_KEYS = {"en", "english", "anglais", "translation_en", "traduction_en"}
AR_KEYS = {"ar", "arabic", "arabe", "translation_ar", "traduction_ar"}
AUDIO_KEYS = {"audio", "sound", "son", "mp3", "wav", "audio_url", "audiofile", "audio_file", "soundfile", "sound_file"}
IMAGE_KEYS = {"image", "img", "picture", "photo", "image_url", "imagefile", "image_file"}
VIDEO_KEYS = {"video", "video_url", "videofile", "video_file"}
POS_KEYS = {"pos", "part_of_speech", "grammatical_category", "categorie", "catégorie"}
DEF_KEYS = {"definition", "définition", "meaning", "sens"}

MASTER_FIELDS = [
    "unit_id", "language", "variant", "collection", "record_order", "headword", "part_of_speech",
    "text", "translation_fr", "translation_en", "translation_ar", "translations_json", "definition",
    "audio", "audio_start", "audio_end", "image", "video", "speaker", "author", "source_page", "app_url", "source_file",
    "evidence", "confidence", "review_status", "notes",
]


def norm_space(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def clean_text(s: Any) -> str:
    s = norm_space(s)
    if not s or GENERIC_UI_RE.match(s):
        return ""
    return s


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


def canon_lang(value: str) -> str:
    parts = [p.strip() for p in re.split(r"[;,/]", value or "") if p.strip()]
    for p in parts:
        low = p.lower()
        if low in LANG_CANON:
            return LANG_CANON[low]
    return parts[0].lower() if parts else ""


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def relpath_or_url(base_file: Path, raw: str, app_root: Path, app_id: str) -> str:
    """Resolve a media ref to a local app-relative path when possible."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    # Use filesystem-relative resolution for downloaded static apps.
    candidate = (base_file.parent / unquote(raw.split("?", 1)[0].split("#", 1)[0])).resolve()
    try:
        rel = candidate.relative_to(app_root.resolve())
        if candidate.exists():
            return rel.as_posix()
    except Exception:
        pass
    return raw


@dataclass
class Unit:
    language: str = ""
    variant: str = ""
    collection: str = ""
    record_order: str = ""
    headword: str = ""
    part_of_speech: str = ""
    text: str = ""
    translation_fr: str = ""
    translation_en: str = ""
    translation_ar: str = ""
    translations_json: str = ""
    definition: str = ""
    audio: str = ""
    audio_start: str = ""
    audio_end: str = ""
    image: str = ""
    video: str = ""
    speaker: str = ""
    author: str = ""
    source_page: str = ""
    app_url: str = ""
    source_file: str = ""
    evidence: str = ""
    confidence: float = 0.0
    review_status: str = ""
    notes: str = ""
    unit_id: str = ""

    def finalize(self) -> "Unit":
        for k in ["headword", "part_of_speech", "text", "translation_fr", "translation_en", "translation_ar",
                  "definition", "speaker", "author", "notes"]:
            setattr(self, k, clean_text(getattr(self, k)))
        # A headword is also usable as text for lexicon rows.
        if not self.text and self.headword:
            self.text = self.headword
        # Avoid translation duplicating source text exactly.
        for attr in ("translation_fr", "translation_en", "translation_ar"):
            if getattr(self, attr) and norm_space(getattr(self, attr)).casefold() == norm_space(self.text).casefold():
                setattr(self, attr, "")
        payload = "|".join([
            self.language, self.variant, self.collection, self.text, self.translation_fr, self.translation_en,
            self.audio, self.audio_start, self.image, self.source_file, self.record_order,
        ])
        self.unit_id = self.unit_id or sha1(payload)
        self.review_status = "aligned" if self.confidence >= 0.85 else "needs_review"
        return self

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = f"{self.confidence:.2f}"
        return d


class AssetIndex:
    """Lookup downloaded assets by URL, basename and stem."""
    def __init__(self, corpus: Path | None, apps: Path | None):
        self.by_url: dict[str, str] = {}
        self.by_basename: defaultdict[str, list[str]] = defaultdict(list)
        self.by_stem: defaultdict[str, list[str]] = defaultdict(list)
        self.meta_by_local: dict[str, dict[str, str]] = {}
        self.roots: list[Path] = []
        if corpus and corpus.exists():
            self.roots.append(corpus)
            for r in read_csv(corpus / "metadata" / "download_manifest.csv"):
                lp = (r.get("local_path") or "").split(";", 1)[0]
                if not lp:
                    continue
                self._add(r.get("source_url", ""), lp, r)
        if apps and apps.exists():
            self.roots.append(apps)
            for r in read_csv(apps / "app_manifest.csv"):
                lp = r.get("local_path", "")
                if not lp:
                    continue
                self._add(r.get("url", ""), lp, r)

    def _add(self, url: str, lp: str, meta: dict[str, str]) -> None:
        if url:
            self.by_url[url] = lp
        name = Path(lp).name.lower()
        stem = Path(lp).stem.lower()
        self.by_basename[name].append(lp)
        self.by_stem[stem].append(lp)
        self.meta_by_local[lp] = meta

    def resolve(self, raw: str, source_file: str = "") -> str:
        raw = (raw or "").strip()
        if not raw:
            return ""
        if raw in self.by_url:
            return self.by_url[raw]
        parsed = urlparse(raw)
        name = Path(unquote(parsed.path or raw)).name.lower()
        if name and len(self.by_basename.get(name, [])) == 1:
            return self.by_basename[name][0]
        stem = Path(name).stem if name else Path(raw).stem.lower()
        if stem and len(self.by_stem.get(stem, [])) == 1:
            return self.by_stem[stem][0]
        # Keep the raw reference if it cannot be resolved uniquely.
        return raw

    def exact_stem_companions(self, path: str) -> dict[str, str]:
        stem = Path(path).stem.lower()
        vals = self.by_stem.get(stem, [])
        out = {"audio": "", "image": "", "video": ""}
        for v in vals:
            ext = Path(v).suffix.lower()
            if ext in AUDIO_EXTS and not out["audio"]:
                out["audio"] = v
            elif ext in IMAGE_EXTS and not out["image"]:
                out["image"] = v
            elif ext in VIDEO_EXTS and not out["video"]:
                out["video"] = v
        return out


def first_value(d: dict[str, Any], keys: set[str]) -> str:
    for k, v in d.items():
        kk = str(k).strip().lower()
        if kk in keys and isinstance(v, (str, int, float)):
            val = clean_text(v)
            if val:
                return val
    return ""


def infer_primary_text(d: dict[str, Any], primary_lang: str) -> tuple[str, str]:
    """Return (text, headword) from a structured record."""
    lower = {str(k).strip().lower(): v for k, v in d.items()}
    lang_keys = {
        "mos": ["mos", "moore", "mooré"], "dyu": ["dyu", "dioula", "jula"],
        "ful": ["ful", "fulfulde"], "gux": ["gux", "gulimancema", "gulmanchema", "gurmancema"],
    }.get(primary_lang, [])
    for k in lang_keys:
        if k in lower and isinstance(lower[k], (str, int, float)):
            return clean_text(lower[k]), ""
    hw = first_value(d, {"headword", "word", "lemma", "entry"})
    txt = first_value(d, TEXT_KEYS)
    return txt or hw, hw


def unit_from_mapping(d: dict[str, Any], meta: dict[str, str], asset_index: AssetIndex,
                      source_file: str, order: int, evidence: str, confidence: float) -> Unit | None:
    lang = canon_lang(meta.get("language", ""))
    text, headword = infer_primary_text(d, lang)
    fr = first_value(d, FR_KEYS)
    en = first_value(d, EN_KEYS)
    ar = first_value(d, AR_KEYS)
    audio = asset_index.resolve(first_value(d, AUDIO_KEYS), source_file)
    image = asset_index.resolve(first_value(d, IMAGE_KEYS), source_file)
    video = asset_index.resolve(first_value(d, VIDEO_KEYS), source_file)
    pos = first_value(d, POS_KEYS)
    definition = first_value(d, DEF_KEYS)
    if not any([text, fr, en, ar, audio, image, video, definition]):
        return None
    # Don't make bare technical asset records unless they carry linguistic content.
    if not any([text, fr, en, ar, definition]) and any([audio, image, video]):
        return None
    return Unit(
        language=lang, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
        record_order=str(order), headword=headword, part_of_speech=pos, text=text,
        translation_fr=fr, translation_en=en, translation_ar=ar, definition=definition,
        audio=audio, image=image, video=video, source_page=meta.get("source_page", ""),
        app_url=meta.get("app_url", ""), source_file=source_file,
        evidence=evidence, confidence=confidence,
    ).finalize()


def iter_json_records(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        scalar_keys = sum(isinstance(v, (str, int, float, bool, type(None))) for v in obj.values())
        recognizable = any(str(k).lower() in (TEXT_KEYS | FR_KEYS | EN_KEYS | AR_KEYS | AUDIO_KEYS | IMAGE_KEYS | DEF_KEYS | POS_KEYS) for k in obj)
        if recognizable and scalar_keys:
            yield obj
        for v in obj.values():
            yield from iter_json_records(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_json_records(v)


def parse_json_file(path: Path, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out = []
    for i, rec in enumerate(iter_json_records(obj), 1):
        u = unit_from_mapping(rec, meta, idx, rel_source, i, "explicit_json_record", 0.99)
        if u:
            out.append(u)
    return out


def parse_js_objects(path: Path, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    """Conservative loose parser for simple JS object literals.

    It does not execute JavaScript. It only reads object-like blocks and quoted key/value pairs.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[Unit] = []
    order = 0
    # Keep blocks bounded to reduce accidental cross-record matches.
    for m in re.finditer(r"\{([^{}]{1,6000})\}", text, flags=re.S):
        block = m.group(1)
        pairs: dict[str, str] = {}
        for km in re.finditer(r"(?:['\"]?([A-Za-zÀ-ÿ_][\wÀ-ÿ-]*)['\"]?)\s*:\s*(['\"])((?:\\.|(?!\2).)*)\2", block, flags=re.S):
            key, _, val = km.groups()
            try:
                val = bytes(val, "utf-8").decode("unicode_escape") if "\\u" in val else val
            except Exception:
                pass
            pairs[key] = val.replace("\\'", "'").replace('\\"', '"')
        if not pairs:
            continue
        recognizable = any(k.lower() in (TEXT_KEYS | FR_KEYS | EN_KEYS | AR_KEYS | AUDIO_KEYS | IMAGE_KEYS | DEF_KEYS | POS_KEYS) for k in pairs)
        if not recognizable:
            continue
        order += 1
        u = unit_from_mapping(pairs, meta, idx, rel_source, order, "javascript_object_fields", 0.92)
        if u:
            out.append(u)
    return out


def strip_media_text(tag: Tag) -> str:
    clone = BeautifulSoup(str(tag), "html.parser")
    for x in clone.find_all(["audio", "video", "source", "img", "script", "style", "button", "nav", "footer"]):
        x.decompose()
    return clean_text(clone.get_text(" ", strip=True))


def media_ref(tag: Tag, kind: str) -> str:
    if kind == "audio":
        node = tag if tag.name in {"audio", "source"} else tag.find(["audio", "source"])
        if node:
            return str(node.get("src") or node.get("data-src") or node.get("data-audio") or "")
    if kind == "image":
        node = tag if tag.name == "img" else tag.find("img")
        if node:
            return str(node.get("src") or node.get("data-src") or "")
    if kind == "video":
        node = tag if tag.name in {"video", "source"} else tag.find(["video", "source"])
        if node:
            return str(node.get("src") or node.get("data-src") or "")
    return ""


def header_lang(label: str) -> str:
    s = norm_space(label).lower()
    for name, code in LANG_CANON.items():
        if name in s:
            return code
    if "traduction" in s or "translation" in s or "explication" in s:
        if "anglais" in s or "english" in s:
            return "en"
        if "arabe" in s or "arabic" in s:
            return "ar"
        return "fr"
    return ""


def parse_html_tables(soup: BeautifulSoup, path: Path, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    out: list[Unit] = []
    order = 0
    primary = canon_lang(meta.get("language", ""))
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers: list[str] = []
        first_cells = rows[0].find_all(["th", "td"])
        if rows[0].find("th"):
            headers = [norm_space(c.get_text(" ", strip=True)) for c in first_cells]
            data_rows = rows[1:]
        else:
            data_rows = rows
        for tr in data_rows:
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            vals = [clean_text(strip_media_text(c)) for c in cells]
            audio = ""; image = ""; video = ""
            for c in cells:
                if not audio:
                    audio = media_ref(c, "audio")
                if not image:
                    image = media_ref(c, "image")
                if not video:
                    video = media_ref(c, "video")
            text = fr = en = ar = hw = pos = definition = ""
            if headers and len(headers) == len(cells):
                for h, val in zip(headers, vals):
                    if not val:
                        continue
                    hl = header_lang(h)
                    hs = h.lower()
                    if hl == primary and not text:
                        text = val
                    elif hl == "fr": fr = val
                    elif hl == "en": en = val
                    elif hl == "ar": ar = val
                    elif any(k in hs for k in ["mot", "word", "headword", "entrée", "entree", "lemme"]): hw = val
                    elif any(k in hs for k in ["nature", "pos", "catégorie", "categorie"]): pos = val
                    elif any(k in hs for k in ["définition", "definition", "sens"]): definition = val
                if not text and hw:
                    text = hw
                if not text:
                    # First textual column not assigned to a translation is source text.
                    for h, val in zip(headers, vals):
                        if val and header_lang(h) not in {"fr", "en", "ar"}:
                            text = val; break
            else:
                nonempty = [v for v in vals if v]
                if nonempty:
                    text = nonempty[0]
                    if len(nonempty) >= 2 and primary not in {"fr", "en", "ar"}:
                        fr = nonempty[1]
                    if len(nonempty) >= 3:
                        en = nonempty[2]
            if not any([text, fr, en, ar, audio, image, video]):
                continue
            order += 1
            u = Unit(
                language=primary, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
                record_order=str(order), headword=hw, part_of_speech=pos, text=text, translation_fr=fr,
                translation_en=en, translation_ar=ar, definition=definition,
                audio=idx.resolve(urljoin(meta.get("url", meta.get("app_url", "")), audio), rel_source) if audio else "",
                image=idx.resolve(urljoin(meta.get("url", meta.get("app_url", "")), image), rel_source) if image else "",
                video=idx.resolve(urljoin(meta.get("url", meta.get("app_url", "")), video), rel_source) if video else "",
                source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""), source_file=rel_source,
                evidence="html_table_row", confidence=0.96 if headers else 0.84,
            ).finalize()
            if u.text or u.translation_fr or u.translation_en:
                out.append(u)
    return out


def nearest_record_container(media: Tag) -> Tag | None:
    cur: Tag | None = media
    best: Tag | None = None
    for _ in range(6):
        if cur is None or not isinstance(cur, Tag):
            break
        if cur.name in {"li", "tr", "article", "section", "p", "div", "td"}:
            txt = strip_media_text(cur)
            media_count = len(cur.find_all(["audio", "video", "img", "source"]))
            if 1 <= len(txt) <= 1500 and media_count <= 4:
                best = cur
                if cur.name in {"li", "tr", "article", "p"}:
                    break
        cur = cur.parent if isinstance(cur.parent, Tag) else None
    return best


def split_labeled_text(container: Tag, primary: str) -> tuple[str, str, str, str]:
    text = fr = en = ar = ""
    # Prefer elements with lang= attributes or clear classes/labels.
    for el in container.find_all(["span", "p", "div", "td", "li"], recursive=True):
        val = clean_text(strip_media_text(el))
        if not val or len(val) > 1200:
            continue
        lang_attr = (el.get("lang") or "").lower()
        cls = " ".join(el.get("class") or []).lower()
        marker = f"{lang_attr} {cls} {val[:40].lower()}"
        code = ""
        if lang_attr.startswith("fr") or re.search(r"\b(fr|french|francais|français|traduction)\b", marker): code = "fr"
        elif lang_attr.startswith("en") or re.search(r"\b(en|english|anglais)\b", marker): code = "en"
        elif lang_attr.startswith("ar") or re.search(r"\b(ar|arabic|arabe)\b", marker): code = "ar"
        elif primary and (lang_attr.startswith(primary) or primary in cls): code = primary
        if code == "fr" and not fr: fr = val
        elif code == "en" and not en: en = val
        elif code == "ar" and not ar: ar = val
        elif code == primary and not text: text = val
    whole = clean_text(strip_media_text(container))
    if not text and whole:
        text = whole
    return text, fr, en, ar


def parse_html_media_containers(soup: BeautifulSoup, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    out: list[Unit] = []
    seen_containers: set[int] = set()
    primary = canon_lang(meta.get("language", ""))
    order = 0
    for media in soup.find_all(["audio", "video"]):
        # Table rows are handled by parse_html_tables with stronger column semantics.
        if media.find_parent("table") is not None:
            continue
        container = nearest_record_container(media)
        if not container or id(container) in seen_containers:
            continue
        seen_containers.add(id(container))
        text, fr, en, ar = split_labeled_text(container, primary)
        audio_raw = media_ref(container, "audio")
        image_raw = media_ref(container, "image")
        video_raw = media_ref(container, "video")
        if not any([text, fr, en, ar]):
            continue
        order += 1
        base_url = meta.get("url", meta.get("app_url", ""))
        out.append(Unit(
            language=primary, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
            record_order=str(order), text=text, translation_fr=fr, translation_en=en, translation_ar=ar,
            audio=idx.resolve(urljoin(base_url, audio_raw), rel_source) if audio_raw else "",
            image=idx.resolve(urljoin(base_url, image_raw), rel_source) if image_raw else "",
            video=idx.resolve(urljoin(base_url, video_raw), rel_source) if video_raw else "",
            source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""), source_file=rel_source,
            evidence="same_dom_container", confidence=0.91,
        ).finalize())
    return out


def numbered_text_items(soup: BeautifulSoup) -> list[tuple[int, str]]:
    vals: list[tuple[int, str]] = []
    seen = set()
    # List items and paragraph/div text nodes are common in simple static apps.
    for el in soup.find_all(["li", "p", "div"]):
        if el.find(["nav", "footer"]):
            continue
        txt = clean_text(strip_media_text(el))
        if not txt or len(txt) > 1000:
            continue
        m = NUMBER_PREFIX_RE.match(txt)
        if not m:
            continue
        n = int(m.group(1)); body = clean_text(m.group(2))
        if not body or (n, body) in seen:
            continue
        seen.add((n, body)); vals.append((n, body))
    vals.sort(key=lambda x: x[0])
    return vals


def parse_html_ordered_pairing(soup: BeautifulSoup, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    """Fallback: pair N ordered audio elements with N numbered textual records.

    Kept at 0.74 confidence so it goes to review unless promoted manually.
    """
    audios = []
    base_url = meta.get("url", meta.get("app_url", ""))
    for a in soup.find_all(["audio", "source"]):
        src = str(a.get("src") or a.get("data-src") or "")
        if src:
            resolved = idx.resolve(urljoin(base_url, src), rel_source)
            if resolved not in audios:
                audios.append(resolved)
    texts = numbered_text_items(soup)
    if len(audios) < 2 or len(audios) != len(texts):
        return []
    primary = canon_lang(meta.get("language", ""))
    return [Unit(
        language=primary, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
        record_order=str(n), text=text, audio=audio,
        source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""), source_file=rel_source,
        evidence="ordered_count_match", confidence=0.74,
        notes="Audio and numbered text counts matched exactly; verify sequence before training.",
    ).finalize() for (n, text), audio in zip(texts, audios)]


def app_language(meta: dict[str, str], media_name: str = "") -> str:
    """Language of an IPS app file: filename hint, then app folder code, then crawler guess."""
    m = FILENAME_LANG_RE.search(unquote(media_name or ""))
    if m:
        return canon_lang(m.group(1).replace("ç", "c"))
    # Folder codes look like "dyu", "dyu-audio" or "mos2".
    first = urlparse(meta.get("app_url", "")).path.strip("/").split("/")[0].split("-")[0].rstrip("0123456789")
    if first.lower() in LANG_CANON:
        return LANG_CANON[first.lower()]
    return canon_lang(meta.get("language", ""))


def has_french_hint(text: str) -> bool:
    """Weak evidence (one French function word, accent or elision, no local letters); only used
    together with layout cues such as italics or parentheses."""
    if LOCAL_LETTERS_RE.search(text):
        return False
    low = text.lower()
    toks = [w for w in re.split(r"[^\w]+", low) if w]
    return bool(toks) and (any(w in FRENCH_WORDS for w in toks) or bool(FRENCH_ACCENT_RE.search(low))
                           or bool(FRENCH_ELISION_RE.search(low)))


def is_french(text: str) -> bool:
    if LOCAL_LETTERS_RE.search(text):
        return False
    low = text.lower()
    toks = [w for w in re.split(r"[^\w]+", low) if w]
    if not toks:
        return False
    accents = len(FRENCH_ACCENT_RE.findall(low))
    if len(toks) < 3:
        return accents > 0
    ratio = sum(w in FRENCH_WORDS for w in toks) / len(toks)
    elisions = bool(FRENCH_ELISION_RE.search(low))
    if text.lstrip().startswith("(") and has_french_hint(text):
        return True  # proverb apps parenthesize the French translation
    return ratio >= 0.3 or (ratio >= 0.12 and (accents > 0 or elisions)) or accents >= 2 or (elisions and accents > 0)


def parse_timed_audio_page(html: str, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    """One unit per timed phrase: transcript text + audio file + start/end seconds."""
    soup = BeautifulSoup(html, "html.parser")
    srcs = [str(s.get("src") or "") for s in soup.select("audio source, audio[src]") if s.get("src")]
    if not srcs:
        return []
    src = next((x for x in srcs if x.lower().split("?")[0].endswith(".mp3")), srcs[0])
    audio = idx.resolve(src, rel_source)
    lang = app_language(meta, src)
    title = ""
    sel = soup.find(id="book-selector")
    if sel:
        title = norm_space(sel.get_text(" "))
    units = []
    italics: list[bool] = []
    for order, (label, start, end) in enumerate(TIMING_RE.findall(html), 1):
        node = soup.find(id=f"T{label}")
        if node is None:
            continue
        # Phrases are split into styling spans mid-word, so join without separators.
        text = norm_space(node.get_text(""))
        if not text:
            continue
        italic_chars = sum(len(sp.get_text("")) for sp in node.find_all(class_=re.compile(r"^(it|bdit)$")))
        italics.append(italic_chars >= 0.6 * len(text))
        units.append(Unit(
            language=lang, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
            record_order=str(order), text=text, audio=audio, audio_start=start, audio_end=end,
            source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""), source_file=rel_source,
            evidence="timed_audio_segment", confidence=0.95, notes=title,
        ).finalize())
    if lang not in {"fr", "en"}:
        # Translations are set in italics; italics plus any French hint is decisive.
        french = [is_french(u.text) or (it and has_french_hint(u.text)) for u, it in zip(units, italics)]
        # A parenthesized translation can open with words that carry no French signal
        # ("(Deux personnes,") — it belongs with the French line that follows.
        for i in range(len(units) - 2, -1, -1):
            if not french[i] and french[i + 1] and units[i].text.lstrip().startswith("("):
                french[i] = True
        for u, fr in zip(units, french):
            if fr:
                u.language = "fr"
        # Proverb pages read a local block (often split over 2-3 timed lines), then its French
        # translation block, then the local block again. Pair each short local run with the
        # French run that follows it. French-dominated pages (French versions of tales) and long
        # runs (story paragraphs with a stray French line) are skipped; pairs are kept for review.
        if any(french) and sum(french) / len(french) <= 0.7:
            runs = [(fr, [u for u, _ in grp]) for fr, grp in groupby(zip(units, french), key=lambda x: x[1])]
            for (fr_a, local), (fr_b, trans) in zip(runs, runs[1:]):
                # The repeated reading of the previous proverb can run into the next numbered
                # one ("… zĩigẽ. 216 Ninsaal …"): start the block at its last numbered line.
                starts = [i for i, u in enumerate(local) if re.match(r"^\d+\s*[.)]?\s", u.text)]
                if starts:
                    local = local[starts[-1]:]
                if fr_a or not fr_b or len(local) > MAX_PAIR_LINES or len(trans) > MAX_PAIR_LINES:
                    continue
                first = local[0]
                units.append(Unit(
                    language=first.language, variant=first.variant, collection=first.collection,
                    record_order=first.record_order, text=" ".join(u.text for u in local),
                    translation_fr=" ".join(u.text for u in trans),
                    source_page=first.source_page, app_url=first.app_url, source_file=rel_source,
                    evidence="adjacent_french_segment", confidence=0.75, notes=title,
                ).finalize())
    return units


ENGLISH_WORDS = {"the", "is", "of", "and", "to", "in", "that", "it", "you", "he", "she", "his", "her", "a", "an",
                 "not", "be", "with", "for", "who", "does", "if", "when", "one", "your", "are", "has", "have"}
MAX_RECORD_CHARS = 400
LABEL_RE = re.compile(r"^\s*(bilgri|signification|sens|meaning|explication|explanation|emploi|usage)\s*:\s*", re.I)


def is_english(text: str) -> bool:
    toks = [w for w in re.split(r"[^\w']+", text.lower()) if w]
    return len(toks) >= 3 and sum(w in ENGLISH_WORDS for w in toks) / len(toks) >= 0.2 and not is_french(text)


def split_variants(text: str) -> dict[str, str]:
    """Split "a) first b) second" into {"a": "first", "b": "second"}; {} when unlettered."""
    marks = list(re.finditer(r"(?:^|\s)([a-e])\)\s+", text))
    if len(marks) < 2 or marks[0].group(1) != "a":
        return {}
    out = {}
    for m, nxt in zip(marks, marks[1:] + [None]):
        out[m.group(1)] = text[m.end(): nxt.start() if nxt else len(text)].strip()
    return out


def parse_untimed_app_page(html: str, meta: dict[str, str], rel_source: str, idx: "AssetIndex | None" = None) -> list[Unit]:
    """Reading App Builder pages without audio: records separated by blank (div.b) lines.

    Proverb collections such as mos/ora/prv-v12 give, per record: the local proverb (bold),
    an optional local explanation ("Bilgri :"), then in italics the French proverb and its
    "Signification", the English proverb and its "Meaning". Records without any translation
    become plain local text (e.g. tales without audio).
    """
    if "/osa/" in meta.get("app_url", ""):
        return []  # scripture apps: third-party texts, kept out like their audio
    content = BeautifulSoup(html, "html.parser").find(id="content")
    if content is None:
        return []
    lang = app_language(meta)
    if not lang:
        return []
    records: list[list[Tag]] = [[]]
    for div in content.find_all("div", recursive=False):
        classes = div.get("class") or []
        if "b" in classes:
            if records[-1]:
                records.append([])
        elif div.get_text(strip=True):
            records[-1].append(div)
    lines = [norm_space(d.get_text("")) for r in records for d in r]
    if lang in {"fr", "en"} or (lines and sum(is_french(t) for t in lines) / len(lines) > 0.5):
        # French/English pages (French apps, French versions of tales): monolingual text.
        page_lang = lang if lang in {"fr", "en"} else "fr"
        return [Unit(language=page_lang, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
                     record_order=str(i), text=line, source_page=meta.get("source_page", ""),
                     app_url=meta.get("app_url", ""), source_file=rel_source, evidence="app_page_text",
                     confidence=0.85).finalize()
                for i, line in enumerate(lines, 1) if line]
    title = ""
    units: list[Unit] = []
    for order, rec in enumerate((r for r in records if r), 1):
        local, local_expl, fr, fr_expl, en, en_expl = [], [], [], [], [], []
        parts = []
        for div in rec:
            # One div can hold several lines separated by <br> (e.g. English proverb + "Meaning:").
            italic = div.find(class_=re.compile(r"^(it|bdit)$")) is not None and not div.find(class_="bd")
            for br in div.find_all("br"):
                br.replace_with("\n")
            parts.extend((norm_space(line), italic) for line in div.get_text("").split("\n") if line.strip())
        for text, italic in parts:
            m = LABEL_RE.match(text)
            label = m.group(1).lower() if m else ""
            body = text[m.end():].strip() if m else text
            if label == "bilgri":
                local_expl.append(body)
            elif label in {"signification", "sens", "explication"}:
                fr_expl.append(body)
            elif label in {"meaning", "explanation"}:
                en_expl.append(body)
            elif label:
                continue
            elif is_french(text) or (italic and not fr and has_french_hint(text)):
                fr.append(text)
            elif is_english(text) or (italic and fr):
                en.append(text)
            else:
                local.append(text)
        if not local:
            continue
        common = dict(language=lang, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
                      source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""),
                      source_file=rel_source, notes=title)
        # Proverb-sized records only: a story paragraph next to a French line is not a pair.
        if (fr or en) and len(" ".join(local)) <= MAX_RECORD_CHARS and len(" ".join(fr)) <= MAX_RECORD_CHARS:
            src = re.sub(r"^\d+\s*[.)]?\s+", "", " ".join(local))
            variants = split_variants(src)
            fr_v, en_v = split_variants(" ".join(fr)), split_variants(" ".join(en))
            if len(variants) > 1 and set(variants) == set(fr_v):
                # "a) … b) …": one pair per variant, matched by letter.
                for letter, text in variants.items():
                    units.append(Unit(record_order=f"{order}{letter}", text=text, translation_fr=fr_v[letter],
                                      translation_en=en_v.get(letter, ""), evidence="app_record_translation",
                                      confidence=0.9, **common).finalize())
            else:
                units.append(Unit(record_order=str(order), text=src, translation_fr=" ".join(fr),
                                  translation_en=" ".join(en), evidence="app_record_translation",
                                  confidence=0.9, **common).finalize())
            if local_expl and (fr_expl or en_expl):
                units.append(Unit(record_order=f"{order}.1", text=" ".join(local_expl),
                                  translation_fr=" ".join(fr_expl), translation_en=" ".join(en_expl),
                                  evidence="app_record_translation", confidence=0.9, **common).finalize())
        else:
            for i, line in enumerate(local + local_expl, 1):
                units.append(Unit(record_order=f"{order}.{i}", text=line, evidence="app_page_text",
                                  confidence=0.85, **common).finalize())
    return units


def _gloss(span: Tag) -> str:
    return norm_space(span.get_text(" ")).rstrip(" ;.,")


def parse_lexique_pro_page(html: str, meta: dict[str, str], rel_source: str) -> list[Unit]:
    """SIL Lexique Pro HTML export: one unit per sense with French/English/German glosses.

    Entries may carry a pronunciation recording (<a href="../audio/x.mp3">) and dialect codes.
    """
    soup = BeautifulSoup(html, "html.parser")
    lang = app_language(meta)
    units: list[Unit] = []
    entry: dict[str, str] = {}

    def emit(sense: dict[str, str]) -> None:
        if not entry.get("headword") or not (sense.get("fr") or sense.get("en") or sense.get("de")):
            return
        extra = {k: v for k, v in (("de", sense.get("de", "")), ("phonetic", entry.get("phonetic", "")),
                                   ("dialects", entry.get("dialects", "")),
                                   ("category", ", ".join(entry.get("categories", [])))) if v}
        units.append(Unit(
            language=lang, variant=meta.get("variant", ""), collection="dictionary",
            record_order=str(len(units) + 1), headword=entry["headword"], part_of_speech=entry.get("pos", ""),
            text=entry["headword"], translation_fr=sense.get("fr", ""), translation_en=sense.get("en", ""),
            translations_json=json.dumps(extra, ensure_ascii=False) if extra else "",
            audio=entry.get("audio", ""),
            source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""), source_file=rel_source,
            evidence="lexique_pro_entry", confidence=0.95,
        ).finalize())

    for p in soup.find_all("p", class_=["lpLexEntryPara", "lpLexEntryPara2", "lpLexSubEntryPara"]):
        classes = p.get("class") or []
        if "lpLexEntryPara2" not in classes:
            name = p.find(class_=re.compile(r"^lpLex(Sub)?EntryName"))
            if name is None:
                continue
            entry = {"headword": norm_space(name.get_text(" ")), "categories": []}
            ph = p.find(class_="lpPhonetic")
            if ph:
                entry["phonetic"] = norm_space(ph.get_text(" "))
            dia = p.find(class_="lpCustomField_Dialects")
            if dia:
                entry["dialects"] = norm_space(dia.get_text(" "))
            link = p.find("a", href=re.compile(r"\.(mp3|wav|ogg|m4a)$", re.I))
            if link:
                # Hrefs are relative to the entry page; store the app-root-relative path.
                entry["audio"] = os.path.normpath(os.path.join(os.path.dirname(rel_source), unquote(link["href"])))
        sense: dict[str, str] = {}
        for span in p.find_all("span"):
            cls = (span.get("class") or [""])[0]
            if cls == "lpSenseNumber" and sense:
                emit(sense)
                sense = {}
            elif cls == "lpPartOfSpeech":
                entry["pos"] = _gloss(span)
            elif cls == "lpCategory":
                entry.setdefault("categories", []).append(_gloss(span))
            elif cls in ("lpGlossFrench", "lpGlossEnglish", "lpGlossGerman"):
                key = {"lpGlossFrench": "fr", "lpGlossEnglish": "en", "lpGlossGerman": "de"}[cls]
                sense[key] = "; ".join(filter(None, [sense.get(key, ""), _gloss(span)]))
        emit(sense)
    return units


def parse_html_file(path: Path, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "var timings" in text:
        return parse_timed_audio_page(text, meta, idx, rel_source)
    if "lpLexEntryPara" in text:
        return parse_lexique_pro_page(text, meta, rel_source)
    if "lpLexEntryName" in text:
        # Lexique Pro index/category pages only point back to entries parsed above.
        return []
    if 'id="content"' in text and 'class="m"' in text:
        units = parse_untimed_app_page(text, meta, rel_source, idx)
        paras, src = page_paragraphs(text)
        if src and paras and "/osa/" not in meta.get("app_url", ""):
            # Audio without phrase timings: the whole recording with the whole page as transcript
            # (long-form speech data; can be force-aligned later).
            lang = app_language(meta, src)
            if sum(is_french(t) for t in paras) / len(paras) > 0.5:
                lang = "fr" if lang not in {"en"} else lang
            # The leading tale number is a page heading, not part of the reading.
            paras = [re.sub(r"^\d+\s*[.)]?\s+", "", paras[0])] + paras[1:]
            units.append(Unit(language=lang, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
                              text=" ".join(paras), audio=idx.resolve(src, rel_source),
                              translations_json=json.dumps({"paragraphs": paras}, ensure_ascii=False),
                              source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""),
                              source_file=rel_source, evidence="page_audio_transcript", confidence=0.85,
                              notes=paras[0]).finalize())
        return units
    soup = BeautifulSoup(text, "html.parser")
    for x in soup.find_all(["script", "style", "noscript", "nav", "footer"]):
        # Keep scripts outside DOM extraction; JS files are handled separately.
        x.decompose()
    units = []
    units.extend(parse_html_tables(soup, path, meta, idx, rel_source))
    units.extend(parse_html_media_containers(soup, meta, idx, rel_source))
    if not any(u.audio for u in units):
        units.extend(parse_html_ordered_pairing(soup, meta, idx, rel_source))
    return units


def parse_xml_file(path: Path, meta: dict[str, str], idx: AssetIndex, rel_source: str) -> list[Unit]:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return []
    out = []
    order = 0
    for elem in root.iter():
        children = list(elem)
        if not children:
            continue
        d = {}
        for ch in children:
            key = ch.tag.split("}")[-1]
            val = norm_space(ch.text)
            if val:
                d[key] = val
            for attr, av in ch.attrib.items():
                if attr.lower() in {"src", "href", "file", "audio", "image"}:
                    d[attr] = av
        if d:
            order += 1
            u = unit_from_mapping(d, meta, idx, rel_source, order, "explicit_xml_record", 0.96)
            if u:
                out.append(u)
    return out


def dedupe_units(units: list[Unit]) -> list[Unit]:
    # Prefer stronger evidence for effectively same linguistic+media unit.
    best: dict[str, Unit] = {}
    for u in units:
        u.finalize()
        key = "|".join([
            u.language, norm_space(u.text).casefold(), norm_space(u.translation_fr).casefold(),
            norm_space(u.translation_en).casefold(), u.audio, u.audio_start, u.image, u.video,
        ])
        if not key.strip("|"):
            continue
        prev = best.get(key)
        if prev is None or u.confidence > prev.confidence:
            best[key] = u
    return list(best.values())


def meta_for_app_file(apps_root: Path, rel: str, manifest_by_local: dict[str, dict[str, str]]) -> dict[str, str]:
    return dict(manifest_by_local.get(rel, {}))


def collect_app_units(apps: Path, idx: AssetIndex) -> list[Unit]:
    rows = read_csv(apps / "app_manifest.csv")
    by_local = {r.get("local_path", ""): r for r in rows if r.get("local_path")}
    units: list[Unit] = []
    for rel, meta in by_local.items():
        p = apps / rel
        if not p.exists() or p.suffix.lower() not in TEXT_SOURCE_EXTS:
            continue
        ext = p.suffix.lower()
        try:
            if ext in {".html", ".htm"}:
                got = parse_html_file(p, meta, idx, rel)
            elif ext == ".json":
                got = parse_json_file(p, meta, idx, rel)
            elif ext == ".js":
                got = [] if VENDOR_JS_RE.search(p.name) else parse_js_objects(p, meta, idx, rel)
            elif ext == ".xml":
                got = parse_xml_file(p, meta, idx, rel)
            else:
                got = []
            units.extend(got)
        except Exception as exc:
            units.append(Unit(
                language=canon_lang(meta.get("language", "")), variant=meta.get("variant", ""),
                collection=meta.get("collection", ""), source_page=meta.get("source_page", ""),
                app_url=meta.get("app_url", ""), source_file=rel, evidence="parser_error", confidence=0.0,
                notes=f"{type(exc).__name__}: {exc}",
            ).finalize())
    units.extend(collect_parallel_pages(apps, by_local, idx))
    return units


def page_paragraphs(html: str) -> tuple[list[str], str]:
    """Top-level text paragraphs of a Reading App Builder page, plus its audio src (if any)."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find(id="content")
    if content is None:
        return [], ""
    paras = []
    for div in content.find_all("div", recursive=False):
        if "b" in (div.get("class") or []):
            continue
        text = norm_space(div.get_text(""))
        if text:
            paras.append(text)
    srcs = [str(x.get("src")) for x in soup.select("audio source, audio[src]") if x.get("src")]
    src = next((x for x in srcs if x.lower().split("?")[0].endswith(".mp3")), srcs[0] if srcs else "")
    return paras, src


def paragraphs_align(local: list[str], french: list[str]) -> bool:
    """Same paragraph count is not enough: the French version often splits/merges differently.
    Require every substantial paragraph to keep the story's overall French/local length ratio."""
    ratio = sum(map(len, french)) / max(1, sum(map(len, local)))
    for a, b in zip(local, french):
        if max(len(a), len(b)) < 25:
            continue  # titles and one-liners carry no length signal
        r = (len(b) / max(1, len(a))) / ratio
        if not 0.6 <= r <= 1.67:
            return False
    return True


def collect_parallel_pages(apps: Path, by_local: dict[str, dict[str, str]], idx: AssetIndex) -> list[Unit]:
    """Local-language page followed by its French version (e.g. "Contes vol. 5 avec français").

    Pages are consecutive in app order and open with the same tale number ("1 Yõens yelle" /
    "1 Problème des Souris"). Emits one whole-story unit per tale (evidence parallel_story, with
    the local page's audio) and, when both versions have the same number of paragraphs,
    paragraph-level pairs (evidence parallel_paragraph).
    """
    pages: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for rel, meta in by_local.items():
        if rel.lower().endswith((".html", ".htm")) and Path(rel).name != "index.html" and Path(rel).parent.name == meta.get("app_id"):
            pages[meta.get("app_id", "")].append((rel, meta))
    units: list[Unit] = []
    for app_pages in pages.values():
        app_pages.sort(key=lambda x: Path(x[0]).name)
        info = []
        for rel, meta in app_pages:
            try:
                paras, src = page_paragraphs((apps / rel).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            french = sum(is_french(t) for t in paras) / len(paras) > 0.5 if paras else False
            num = re.match(r"^(\d+)\b", paras[0]) if paras else None
            info.append((rel, meta, paras, src, french, num.group(1) if num else None))
        # Apps that strictly alternate local page / French page pair by position even when the
        # local page opens with a song instead of the tale number (dyu/ora/c04: "landa donkili").
        flags = [x[4] for x in info]
        alternating = len(flags) >= 2 and not flags[0] and all(a != b for a, b in zip(flags, flags[1:]))
        for (rel_a, meta, loc, src, fr_a, num_a), (rel_b, _, fra, _, fr_b, num_b) in zip(info, info[1:]):
            if fr_a or not fr_b:
                continue
            if not (alternating or (num_a is not None and num_a == num_b)):
                continue
            num_a = num_a or num_b or str(len(units) + 1)
            lang = app_language(meta, src)
            if lang in {"fr", "en", ""}:
                continue
            strip_num = lambda t: re.sub(r"^\d+\s*[.)]?\s*", "", t)
            loc, fra = [strip_num(loc[0])] + loc[1:], [strip_num(fra[0])] + fra[1:]
            common = dict(language=lang, variant=meta.get("variant", ""), collection=meta.get("collection", ""),
                          source_page=meta.get("source_page", ""), app_url=meta.get("app_url", ""),
                          source_file=f"{rel_a}|{rel_b}", notes=loc[0])
            units.append(Unit(record_order=num_a, text=" ".join(loc), translation_fr=" ".join(fra),
                              translations_json=json.dumps({"local_paragraphs": loc, "fr_paragraphs": fra},
                                                           ensure_ascii=False),
                              audio=idx.resolve(src, rel_a) if src else "", evidence="parallel_story",
                              confidence=0.9, **common).finalize())
            if len(loc) == len(fra) and paragraphs_align(loc, fra):
                for i, (a, b) in enumerate(zip(loc, fra), 1):
                    units.append(Unit(record_order=f"{num_a}.{i}", text=a, translation_fr=b,
                                      evidence="parallel_paragraph", confidence=0.8, **common).finalize())
    return units


def parse_content_page_html(path: Path, meta: dict[str, str], idx: AssetIndex, rel: str) -> list[Unit]:
    """Extract useful text/parallel rows from downloaded MooreBurkina HTML pages.

    This intentionally avoids turning navigation menus into corpus units. It only emits table rows
    and content blocks with explicit language labels, or simple numbered content on pages whose
    collection is folktale/proverb/riddle/poem/dictionary.
    """
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for x in soup.find_all(["script", "style", "noscript", "nav", "footer", "header"]):
        x.decompose()
    units = parse_html_tables(soup, path, meta, idx, rel)
    coll = (meta.get("collection") or "").lower()
    if coll in {"folktale", "proverb", "riddle", "poem", "dictionary"}:
        primary = canon_lang(meta.get("language", ""))
        existing_texts = {norm_space(u.text).casefold() for u in units}
        order = 0
        for n, body in numbered_text_items(soup):
            if norm_space(body).casefold() in existing_texts:
                continue
            order += 1
            units.append(Unit(
                language=primary, variant=meta.get("variant", ""), collection=coll,
                record_order=str(n), text=body, source_page=meta.get("source_url", meta.get("url", "")),
                source_file=rel, evidence="numbered_content_page_text", confidence=0.80,
                notes="Text unit extracted from numbered content page; no media relation asserted.",
            ).finalize())
    return units


def collect_corpus_page_units(corpus: Path, idx: AssetIndex) -> list[Unit]:
    rows = read_csv(corpus / "metadata" / "download_manifest.csv")
    out = []
    for r in rows:
        if r.get("item_kind") != "page":
            continue
        paths = (r.get("local_path") or "").split(";")
        html_rel = next((x for x in paths if x.lower().endswith(('.html', '.htm'))), "")
        if not html_rel:
            continue
        p = corpus / html_rel
        if not p.exists():
            continue
        meta = dict(r)
        meta["url"] = r.get("source_url", "")
        try:
            out.extend(parse_content_page_html(p, meta, idx, html_rel))
        except Exception:
            pass
    return out


# PDF dictionaries name their language in the file name; the crawler's page-level guess is noisy.
PDF_LANG_HINTS = [("bambara", "bam"), ("moore", "mos"), ("mooré", "mos"), ("dioula", "dyu"), ("jula", "dyu"),
                  ("fulfulde", "ful"), ("gulmancema", "gux"), ("gulimancema", "gux")]


def pdf_language(name: str) -> str:
    low = unquote(name).lower()
    return next((code for hint, code in PDF_LANG_HINTS if hint in low), "")


def collect_pdf_dictionary_units(corpus: Path) -> list[Unit]:
    """Lexique Pro dictionary PDFs (Mooré has no dictionary app): one unit per sense and per
    translated example sentence. Other PDFs are left to the text extraction step."""
    try:
        import lexique_pdf
    except ImportError:
        return []
    units: list[Unit] = []
    for r in read_csv(corpus / "metadata" / "download_manifest.csv"):
        rel = (r.get("local_path") or "").split(";")[0]
        if r.get("resource_type") != "pdf" or not rel.lower().endswith(".pdf") or not (corpus / rel).exists():
            continue
        lang = pdf_language(Path(rel).name) or pdf_language(r.get("source_url", ""))
        if not lang:
            continue
        try:
            entries = lexique_pdf.parse_pdf(corpus / rel)
        except Exception as exc:  # a broken PDF should not stop the run
            print(f"PDF dictionary skipped ({type(exc).__name__}): {rel}")
            continue
        common = dict(language=lang, source_page=r.get("source_url", ""), source_file=rel)
        for order, e in enumerate(entries, 1):
            extra = {k: v for k, v in (("phonetic", e.phonetic), ("category", e.fields.get("category", "")),
                                       ("dialects", e.fields.get("dialects", ""))) if v}
            for i, sense in enumerate(e.senses, 1):
                units.append(Unit(record_order=f"{order}.{i}", headword=e.headword, text=e.headword,
                                  part_of_speech=e.part_of_speech, translation_fr=sense.get("fr", ""),
                                  translation_en=sense.get("en", ""), collection="dictionary",
                                  translations_json=json.dumps({**extra, **({"de": sense["de"]} if sense.get("de") else {})},
                                                               ensure_ascii=False),
                                  evidence="lexique_pro_pdf_entry", confidence=0.9, **common).finalize())
            for j, ex in enumerate(e.examples, 1):
                if ex.get("fr") or ex.get("en"):
                    units.append(Unit(record_order=f"{order}.x{j}", headword=e.headword, text=ex["local"],
                                      translation_fr=ex.get("fr", ""), translation_en=ex.get("en", ""),
                                      collection="dictionary_example",
                                      translations_json=json.dumps({"de": ex["de"]} if ex.get("de") else {},
                                                                   ensure_ascii=False),
                                      evidence="lexique_pro_pdf_example", confidence=0.9, **common).finalize())
    return units


def enrich_by_exact_stem(units: list[Unit], idx: AssetIndex) -> None:
    for u in units:
        # If a source unit refers to one media type, exact stem can add companion media.
        anchor = u.audio or u.image or u.video
        if not anchor:
            continue
        comps = idx.exact_stem_companions(anchor)
        changed = False
        if not u.audio and comps["audio"]:
            u.audio = comps["audio"]; changed = True
        if not u.image and comps["image"]:
            u.image = comps["image"]; changed = True
        # Audio-only .webm files sit in VIDEO_EXTS; never re-add the audio itself as video.
        if not u.video and comps["video"] and comps["video"] != u.audio:
            u.video = comps["video"]; changed = True
        if changed:
            u.evidence += "+exact_stem_companion"
            u.confidence = min(1.0, u.confidence + 0.03)
            u.finalize()


def outputs(units: list[Unit], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = [u.dict() for u in sorted(units, key=lambda x: (x.language, x.collection, x.source_file, int(x.record_order or 0) if str(x.record_order or '').isdigit() else 0, x.unit_id))]
    write_csv(out / "master_units.csv", rows, MASTER_FIELDS)
    with (out / "master_units.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    speech = [r for r in rows if r["text"] and r["audio"]]
    parallel = []
    for r in rows:
        for col, target in [("translation_fr", "fr"), ("translation_en", "en"), ("translation_ar", "ar")]:
            if r["text"] and r[col]:
                x = dict(r)
                x["source_text"] = r["text"]
                x["target_text"] = r[col]
                x["target_language"] = target
                parallel.append(x)
    lexicon = [r for r in rows if r["headword"] or (r["collection"] == "dictionary" and len(r["text"].split()) <= 8)]
    multimodal = [r for r in rows if r["text"] and (r["image"] or (r["audio"] and r["image"]))]
    text_only = [r for r in rows if r["text"] and not r["audio"] and not r["image"] and not r["video"]]
    review = [r for r in rows if r["review_status"] == "needs_review"]

    write_csv(out / "speech.csv", speech, MASTER_FIELDS)
    parallel_fields = MASTER_FIELDS + ["source_text", "target_text", "target_language"]
    write_csv(out / "parallel_translation.csv", parallel, parallel_fields)
    write_csv(out / "lexicon.csv", lexicon, MASTER_FIELDS)
    write_csv(out / "multimodal.csv", multimodal, MASTER_FIELDS)
    write_csv(out / "text_only.csv", text_only, MASTER_FIELDS)
    write_csv(out / "needs_review.csv", review, MASTER_FIELDS)

    summary = {
        "units": len(rows),
        "aligned": sum(r["review_status"] == "aligned" for r in rows),
        "needs_review": len(review),
        "speech_pairs": len(speech),
        "parallel_pairs": len(parallel),
        "lexicon_units": len(lexicon),
        "multimodal_units": len(multimodal),
        "text_only_units": len(text_only),
        "by_language": dict(Counter(r["language"] or "(unknown)" for r in rows).most_common()),
        "by_collection": dict(Counter(r["collection"] or "(unknown)" for r in rows).most_common()),
        "by_evidence": dict(Counter(r["evidence"] or "(unknown)" for r in rows).most_common()),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Align MooreBurkina text/audio/translations/images")
    ap.add_argument("--corpus", default="MooreBurkinaCorpus", help="Raw corpus produced by downloader")
    ap.add_argument("--apps", default="MooreBurkinaApps", help="Static IPS apps produced by app collector")
    ap.add_argument("--out", default="MooreBurkinaAligned")
    ap.add_argument("--min-confidence", type=float, default=0.0, help="Drop units below this score; default keeps them in review")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    apps = Path(args.apps)
    corpus_opt = corpus if corpus.exists() else None
    apps_opt = apps if apps.exists() else None
    if not corpus_opt and not apps_opt:
        raise SystemExit("Neither corpus nor apps directory exists.")

    idx = AssetIndex(corpus_opt, apps_opt)
    units: list[Unit] = []
    if apps_opt:
        units.extend(collect_app_units(apps_opt, idx))
    if corpus_opt:
        units.extend(collect_corpus_page_units(corpus_opt, idx))
        units.extend(collect_pdf_dictionary_units(corpus_opt))
    enrich_by_exact_stem(units, idx)
    units = dedupe_units([u for u in units if u.confidence >= args.min_confidence and (u.text or u.translation_fr or u.translation_en or u.audio or u.image)])
    outputs(units, Path(args.out))


if __name__ == "__main__":
    main()
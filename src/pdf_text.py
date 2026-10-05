#!/usr/bin/env python3
"""Text from non-dictionary PDFs (books, guides, courses).

* Bilingual books laid out in two columns (local language left, French right, the same passage
  on each page, e.g. "SIDA mooré - français") give one paragraph pair per page. Columns are
  identified by position, which is more reliable than guessing the language of each block.
* Other pages give monolingual paragraphs labelled French, English or the PDF's local language.

  python src/pdf_text.py book.pdf mos      # prints pairs and paragraphs as JSON lines
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pairing_engine import LOCAL_LETTERS_RE, is_english, is_french

# Reverse indexes and the illustrated/alternate dictionary editions duplicate dictionaries that
# are parsed elsewhere (apps or lexique_pdf.py).
# English-medium grammar and course books mix English prose with Fulfulde examples line by line;
# as monolingual text that is noise, so they need a dedicated parser (not done yet).
# The Dioula orthography guide is table-based (word/gloss columns), which line extraction scrambles.
SKIP_NAME_RE = re.compile(r"index|avec_images|et_images|_lexicon_?_|grammar|language_learning|orthographe", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?»”])\s+(?=[«“\"A-ZÀ-ÖØ-Þ\u0186\u0190\u014a])")
GLOSS_RE = re.compile(r"^(.{2,80}?)\s+=\s+(.{2,120})$")
MIN_CHARS = 20


def clean(text: str, local: bool) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # Undo line-break hyphenation. Local orthographies use hyphens inside words ("ges-ba"), so
    # only the line-break space is removed there; French loses the hyphen too ("nourri- ture").
    return re.sub(r"(\w)- (\w)", r"\1-\2" if local else r"\1\2", text)


def page_blocks(page) -> list[tuple[float, float, float, str]]:
    """(x0, x1, y0, text) of the text blocks, without page numbers."""
    out = []
    for x0, y0, x1, _y1, text, *_ in page.get_text("blocks"):
        t = re.sub(r"\s+", " ", text).strip()
        if t and not re.fullmatch(r"[\d\s.\-–]+", t):
            out.append((x0, x1, y0, t))
    return out


def language_of(text: str, local: str) -> str:
    if LOCAL_LETTERS_RE.search(text):
        return local
    if is_french(text):
        return "fr"
    if is_english(text):
        return "en"
    return local


def bilingual_pages(doc, local: str) -> list[dict] | None:
    """Pairs when the book is two-column local/French; None when it is not laid out that way."""
    pairs, two_col, content = [], 0, 0
    for pno, page in enumerate(doc, 1):
        mid = page.rect.width / 2
        blocks = page_blocks(page)
        if not blocks:
            continue
        content += 1
        left = [b for b in blocks if b[1] <= mid + 15]
        right = [b for b in blocks if b[0] >= mid - 15]
        if not left or not right or len(left) + len(right) != len(blocks):
            continue
        ltxt = " ".join(b[3] for b in sorted(left, key=lambda b: b[2]))
        rtxt = " ".join(b[3] for b in sorted(right, key=lambda b: b[2]))
        # Right column must read as French and the left one must not.
        if language_of(rtxt, local) == local and not re.search(r"[éèàçê]", rtxt):
            continue
        if is_french(ltxt) and not LOCAL_LETTERS_RE.search(ltxt):
            continue
        two_col += 1
        pairs.append({"page": pno, "local": clean(ltxt, True), "fr": clean(rtxt, False)})
    if not content or two_col / content < 0.5:
        return None
    # A French word cut at a page break ("Au com-" | "ment de la saison") belongs to the earlier page.
    for a, b in zip(pairs, pairs[1:]):
        m = re.match(r"^([a-zà-ÿ]+)\s*(.*)$", b["fr"])
        if a["fr"].endswith("-") and m:
            a["fr"] = a["fr"][:-1] + m.group(1)
            b["fr"] = m.group(2)
    return pairs


def paragraphs(doc, local: str) -> tuple[list[dict], list[dict]]:
    """Sentences per language, plus "local = French" gloss lines (orthography guides)."""
    out, glosses = [], []
    for pno, page in enumerate(doc, 1):
        blocks = page_blocks(page)
        body = []
        for _x0, _x1, _y0, text in blocks:
            m = GLOSS_RE.match(text)
            if m and language_of(m.group(1), local) == local and has_french_word(m.group(2)):
                glosses.append({"page": pno, "local": clean(m.group(1), True), "fr": clean(m.group(2), False)})
            else:
                body.append(text)
        # PDF blocks are often single lines: rebuild the page text, then split into sentences.
        for sent in SENTENCE_RE.split(" ".join(body)):
            sent = sent.strip()
            if len(sent) < MIN_CHARS or sum(ch.isalpha() for ch in sent) < 0.6 * len(sent):
                continue
            lang = language_of(sent, local)
            out.append({"page": pno, "language": lang, "text": clean(sent, lang not in {"fr", "en"})})
    return out, glosses


def has_french_word(text: str) -> bool:
    return is_french(text) or bool(re.search(r"[éèàçêù]|\b(le|la|les|un|une|des|du|de|nous|vous|tous|pas)\b", text.lower()))


def extract(path: Path, local: str) -> tuple[list[dict], list[dict]]:
    """(pairs, sentences) for one PDF; pairs are bilingual pages or gloss lines."""
    import pymupdf  # optional dependency
    doc = pymupdf.open(path)
    pairs = bilingual_pages(doc, local)
    if pairs is not None:
        return pairs, []
    sentences, glosses = paragraphs(doc, local)
    return glosses, sentences


if __name__ == "__main__":
    pairs, paras = extract(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "mos")
    for row in pairs + paras:
        print(json.dumps(row, ensure_ascii=False))

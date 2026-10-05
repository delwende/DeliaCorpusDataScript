#!/usr/bin/env python3
"""Parse SIL Lexique Pro dictionary PDFs (two-column print exports) into entries.

MooreBurkina publishes its Mooré (and some Dioula/Fulfulde) dictionaries only as PDFs. Their
typography encodes the structure:

  LucidaSansUnicode ~11pt   headword
  LucidaSansUnicode ~9pt    local-language text: field values (plain) and examples (bold)
  Times-Roman 10pt          French (glosses, example translations, category values)
  Times-Italic 10pt         English (glosses, example translations, scientific names)
  Times-Italic 9pt          labels ("orthographe:", "Plural:", part of speech, "Catégorie :")

Bold is faked by over-printing each glyph about four times with ~1pt offsets, which garbles
plain text extraction. Glyphs are therefore de-duplicated by position and the duplicate count
marks text as bold.

  python src/lexique_pdf.py some_dictionary.pdf      # prints parsed entries as JSON lines
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

HEAD, LOCAL, LABEL, FR, EN, OTHER = "head", "local", "label", "fr", "en", "other"

# Labels whose value is a local-language form, not a translation.
FORM_LABELS = re.compile(r"^(orthographe|plural|pluriel|varinat|variante|racine|comparez|antonyme|synonyme|"
                         r"voir|see|cf|singulier|singular)\b", re.I)
CATEGORY_LABEL = re.compile(r"^cat[ée]gorie\b", re.I)


def char_kind(font: str, size: float) -> str:
    f = font.lower()
    if "lucida" in f or "charis" in f or f.startswith("arial") or "doulos" in f or "gentium" in f:
        return HEAD if size >= 10.5 else LOCAL
    if "times" in f:
        if "italic" in f:
            return LABEL if size < 9.5 else EN
        if "bold" in f:
            return OTHER
        return FR
    return OTHER


@dataclass
class Token:
    kind: str
    text: str
    bold: bool = False


def page_tokens(page, header: float = 50, footer: float = 785) -> list[Token]:
    """Font-classified text runs of one page in reading order (column by column)."""
    raw = page.get_text("rawdict")
    mid = page.rect.width / 2
    kept: dict[tuple, list[list]] = {}   # (char, x bucket) -> glyph records
    chars = []
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                kind = char_kind(span["font"], span["size"])
                font_bold = "bold" in span["font"].lower()
                for ch in span["chars"]:
                    c = ch["c"]
                    x, y = ch["origin"]
                    if y < header or y > footer:
                        continue
                    if not c.strip():
                        chars.append([x, y, " ", kind, 1, span["size"]])
                        continue
                    # Over-printed (fake bold) copies sit within ~1pt of each other; genuine
                    # doubled letters ("ll", "ii") are at least ~2.5pt apart.
                    dup = None
                    for b in (int(x // 4) - 1, int(x // 4), int(x // 4) + 1):
                        for rec in kept.get((c, b), ()):
                            if abs(rec[0] - x) <= 1.3 and abs(rec[1] - y) <= 1.3:
                                dup = rec
                                break
                        if dup:
                            break
                    if dup:
                        dup[4] += 1
                        continue
                    rec = [x, y, c, kind, 3 if font_bold else 1, span["size"]]
                    kept.setdefault((c, int(x // 4)), []).append(rec)
                    chars.append(rec)
    # Group into lines per column by baseline.
    lines: dict[tuple[int, int], list] = {}
    for rec in chars:
        col = 0 if rec[0] < mid else 1
        lines.setdefault((col, round(rec[1] / 3)), []).append(rec)
    tokens: list[Token] = []
    for (col, _), recs in sorted(lines.items()):
        recs.sort(key=lambda r: r[0])
        prev_x = None
        for x, y, c, kind, n, size in recs:
            if c == " ":
                if tokens:
                    tokens[-1].text += " "
                prev_x = None
                continue
            gap = prev_x is not None and x - prev_x > size * 0.6
            bold = n >= 3
            # Within a word (no gap) a bold change is a fallback-font glyph, not a new run.
            if tokens and tokens[-1].kind == kind and (kind != LOCAL or tokens[-1].bold == bold or not gap):
                tokens[-1].text += (" " if gap else "") + c
            else:
                if gap and tokens:
                    tokens[-1].text += " "
                tokens.append(Token(kind, c, bold))
            prev_x = x + size * 0.5
        if tokens:
            tokens[-1].text += "\n"  # line break marker, resolved when merging runs
    return tokens


def join_lines(text: str) -> str:
    """Join wrapped lines; Lexique Pro repeats a hyphenated prefix on the next line ("maan-\\nmaan-y")."""
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    out = ""
    for p in parts:
        if out.endswith("-"):
            stem = out.rsplit(" ", 1)[-1]
            out = out[: -len(stem)] + p if p.startswith(stem) else out + p
        else:
            out = (out + " " + p).strip()
    return re.sub(r"\s+", " ", out).strip()


def merge_runs(tokens: list[Token]) -> list[Token]:
    """Merge consecutive same-kind tokens (across line wraps) and ignore OTHER noise."""
    out: list[Token] = []
    for t in tokens:
        if not t.text.strip():
            if out:
                out[-1].text += t.text
            continue
        if out and out[-1].kind == t.kind and (t.kind != LOCAL or out[-1].bold == t.bold):
            out[-1].text += t.text if out[-1].text.endswith(("\n", " ")) else t.text
        else:
            out.append(Token(t.kind, t.text, t.bold))
    for t in out:
        t.text = join_lines(t.text)
    return [t for t in out if t.text]


@dataclass
class Entry:
    headword: str
    phonetic: str = ""
    part_of_speech: str = ""
    senses: list[dict] = field(default_factory=list)     # {"fr", "en"}
    examples: list[dict] = field(default_factory=list)   # {"local", "fr", "en"}
    fields: dict = field(default_factory=dict)           # orthographe, plural, category, ...


def clean_gloss(s: str) -> str:
    s = re.sub(r"^\d+\s*•\s*", "", s.strip())
    return s.strip(" ;.,")


def parse_entries(tokens: list[Token], german: bool = False) -> list[Entry]:
    """german: the export adds a German gloss (Times-Roman, like French) after each English one."""
    entries: list[Entry] = []
    cur: Entry | None = None
    label = ""          # the label the next value belongs to
    pending_fr = ""     # gloss/translation French waiting for its English
    example: dict | None = None
    after_en: dict | None = None  # sense/example whose English was just read (German may follow)

    def flush_pending() -> None:
        nonlocal pending_fr
        if cur is not None and clean_gloss(pending_fr):
            cur.senses.append({"fr": clean_gloss(pending_fr), "en": ""})
        pending_fr = ""

    for t in merge_runs(tokens):
        text = t.text
        if not re.search(r"\w", text):
            continue  # punctuation set in a gloss font
        if t.kind == HEAD:
            flush_pending()
            # Wrapped headwords repeat the hyphenated prefix: keep the longer continuation.
            if cur is not None and not cur.senses and not cur.examples and not cur.fields and cur.headword.endswith("-") \
                    and text.startswith(cur.headword):
                cur.headword = text
                continue
            m = re.match(r"^(.*?)(\d+)?$", text)
            cur = Entry(headword=m.group(1).strip() if m else text)
            if m and m.group(2):
                cur.fields["homonym"] = m.group(2)
            entries.append(cur)
            label, example, after_en = "", None, None
            continue
        if cur is None:
            continue
        if t.kind == LABEL:
            flush_pending()
            example, after_en = None, None
            lab = text.rstrip(" :.")
            if FORM_LABELS.match(lab) or CATEGORY_LABEL.match(lab):
                label = lab.lower().split()[0]
            elif re.match(r"^\[.*\]$", lab):
                cur.phonetic = lab
            else:
                cur.part_of_speech = cur.part_of_speech or lab
                label = ""
            continue
        if t.kind == LOCAL:
            after_en = None
            if text.startswith("[") and not cur.phonetic and "]" in text:
                cur.phonetic, _, rest = text.partition("]")
                cur.phonetic += "]"
                text = rest.strip()
                if not text:
                    continue
            if label.startswith("cat"):
                # Some exports set category names in a bold sans font, like local examples.
                cur.fields["category"] = (cur.fields.get("category", "") + " " + text).strip(" .")
                continue
            if label:
                cur.fields[label] = (cur.fields.get(label, "") + " " + text).strip(" .")
                continue
            if example is not None and not example["fr"]:
                # Glyphs from fallback fonts interrupt bold runs; they belong to the example.
                example["local"] += text if example["local"].endswith(("-", " ")) or not t.bold else " " + text
                continue
            if t.bold:
                flush_pending()
                example = {"local": text.strip(), "fr": "", "en": ""}
                cur.examples.append(example)
                label = ""
            continue
        if t.kind == FR:
            if german and after_en is not None and not re.match(r"^\d+\s*•", text):
                after_en["de"] = clean_gloss(text)
                after_en = None
                continue
            after_en = None
            if label.startswith("cat"):
                cur.fields["category"] = (cur.fields.get("category", "") + " " + text).strip(" .")
                continue
            if example is not None and not example["fr"]:
                example["fr"] = text.strip()
                continue
            label = ""
            # A French gloss can arrive in several runs (e.g. around "(se)"); keep one sense.
            pending_fr = f"{pending_fr.rstrip(' ;,')}; {text}" if clean_gloss(pending_fr) else text
            continue
        if t.kind == EN:
            if example is not None and example["fr"] and not example["en"]:
                example["en"] = text.strip()
                after_en = example
                continue
            if pending_fr:
                cur.senses.append({"fr": clean_gloss(pending_fr), "en": clean_gloss(text)})
                after_en = cur.senses[-1]
                pending_fr = ""
            elif label.startswith("cat") or cur.fields.get("category"):
                cur.fields["scientific"] = (cur.fields.get("scientific", "") + " " + text).strip(" .")
            continue
    flush_pending()
    for e in entries:
        e.headword = strip_section_letters(e.headword)
    for e in entries:
        for ex in e.examples:
            for k in ("local", "fr", "en"):
                ex[k] = re.sub(r"(\s*[.!?])\s*\.$", r"\1", ex[k].strip())
            ex["local"] = ex["local"].strip(" .") + "."  if ex["local"] and not ex["local"].endswith(("?", "!", ".")) else ex["local"]
    return [e for e in entries if e.headword and (e.senses or e.examples)]


def strip_section_letters(headword: str) -> str:
    """Alphabet section headings ("J", "ŋ p s") share a line with the first entry of the section
    and get glued to it ("j jabidafura", "M -minikɛnan"). Drop leading one-letter tokens when the
    word after them starts with that letter or a hyphen; a lone letter is left as is."""
    toks = headword.split()
    i = 0
    while i < len(toks) - 1 and len(toks[i]) == 1:
        i += 1
    if i:
        nxt = toks[i]
        if nxt.startswith("-") or nxt[0].lower() == toks[i - 1].lower():
            return " ".join(toks[i:])
    return headword


def is_lexique_pro_pdf(doc) -> bool:
    sample = "".join(doc[i].get_text() for i in range(min(len(doc), 8)))
    return sample.count("orthographe:") >= 5 or sample.count("Catégorie") >= 5


def parse_pdf(path: Path) -> list[Entry]:
    import pymupdf  # optional dependency: only needed for PDF dictionaries
    doc = pymupdf.open(path)
    if not is_lexique_pro_pdf(doc):
        return []
    sample = " ".join(doc[i].get_text() for i in range(min(len(doc), 15)))
    german = len(re.findall(r"\b(und|der|die|das|für|mit|nicht|ein|eine)\b", sample)) >= 25
    tokens: list[Token] = []
    for page in doc:
        tokens.extend(page_tokens(page))
    return parse_entries(tokens, german=german)


if __name__ == "__main__":
    for e in parse_pdf(Path(sys.argv[1])):
        print(json.dumps(asdict(e), ensure_ascii=False))

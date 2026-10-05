#!/usr/bin/env python3
"""Import Webonary dictionary pages saved from a browser.

Webonary (www.webonary.org) blocks automated access, so pages are saved by hand (browse or search
results, "Save page as… HTML") into data/webonary/<site>/, e.g. data/webonary/moore/a_1.html.
This script parses every saved page of a site into one entry per headword and, with --audio,
downloads each entry's pronunciation recording (public files on Webonary's cloud storage).

Output per site: data/webonary/<site>/entries.jsonl (+ audio/ when --audio is given).

  python src/webonary_import.py --root data/webonary --audio

--fetch saves the browse pages itself, for use from a connection Webonary does not block (e.g. a
home connection; it blocks data-centre addresses). It is deliberately polite: it obeys robots.txt,
waits between pages, identifies itself, and stops at the first block or Cloudflare challenge
instead of trying to get around it. Start from one browse page; letter and page links are
discovered from the pages themselves. Pages already saved are skipped, so a run can be resumed.

  python src/webonary_import.py --fetch "https://www.webonary.org/moore/en/browse/browse-vernacular-english/?key=mos&letter=a" --audio
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from collections import deque
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

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


USER_AGENT = "DeliaCorpusDataScript/1.0 (language corpus for Burkina Faso languages; github.com/delwende/DeliaCorpusDataScript)"
BLOCK_MARKERS = ("cf-chl", "Just a moment", "Attention Required", "cf-browser-verification")


class Blocked(RuntimeError):
    pass


# Query parameters that do not change the page content ("totalEntries" is just a count shown in
# pagination links); ignoring them keeps one URL, and one saved file, per page.
IGNORED_PARAMS = {"totalEntries"}


def canonical(url: str) -> str:
    p = urlparse(url)
    params = sorted((k, v) for k, v in parse_qsl(p.query) if k not in IGNORED_PARAMS)
    # Page 1 is the same with or without "pagenr=1".
    params = [(k, v) for k, v in params if not (k == "pagenr" and v == "1")]
    return p._replace(query=urlencode(params), fragment="").geturl()


def page_filename(url: str) -> str:
    params = [(k, v) for k, v in sorted(parse_qsl(urlparse(canonical(url)).query)) if k != "key"]
    name = "_".join(f"{k}-{v}" for k, v in params) or "index"
    return re.sub(r"[^\w.-]+", "_", name) + ".html"


def browse_links(html: str, page_url: str, start: str) -> set[str]:
    """Letter and pagination links that stay on the same browse view of the same dictionary."""
    base = urlparse(start)
    out = set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(page_url, a["href"])
        p = urlparse(url)
        if p.netloc == base.netloc and p.path == base.path and "letter" in dict(parse_qsl(p.query)):
            out.add(canonical(url))
    return out


def fetch_site(start: str, out_dir: Path, delay: float, max_pages: int) -> dict:
    """Save a dictionary's browse pages; stops (raises Blocked) at the first refusal."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    base = urlparse(start)
    robots = RobotFileParser()
    resp = session.get(f"{base.scheme}://{base.netloc}/robots.txt", timeout=60)
    if resp.status_code != 200 or any(m in resp.text for m in BLOCK_MARKERS):
        raise Blocked(f"robots.txt not readable (HTTP {resp.status_code}): access is blocked from this connection")
    robots.parse(resp.text.splitlines())
    if not robots.can_fetch(USER_AGENT, start):
        raise Blocked("robots.txt disallows these pages for automated clients")
    out_dir.mkdir(parents=True, exist_ok=True)
    queue, seen, fetched, reused = deque([canonical(start)]), {canonical(start)}, 0, 0
    while queue and fetched + reused < max_pages:
        url = queue.popleft()
        dest = out_dir / page_filename(url)
        if dest.exists():
            html = dest.read_text(encoding="utf-8", errors="replace")
            reused += 1
        else:
            if not robots.can_fetch(USER_AGENT, url):
                continue
            time.sleep(delay)
            resp = session.get(url, timeout=60)
            if resp.status_code != 200 or any(m in resp.text for m in BLOCK_MARKERS):
                raise Blocked(f"stopped at {url}: HTTP {resp.status_code}"
                              + (" (Cloudflare challenge)" if any(m in resp.text for m in BLOCK_MARKERS) else ""))
            html = resp.text
            dest.write_text(html, encoding="utf-8")
            fetched += 1
            print(f"saved {dest.name} ({len(parse_page(html))} entries)", flush=True)
        for link in sorted(browse_links(html, url, start) - seen):
            seen.add(link)
            queue.append(link)
    return {"pages_fetched": fetched, "pages_already_saved": reused, "pages_left": len(queue)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default="data/webonary", help="Folder with one sub-folder of saved pages per site")
    ap.add_argument("--audio", action="store_true", help="Download each entry's pronunciation recording")
    ap.add_argument("--delay", type=float, default=0.3, help="Seconds between audio downloads")
    ap.add_argument("--fetch", metavar="BROWSE_URL", help="Save a dictionary's browse pages, starting from this page")
    ap.add_argument("--page-delay", type=float, default=3.0, help="Seconds between page requests with --fetch")
    ap.add_argument("--max-pages", type=int, default=3000, help="Safety limit on pages per --fetch run")
    args = ap.parse_args()
    root = Path(args.root)
    if args.fetch:
        site = urlparse(args.fetch).path.strip("/").split("/")[0]
        try:
            print(site, json.dumps(fetch_site(args.fetch, root / site, args.page_delay, args.max_pages)))
        except Blocked as exc:
            print(f"{site}: {exc}. Saved pages are kept; save the rest by hand or ask the owners for an export.")
    for site_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        print(site_dir.name, json.dumps(import_site(site_dir, args.audio, args.delay)))


if __name__ == "__main__":
    main()

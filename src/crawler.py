#!/usr/bin/env python3
"""Exhaustive inventory crawler for mooreburkina.com.

Outputs (inventory only; does NOT download media):
  pages.csv       - HTML pages visited
  resources.csv   - documents/audio/video/images/apps/external media discovered
  summary.json    - counts by language/type/host/extension

Usage:
  python mooreburkina_crawler.py --out mooreburkina_inventory
  python mooreburkina_crawler.py --out mooreburkina_inventory --max-pages 10000

Requirements:
  pip install requests beautifulsoup4
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, unquote

import requests
from bs4 import BeautifulSoup

BASE_HOSTS = {"mooreburkina.com", "www.mooreburkina.com"}
SEEDS = [
    "https://mooreburkina.com/",
    "https://mooreburkina.com/fr/sitemap",
]

RESOURCE_EXT = {
    # audio
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".m4a": "audio", ".aac": "audio", ".flac": "audio",
    # video
    ".mp4": "video", ".webm": "video", ".mov": "video", ".mkv": "video", ".3gp": "video",
    # images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image", ".svg": "image",
    # documents
    ".pdf": "pdf", ".doc": "document", ".docx": "document", ".txt": "text", ".rtf": "document",
    ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".csv": "data",
    # packages/apps/archives
    ".apk": "app", ".zip": "archive", ".rar": "archive", ".7z": "archive", ".tar": "archive", ".gz": "archive",
    ".epub": "ebook",
}

EXTERNAL_MEDIA_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com",
    "play.google.com", "apps.apple.com", "media.ipsapps.org", "www.ipsapps.org", "ipsapps.org",
}

# Canonical labels. Keep aliases in detection but normalize output.
LANG_ALIASES = {
    "moore": ["mooré", "moore", "mossi", "moosi"],
    "dioula": ["dioula", "jula", "dyula"],
    "fulfulde": ["fulfulde", "fulani", "peul", "peulh", "jelgoore", "gurmaare", "moosiire", "yaagaare", "maasina", "massina"],
    "gulimancema": ["gulimancema", "gulmanchema", "gulmanche", "gourma", "gurmancema"],
    "lobiri": ["lobiri"],
    "lyele": ["lyélé", "lyele"],
    "nuni": ["nuni"],
    "san": ["san de toma", "san", "samo"],
    "french": ["français", "french"],
    "english": ["anglais", "english"],
}

VARIANT_PATTERNS = {
    "fulfulde_jelgoore": ["jelgoore"],
    "fulfulde_gurmaare": ["gurmaare"],
    "fulfulde_moosiire": ["moosiire"],
    "fulfulde_yaagaare": ["yaagaare"],
    "fulfulde_maasina": ["maasina", "massina"],
}

BAD_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")
SKIP_SUFFIXES = ("/contact", "/user/login")


def normalize_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url)
    url, _frag = urldefrag(url)
    return url.strip()


def host(url: str) -> str:
    return urlparse(url).netloc.lower()


def is_internal(url: str) -> bool:
    return host(url) in BASE_HOSTS


def extension(url: str) -> str:
    p = unquote(urlparse(url).path).lower()
    for ext in sorted(RESOURCE_EXT, key=len, reverse=True):
        if p.endswith(ext):
            return ext
    return ""


def classify_resource(url: str, tag: str = "", attrs: dict | None = None) -> str:
    ext = extension(url)
    if ext:
        return RESOURCE_EXT[ext]
    h = host(url)
    if h in {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}:
        return "external_video"
    if h in {"play.google.com", "apps.apple.com"}:
        return "app_store"
    if h in {"media.ipsapps.org", "www.ipsapps.org", "ipsapps.org"}:
        return "external_interactive"
    if tag in {"audio", "source"} and attrs:
        typ = (attrs.get("type") or "").lower()
        if typ.startswith("audio/"):
            return "audio"
        if typ.startswith("video/"):
            return "video"
    if tag == "img":
        return "image"
    if tag in {"video", "iframe"}:
        return "video_or_embed"
    return "link"


def detect_languages(text: str, url: str = "") -> list[str]:
    blob = f"{unquote(url)} {text}".lower()
    found = []
    for canonical, aliases in LANG_ALIASES.items():
        if any(a in blob for a in aliases):
            found.append(canonical)
    # URL sections are strong signals.
    path = unquote(urlparse(url).path).lower()
    if "/dioula" in path and "dioula" not in found:
        found.append("dioula")
    if "/fulfulde" in path and "fulfulde" not in found:
        found.append("fulfulde")
    if "/gulimancema" in path and "gulimancema" not in found:
        found.append("gulimancema")
    if ("/contes-proverbes" in path or "/dictionnaire-moor" in path or "/livres-en-moor" in path) and "moore" not in found:
        found.append("moore")
    return sorted(set(found))


def detect_variants(text: str, url: str = "") -> list[str]:
    blob = f"{unquote(url)} {text}".lower()
    return sorted({name for name, pats in VARIANT_PATTERNS.items() if any(p in blob for p in pats)})


def infer_collection(title: str, url: str, anchor: str = "") -> str:
    s = f"{title} {anchor} {unquote(url)}".lower()
    rules = [
        ("dictionary", ["dictionnaire", "dictionary", "lexique", "index - français", "index - anglais"]),
        ("folktale", ["conte", "folktale", "sagesse du moogo"]),
        ("proverb", ["proverbe"]),
        ("riddle", ["devinette"]),
        ("poem", ["poème", "poeme"]),
        ("orthography", ["orthographe", "alphabet", "grammar", "grammaire", "language learning"]),
        ("religious", ["evangile", "parole de dieu", "jésus", "jesus", "magdala", "justice", "nouveau testament", "matthieu", "marc", "luc"]),
        ("music", ["chant", "musique", "hymne", "chorale"]),
        ("image_culture", ["image", "tissus", "animaux", "ouagadougou", "quotidien"]),
        ("app", ["appli", "application", "play store", "app store"]),
        ("book_document", ["livre", "document", ".pdf"]),
    ]
    for label, keys in rules:
        if any(k in s for k in keys):
            return label
    return "other"


def text_clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


def crawl(out_dir: Path, max_pages: int, delay: float, timeout: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": "AI-KING-LanguageInventory/1.0 (+research inventory; no bulk media download)",
        "Accept-Language": "fr,en;q=0.8",
    })

    queue = deque(SEEDS)
    queued = set(SEEDS)
    visited: set[str] = set()
    pages: list[dict] = []
    resources: dict[str, dict] = {}
    errors: list[dict] = []

    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            final_url = normalize_url(resp.url)
            ctype = (resp.headers.get("content-type") or "").lower()
            status = resp.status_code

            if "text/html" not in ctype:
                resources.setdefault(final_url, {
                    "resource_id": sha1(final_url), "resource_url": final_url,
                    "resource_type": classify_resource(final_url), "extension": extension(final_url),
                    "language": "", "variant": "", "collection": "other", "source_page": url,
                    "page_title": "", "anchor_text": "", "tag": "direct", "external_host": host(final_url),
                    "http_status": status, "content_type": ctype, "notes": "discovered_as_non_html_page",
                })
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            title = text_clean((soup.title.string if soup.title and soup.title.string else ""))
            h1 = text_clean(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
            main_text = text_clean(soup.get_text(" ", strip=True))[:10000]
            langs = detect_languages(f"{title} {h1} {main_text[:2500]}", final_url)
            variants = detect_variants(f"{title} {h1} {main_text[:2500]}", final_url)
            collection = infer_collection(title or h1, final_url)

            pages.append({
                "page_id": sha1(final_url), "url": final_url, "title": title or h1,
                "language": ";".join(langs), "variant": ";".join(variants),
                "collection": collection, "http_status": status, "content_type": ctype,
                "text_chars": len(main_text),
            })

            # Extract a/src/href plus lazy-load/data-* variants commonly used by Drupal/media players.
            candidates: list[tuple[str, str, str, dict]] = []
            for tag in soup.find_all(["a", "audio", "video", "source", "img", "iframe"]):
                attrs = dict(tag.attrs)
                raw = attrs.get("href") or attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src")
                if not raw or not isinstance(raw, str):
                    continue
                if raw.lower().startswith(BAD_SCHEMES):
                    continue
                absolute = normalize_url(raw, final_url)
                if not absolute.startswith(("http://", "https://")):
                    continue
                anchor = text_clean(tag.get_text(" ", strip=True) or attrs.get("alt") or attrs.get("title") or "")
                candidates.append((tag.name, absolute, anchor, attrs))

            for tag_name, link, anchor, attrs in candidates:
                ext = extension(link)
                link_host = host(link)
                rtype = classify_resource(link, tag_name, attrs)

                # Recursively queue same-domain HTML-looking pages.
                if is_internal(link) and not ext and tag_name == "a":
                    lowpath = unquote(urlparse(link).path).lower().rstrip("/")
                    if not any(lowpath.endswith(s) for s in SKIP_SUFFIXES) and link not in visited and link not in queued:
                        queue.append(link)
                        queued.add(link)

                # Store actual assets, embeds and useful external destinations.
                should_store = bool(ext) or tag_name in {"audio", "video", "source", "img", "iframe"} or link_host in EXTERNAL_MEDIA_HOSTS
                if should_store:
                    rlangs = detect_languages(f"{title} {anchor}", f"{final_url} {link}") or langs
                    rvars = detect_variants(f"{title} {anchor}", f"{final_url} {link}") or variants
                    key = link
                    row = resources.get(key)
                    if row is None:
                        resources[key] = {
                            "resource_id": sha1(link), "resource_url": link,
                            "resource_type": rtype, "extension": ext,
                            "language": ";".join(rlangs), "variant": ";".join(rvars),
                            "collection": infer_collection(title, final_url, anchor),
                            "source_page": final_url, "page_title": title or h1, "anchor_text": anchor,
                            "tag": tag_name, "external_host": link_host if not is_internal(link) else "",
                            "http_status": "", "content_type": attrs.get("type", ""), "notes": "",
                        }
                    else:
                        # Preserve multiple source pages without duplicating the asset.
                        sources = set(filter(None, row["source_page"].split(";")))
                        sources.add(final_url)
                        row["source_page"] = ";".join(sorted(sources))

            time.sleep(delay)

        except Exception as exc:
            errors.append({"url": url, "error": repr(exc)})

        if len(visited) % 25 == 0:
            print(f"visited={len(visited)} queued={len(queue)} resources={len(resources)} errors={len(errors)}")

    page_fields = ["page_id", "url", "title", "language", "variant", "collection", "http_status", "content_type", "text_chars"]
    res_fields = ["resource_id", "resource_url", "resource_type", "extension", "language", "variant", "collection", "source_page", "page_title", "anchor_text", "tag", "external_host", "http_status", "content_type", "notes"]

    with (out_dir / "pages.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=page_fields)
        w.writeheader(); w.writerows(sorted(pages, key=lambda x: x["url"]))
    with (out_dir / "resources.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=res_fields)
        w.writeheader(); w.writerows(sorted(resources.values(), key=lambda x: (x["language"], x["resource_type"], x["resource_url"])))
    with (out_dir / "errors.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["url", "error"])
        w.writeheader(); w.writerows(errors)

    lang_counter = Counter()
    type_counter = Counter()
    host_counter = Counter()
    ext_counter = Counter()
    for r in resources.values():
        for lang in filter(None, r["language"].split(";")):
            lang_counter[lang] += 1
        type_counter[r["resource_type"]] += 1
        ext_counter[r["extension"] or "(none)"] += 1
        if r["external_host"]:
            host_counter[r["external_host"]] += 1

    summary = {
        "seed_urls": SEEDS,
        "pages_visited": len(visited),
        "html_pages_recorded": len(pages),
        "unique_resources": len(resources),
        "errors": len(errors),
        "by_language": dict(lang_counter.most_common()),
        "by_resource_type": dict(type_counter.most_common()),
        "by_extension": dict(ext_counter.most_common()),
        "external_hosts": dict(host_counter.most_common()),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="mooreburkina_inventory", help="Output directory")
    ap.add_argument("--max-pages", type=int, default=10000)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()
    crawl(Path(args.out), args.max_pages, args.delay, args.timeout)


if __name__ == "__main__":
    main()
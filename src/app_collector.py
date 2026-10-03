#!/usr/bin/env python3
"""Collect static embedded language apps referenced by MooreBurkina.

The main MooreBurkina pages often embed interactive resources hosted at media.ipsapps.org.
Those apps may contain the actual aligned text/audio/image assets. This collector crawls each
referenced app *within its own directory prefix*, downloads its static files, and writes a
manifest suitable for the pairing/alignment engine.

Inputs:
  - inventory/resources.csv produced by mooreburkina_crawler.py, OR
  - one or more --url values.

Outputs under <out>:
  apps/<app_id>/... downloaded static files preserving relative paths
  app_manifest.csv
  app_manifest.jsonl
  app_summary.json
  errors.csv

Examples:
  python mooreburkina_app_collector.py --inventory mooreburkina_inventory --out MooreBurkinaApps
  python mooreburkina_app_collector.py --url https://media.ipsapps.org/mos/ora/p1/ --language moore

Requirements:
  pip install requests beautifulsoup4
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urljoin, urlparse, urldefrag, unquote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

APP_HOSTS = {"media.ipsapps.org", "www.media.ipsapps.org"}
TEXT_EXTS = {".html", ".htm", ".js", ".json", ".xml", ".css", ".txt", ".csv", ".vtt", ".srt", ".svg"}
MEDIA_EXTS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus",
    ".mp4", ".webm", ".mov", ".mkv", ".3gp",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
}
OTHER_STATIC_EXTS = {".woff", ".woff2", ".ttf", ".eot", ".ico", ".pdf", ".zip"}
STATIC_EXTS = TEXT_EXTS | MEDIA_EXTS | OTHER_STATIC_EXTS
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
TEXT_KINDS = {"html", "javascript", "json", "xml", "css", "text"}

# ISO-ish URL codes observed/expected in IPS app paths.
PATH_LANGUAGE_CODES = {
    "mos": "moore",
    "dyu": "dioula",
    "jul": "dioula",
    "gux": "gulimancema",
    "fub": "fulfulde",
    "ful": "fulfulde",
    "ff": "fulfulde",
}

MANIFEST_FIELDS = [
    "app_id", "app_url", "language", "variant", "collection", "source_page",
    "url", "referer", "local_path", "kind", "extension", "content_type",
    "http_status", "bytes", "sha256", "status", "downloaded_at", "error",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    url = url.strip()
    # Seeds like ".../vol4//" would otherwise make the app prefix "/vol4//", which no real
    # page path starts with, so every page was treated as outside the app and skipped.
    p = urlparse(url)
    if "//" in p.path:
        url = p._replace(path=re.sub(r"/{2,}", "/", p.path)).geturl()
    return url


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def app_prefix(url: str) -> str:
    """Directory URL used as the crawl sandbox for one app."""
    p = urlparse(url)
    path = p.path or "/"
    if not path.endswith("/"):
        path = str(PurePosixPath(path).parent) + "/"
    return f"{p.scheme}://{p.netloc}{path}"


def within_prefix(url: str, prefix: str) -> bool:
    u, p = urlparse(url), urlparse(prefix)
    return u.netloc.lower() == p.netloc.lower() and unquote(u.path).startswith(unquote(p.path))


def ext_from_url(url: str) -> str:
    return Path(unquote(urlparse(url).path)).suffix.lower()


def kind_from(ext: str, content_type: str = "") -> str:
    ct = (content_type or "").lower()
    if ext in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"} or ct.startswith("audio/"):
        return "audio"
    if ext in {".mp4", ".webm", ".mov", ".mkv", ".3gp"} or ct.startswith("video/"):
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"} or ct.startswith("image/"):
        return "image"
    if ext in {".html", ".htm"} or "text/html" in ct:
        return "html"
    if ext == ".json" or "application/json" in ct:
        return "json"
    if ext == ".js" or "javascript" in ct:
        return "javascript"
    if ext == ".css" or "text/css" in ct:
        return "css"
    if ext == ".xml" or "xml" in ct:
        return "xml"
    if ext in {".txt", ".csv", ".vtt", ".srt"} or ct.startswith("text/"):
        return "text"
    if ext == ".pdf" or "application/pdf" in ct:
        return "pdf"
    return "other"


def infer_language_from_url(url: str) -> str:
    parts = [p.lower() for p in PurePosixPath(unquote(urlparse(url).path)).parts]
    for p in parts:
        if p in PATH_LANGUAGE_CODES:
            return PATH_LANGUAGE_CODES[p]
    return ""


def make_session(user_agent: str, retries: int) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": user_agent, "Accept-Language": "fr,en;q=0.8"})
    return s


def local_rel_path(app_id: str, url: str, prefix: str, content_type: str = "") -> Path:
    parsed = urlparse(url)
    pfx = urlparse(prefix).path
    if parsed.path.startswith(pfx):
        rel = unquote(parsed.path[len(pfx):])
    else:
        # Shared static asset outside the app prefix: preserve enough host path to avoid collisions.
        rel = "_shared/" + unquote(parsed.path).lstrip("/")
    rel = rel.lstrip("/")
    if not rel or rel.endswith("/"):
        rel = rel.rstrip("/") + ("/" if rel else "") + "index.html"
    rel_path = Path(*[x for x in PurePosixPath(rel).parts if x not in {"", ".", ".."}])
    if not rel_path.suffix:
        guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip()) or ""
        if "text/html" in (content_type or "").lower():
            guessed = ".html"
        if guessed:
            rel_path = rel_path.with_suffix(guessed)
    # Query strings can identify distinct resources with same path.
    if parsed.query:
        qh = sha1(parsed.query)[:8]
        rel_path = rel_path.with_name(f"{rel_path.stem}__q{qh}{rel_path.suffix}")
    return Path("apps") / app_id / rel_path


def extract_refs_from_html(text: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(text, "html.parser")
    out: set[str] = set()
    attrs_to_check = ["href", "src", "data-src", "data-file", "data-audio", "data-sound", "poster"]
    for tag in soup.find_all(True):
        for attr in attrs_to_check:
            raw = tag.get(attr)
            if isinstance(raw, str) and raw.strip() and not raw.lower().startswith(("data:", "javascript:", "mailto:", "tel:")):
                out.add(clean_url(raw, base_url))
        srcset = tag.get("srcset")
        if isinstance(srcset, str):
            for piece in srcset.split(","):
                raw = piece.strip().split(" ", 1)[0]
                if raw:
                    out.add(clean_url(raw, base_url))
    # Some apps encode asset paths in inline JS/data attributes.
    out.update(extract_refs_from_text(text, base_url))
    return {u for u in out if u.startswith(("http://", "https://"))}


def extract_refs_from_text(text: str, base_url: str) -> set[str]:
    out: set[str] = set()
    # Quoted relative/absolute static paths, including cache-busting queries.
    pattern = re.compile(
        r"(?P<q>['\"])(?P<url>(?!data:|javascript:|mailto:)[^'\"\s<>]{1,500}?"
        r"(?:\.mp3|\.wav|\.ogg|\.m4a|\.aac|\.flac|\.opus|\.mp4|\.webm|\.mov|\.mkv|\.3gp|"
        r"\.jpg|\.jpeg|\.png|\.gif|\.webp|\.svg|\.json|\.xml|\.csv|\.txt|\.vtt|\.srt|\.js|\.css|\.html?|\.pdf)"
        r"(?:\?[^'\"\s<>]*)?)(?P=q)",
        flags=re.I,
    )
    for m in pattern.finditer(text):
        out.add(clean_url(m.group("url"), base_url))
    # url(...) in CSS.
    for m in re.finditer(r"url\(\s*['\"]?([^)'\"\s]+)['\"]?\s*\)", text, flags=re.I):
        raw = m.group(1)
        if not raw.lower().startswith("data:"):
            out.add(clean_url(raw, base_url))
    return {u for u in out if u.startswith(("http://", "https://"))}


@dataclass
class Seed:
    url: str
    language: str = ""
    variant: str = ""
    collection: str = ""
    source_page: str = ""


class Collector:
    def __init__(self, out: Path, timeout: float, delay: float, max_files: int, max_file_bytes: int | None,
                 session_factory, workers: int = 1, skip_images: bool = False):
        self.out = out
        self.timeout = timeout
        self.delay = delay
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.session_factory = session_factory
        self.workers = max(1, workers)
        self.skip_images = skip_images
        self._local = threading.local()
        self.rows: list[dict[str, str]] = []
        self.errors: list[dict[str, str]] = []

    @property
    def session(self) -> requests.Session:
        # requests.Session is not guaranteed thread-safe: one per worker thread.
        if not hasattr(self._local, "session"):
            self._local.session = self.session_factory()
        return self._local.session

    def fetch(self, url: str, referer: str, root: str, prefix: str, app_id: str, base_row: dict[str, str]
              ) -> tuple[dict[str, str], str, str]:
        """Download (or reuse) one file. Returns (row, kind, text for link discovery)."""
        row = dict(base_row, url=url, referer=referer, extension=ext_from_url(url))
        # Resume: files are written via .part + rename, so an existing file is complete.
        existing = self.out / local_rel_path(app_id, url, prefix)
        if existing.is_file():
            kind = kind_from(existing.suffix.lower())
            row.update(local_path=existing.relative_to(self.out).as_posix(), kind=kind,
                       extension=existing.suffix.lower(), bytes=str(existing.stat().st_size),
                       sha256=sha256_file(existing), status="already_present", downloaded_at=utcnow())
            text = existing.read_text(encoding="utf-8", errors="replace") if kind in TEXT_KINDS else ""
            return row, kind, text
        part = None
        try:
            with self.session.get(url, stream=True, timeout=self.timeout, allow_redirects=True) as resp:
                row["http_status"] = str(resp.status_code)
                ctype = resp.headers.get("content-type", "")
                row["content_type"] = ctype
                resp.raise_for_status()
                clen = resp.headers.get("content-length")
                if clen and self.max_file_bytes is not None and int(clen) > self.max_file_bytes:
                    row.update(status="skipped_too_large", error=f"content-length {clen} > max")
                    return row, "", ""
                rel = local_rel_path(app_id, resp.url, prefix, ctype)
                dest = self.out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_suffix(dest.suffix + f".{threading.get_ident()}.part")
                total = 0
                with part.open("wb") as f:
                    for chunk in resp.iter_content(1024 * 512):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if self.max_file_bytes is not None and total > self.max_file_bytes:
                            raise ValueError(f"stream exceeded max file size ({total} bytes)")
                        f.write(chunk)
                os.replace(part, dest)
                kind = kind_from(dest.suffix.lower(), ctype)
                row.update(
                    local_path=rel.as_posix(), kind=kind, extension=dest.suffix.lower(),
                    bytes=str(dest.stat().st_size), sha256=sha256_file(dest),
                    status="downloaded", downloaded_at=utcnow(),
                )
                text = dest.read_text(encoding="utf-8", errors="replace") if kind in TEXT_KINDS else ""
                # Children resolve against the final URL after redirects.
                row["_base"] = resp.url
                return row, kind, text
        except Exception as exc:
            try:
                if part is not None and part.exists():
                    part.unlink()
            except Exception:
                pass
            row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            self.errors.append({"app_url": root, "url": url, "error": row["error"]})
            return row, "", ""
        finally:
            if self.delay:
                time.sleep(self.delay)

    def crawl_seed(self, seed: Seed) -> None:
        root = clean_url(seed.url)
        prefix = app_prefix(root)
        app_id = sha1(root)
        lang = seed.language or infer_language_from_url(root)
        base_row = {
            "app_id": app_id, "app_url": root, "language": lang,
            "variant": seed.variant, "collection": seed.collection,
            "source_page": seed.source_page, "url": "", "referer": "",
            "local_path": "", "kind": "", "extension": "",
            "content_type": "", "http_status": "", "bytes": "", "sha256": "",
            "status": "", "downloaded_at": "", "error": "",
        }
        q = deque([(root, "")])
        queued = {root}
        visited: set[str] = set()

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while q and (self.max_files <= 0 or len(visited) < self.max_files):
                # Breadth-first in waves; each wave is fetched concurrently.
                room = self.workers * 4 if self.max_files <= 0 else min(self.workers * 4, self.max_files - len(visited))
                batch = []
                while q and len(batch) < room:
                    url, referer = q.popleft()
                    if url not in visited:
                        visited.add(url)
                        batch.append((url, referer))
                results = pool.map(lambda ur: self.fetch(ur[0], ur[1], root, prefix, app_id, base_row), batch)
                for (url, _), (row, kind, text) in zip(batch, results):
                    base = row.pop("_base", url)
                    self.rows.append(row)
                    # Discover children only from text-like app files.
                    if kind not in TEXT_KINDS or not text:
                        continue
                    refs = extract_refs_from_html(text, base) if kind == "html" else extract_refs_from_text(text, base)
                    for child in refs:
                        cext = ext_from_url(child)
                        same_host = urlparse(child).netloc.lower() == urlparse(prefix).netloc.lower()
                        inside = within_prefix(child, prefix)
                        # HTML/routes are sandboxed to the app directory so we never crawl the entire host.
                        # Static assets/data discovered by the app may live in shared parent directories.
                        if not inside:
                            if not same_host or cext not in STATIC_EXTS or cext in {".html", ".htm"}:
                                continue
                        if cext and cext not in STATIC_EXTS:
                            continue
                        if self.skip_images and cext in IMAGE_EXTS:
                            continue
                        if child not in queued and child not in visited:
                            q.append((child, url))
                            queued.add(child)

        print(f"app={root} files={len(visited)} queued_remaining={len(q)}", flush=True)


def collect_seeds(inventory: Path | None, explicit_urls: list[str], language: str) -> list[Seed]:
    seeds: dict[str, Seed] = {}
    if inventory:
        resources = read_csv(inventory / "resources.csv")
        for r in resources:
            url = r.get("resource_url", "")
            h = urlparse(url).netloc.lower()
            if h not in APP_HOSTS:
                continue
            # Iframe/app root entries, not arbitrary external media already linked elsewhere.
            rt = (r.get("resource_type") or "").lower()
            if rt not in {"external_interactive", "video_or_embed", "link", ""} and not url.endswith("/"):
                continue
            seeds.setdefault(url, Seed(
                url=url,
                language=r.get("language", "") or infer_language_from_url(url),
                variant=r.get("variant", ""),
                collection=r.get("collection", ""),
                source_page=r.get("source_page", ""),
            ))
    for url in explicit_urls:
        seeds[url] = Seed(url=url, language=language or infer_language_from_url(url))
    return list(seeds.values())


def write_csv(path: Path, rows: Iterable[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect static language apps embedded by MooreBurkina")
    ap.add_argument("--inventory", help="Inventory directory containing resources.csv")
    ap.add_argument("--url", action="append", default=[], help="Explicit app URL; repeatable")
    ap.add_argument("--language", default="", help="Language for explicit --url seeds")
    ap.add_argument("--out", default="MooreBurkinaApps")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--max-files-per-app", type=int, default=10000, help="0 = unlimited")
    ap.add_argument("--max-file-mb", type=float, default=0.0, help="0 = unlimited")
    ap.add_argument("--user-agent", default="AI-KING-LanguageCorpus/1.1")
    ap.add_argument("--workers", type=int, default=1, help="Concurrent downloads per app")
    ap.add_argument("--skip-images", action="store_true", help="Do not download image files")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    inventory = Path(args.inventory) if args.inventory else None
    seeds = collect_seeds(inventory, args.url, args.language)
    if not seeds:
        raise SystemExit("No media.ipsapps.org app URLs found. Supply --inventory or --url.")

    max_bytes = None if args.max_file_mb <= 0 else int(args.max_file_mb * 1024 * 1024)
    collector = Collector(out, args.timeout, args.delay, args.max_files_per_app, max_bytes,
                          lambda: make_session(args.user_agent, args.retries),
                          workers=args.workers, skip_images=args.skip_images)

    def write_manifests() -> None:
        rows = sorted(collector.rows, key=lambda r: (r["app_url"], r["url"]))
        write_csv(out / "app_manifest.csv", rows, MANIFEST_FIELDS)
        with (out / "app_manifest.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        write_csv(out / "errors.csv", collector.errors, ["app_url", "url", "error"])

    for i, seed in enumerate(seeds, 1):
        print(f"[{i}/{len(seeds)}] {seed.url} language={seed.language or '?'} collection={seed.collection or '?'}", flush=True)
        collector.crawl_seed(seed)
        # Rewrite after every app so an interrupted run keeps a usable manifest.
        write_manifests()

    summary = {
        "generated_at": utcnow(),
        "apps": len(seeds),
        "items": len(collector.rows),
        "downloaded": sum(r["status"] in {"downloaded", "already_present"} for r in collector.rows),
        "errors": len(collector.errors),
        "bytes": sum(int(r["bytes"] or 0) for r in collector.rows),
        "by_kind": dict(Counter(r["kind"] or "(unknown)" for r in collector.rows).most_common()),
        "by_language": dict(Counter(r["language"] or "(unknown)" for r in collector.rows).most_common()),
    }
    (out / "app_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
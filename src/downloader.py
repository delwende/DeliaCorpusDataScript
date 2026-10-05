#!/usr/bin/env python3
"""Download and organize a MooreBurkina inventory produced by mooreburkina_crawler.py.

This script consumes:
  <inventory>/resources.csv
  <inventory>/pages.csv

It downloads direct HTTP(S) assets, saves page HTML + clean text, organizes data by
language/variant/collection/type, and writes durable provenance manifests.

Examples:
  python mooreburkina_downloader.py --inventory mooreburkina_inventory --out MooreBurkinaCorpus
  python mooreburkina_downloader.py --inventory mooreburkina_inventory --out MooreBurkinaCorpus --language moore
  python mooreburkina_downloader.py --inventory mooreburkina_inventory --out MooreBurkinaCorpus --type audio --limit 100
  python mooreburkina_downloader.py --inventory mooreburkina_inventory --out MooreBurkinaCorpus --dry-run

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
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_HOSTS = {"mooreburkina.com", "www.mooreburkina.com"}
DIRECT_TYPES = {
    "audio", "video", "image", "pdf", "document", "text", "spreadsheet",
    "data", "app", "archive", "ebook", "external_interactive",
}
NON_DIRECT_TYPES = {"external_video", "app_store", "video_or_embed", "link"}
# Not needed for the text/audio language corpus: several GB of video and images, plus Android APKs.
DEFAULT_SKIP_TYPES = {"video", "image", "app"}

MANIFEST_FIELDS = [
    "item_kind", "resource_id", "source_url", "source_page", "page_title",
    "language", "variant", "collection", "resource_type", "extension",
    "status", "local_path", "bytes", "sha256", "content_type", "http_status",
    "etag", "last_modified", "downloaded_at", "error",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def split_field(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(";") if x.strip()]


def slug(value: str | None, fallback: str = "unknown", max_len: int = 90) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    # Preserve readable Latin letters while eliminating path-hostile characters.
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9._+-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    return (value[:max_len] or fallback)


def canonical_language_dir(value: str | None) -> str:
    langs = sorted(set(split_field(value)))
    if not langs:
        return "unknown"
    if len(langs) == 1:
        return slug(langs[0])
    return "multilingual__" + "_".join(slug(x) for x in langs)


def canonical_variant_dir(value: str | None) -> str:
    variants = sorted(set(split_field(value)))
    if not variants:
        return "general"
    if len(variants) == 1:
        return slug(variants[0])
    return "multi_variant__" + "_".join(slug(x) for x in variants)


def url_basename(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip()
    return name


def extension_from_content_type(content_type: str) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(ctype) or ""
    if ext == ".jpe":
        ext = ".jpg"
    return ext


def choose_filename(row: dict[str, str], content_type: str = "") -> str:
    rid = row.get("resource_id") or hashlib.sha1((row.get("resource_url") or "").encode()).hexdigest()[:16]
    raw = url_basename(row.get("resource_url") or "")
    ext = (row.get("extension") or "").strip().lower()
    if not ext:
        ext = Path(raw).suffix.lower() if raw else ""
    if not ext and content_type:
        ext = extension_from_content_type(content_type)
    stem = Path(raw).stem if raw else "resource"
    stem = slug(stem, fallback="resource", max_len=100)
    return f"{stem}__{rid}{ext}"


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def make_session(user_agent: str, retries: int) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": user_agent, "Accept-Language": "fr,en;q=0.8"})
    return s


def same_site(url: str) -> bool:
    return urlparse(url).netloc.lower() in BASE_HOSTS


def matches_filters(row: dict[str, str], languages: set[str], types: set[str]) -> bool:
    if languages:
        row_langs = {x.lower() for x in split_field(row.get("language"))}
        if not (row_langs & languages):
            return False
    if types and (row.get("resource_type") or "").lower() not in types:
        return False
    return True


def asset_folder(root: Path, row: dict[str, str]) -> Path:
    return (
        root
        / "assets"
        / canonical_language_dir(row.get("language"))
        / canonical_variant_dir(row.get("variant"))
        / slug(row.get("collection"), "other")
        / slug(row.get("resource_type"), "other")
    )


def page_folder(root: Path, row: dict[str, str]) -> Path:
    return (
        root
        / "pages"
        / canonical_language_dir(row.get("language"))
        / canonical_variant_dir(row.get("variant"))
        / slug(row.get("collection"), "other")
    )


def write_manifest_csv(path: Path, rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})


def write_manifest_jsonl(path: Path, rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({k: row.get(k, "") for k in MANIFEST_FIELDS}, ensure_ascii=False) + "\n")


def load_existing_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        key = f"{row.get('item_kind','')}::{row.get('resource_id','')}"
        out[key] = row
    return out


def clean_page_text(html: bytes, apparent_encoding: str | None = None) -> str:
    # BeautifulSoup can usually determine encoding; decode only when useful.
    if apparent_encoding:
        try:
            doc = html.decode(apparent_encoding, errors="replace")
        except LookupError:
            doc = html.decode("utf-8", errors="replace")
    else:
        doc = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(doc, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


@dataclass
class Downloader:
    session: requests.Session
    root: Path
    timeout: float
    delay: float
    max_bytes: int | None
    dry_run: bool

    def download_asset(self, row: dict[str, str]) -> dict[str, str]:
        url = row.get("resource_url", "")
        rid = row.get("resource_id", "")
        base = {
            "item_kind": "asset",
            "resource_id": rid,
            "source_url": url,
            "source_page": row.get("source_page", ""),
            "page_title": row.get("page_title", ""),
            "language": row.get("language", ""),
            "variant": row.get("variant", ""),
            "collection": row.get("collection", ""),
            "resource_type": row.get("resource_type", ""),
            "extension": row.get("extension", ""),
            "status": "",
            "local_path": "",
            "bytes": "",
            "sha256": "",
            "content_type": "",
            "http_status": "",
            "etag": "",
            "last_modified": "",
            "downloaded_at": "",
            "error": "",
        }
        if not url.startswith(("http://", "https://")):
            base.update(status="skipped_non_http", error="non-http URL")
            return base

        rtype = (row.get("resource_type") or "").lower()
        if rtype in NON_DIRECT_TYPES:
            base.update(status="reference_only")
            return base

        folder = asset_folder(self.root, row)
        filename = choose_filename(row)
        dest = folder / filename
        rel = dest.relative_to(self.root).as_posix()
        base["local_path"] = rel

        if dest.exists() and dest.stat().st_size > 0:
            base.update(
                status="already_present",
                bytes=str(dest.stat().st_size),
                sha256=sha256_file(dest),
                downloaded_at=utcnow(),
            )
            return base

        if self.dry_run:
            base.update(status="planned")
            return base

        folder.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        headers = {}
        resume_from = part.stat().st_size if part.exists() else 0
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

        try:
            with self.session.get(url, stream=True, timeout=self.timeout, allow_redirects=True, headers=headers) as resp:
                base["http_status"] = str(resp.status_code)
                base["content_type"] = resp.headers.get("content-type", "")
                base["etag"] = resp.headers.get("etag", "")
                base["last_modified"] = resp.headers.get("last-modified", "")
                resp.raise_for_status()

                # If server ignored Range, start over.
                mode = "ab" if resume_from and resp.status_code == 206 else "wb"
                if mode == "wb":
                    resume_from = 0

                content_length = resp.headers.get("content-length")
                if content_length and self.max_bytes is not None:
                    expected = int(content_length) + resume_from
                    if expected > self.max_bytes:
                        base.update(status="skipped_too_large", error=f"expected {expected} bytes > max {self.max_bytes}")
                        return base

                total = resume_from
                with part.open(mode) as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if self.max_bytes is not None and total > self.max_bytes:
                            f.close()
                            try:
                                part.unlink()
                            except OSError:
                                pass
                            base.update(status="skipped_too_large", error=f"stream exceeded max {self.max_bytes} bytes")
                            return base
                        f.write(chunk)

            # Refine extension from returned MIME only if source had no extension.
            if not row.get("extension") and not dest.suffix:
                ext = extension_from_content_type(base["content_type"])
                if ext:
                    new_dest = dest.with_suffix(ext)
                    dest = new_dest
                    base["local_path"] = dest.relative_to(self.root).as_posix()
                    base["extension"] = ext
            os.replace(part, dest)
            base.update(
                status="downloaded",
                bytes=str(dest.stat().st_size),
                sha256=sha256_file(dest),
                downloaded_at=utcnow(),
            )
        except Exception as exc:
            base.update(status="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            if self.delay:
                time.sleep(self.delay)
        return base

    def download_page(self, row: dict[str, str]) -> dict[str, str]:
        url = row.get("url", "")
        rid = row.get("page_id", "")
        folder = page_folder(self.root, row)
        html_path = folder / f"{rid}.html"
        txt_path = folder / f"{rid}.txt"
        base = {
            "item_kind": "page",
            "resource_id": rid,
            "source_url": url,
            "source_page": url,
            "page_title": row.get("title", ""),
            "language": row.get("language", ""),
            "variant": row.get("variant", ""),
            "collection": row.get("collection", ""),
            "resource_type": "html_page",
            "extension": ".html",
            "status": "",
            "local_path": html_path.relative_to(self.root).as_posix(),
            "bytes": "",
            "sha256": "",
            "content_type": "",
            "http_status": "",
            "etag": "",
            "last_modified": "",
            "downloaded_at": "",
            "error": "",
        }
        if html_path.exists() and txt_path.exists() and html_path.stat().st_size > 0:
            base.update(
                status="already_present",
                bytes=str(html_path.stat().st_size),
                sha256=sha256_file(html_path),
                downloaded_at=utcnow(),
            )
            return base
        if self.dry_run:
            base.update(status="planned")
            return base
        folder.mkdir(parents=True, exist_ok=True)
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            base["http_status"] = str(resp.status_code)
            base["content_type"] = resp.headers.get("content-type", "")
            base["etag"] = resp.headers.get("etag", "")
            base["last_modified"] = resp.headers.get("last-modified", "")
            resp.raise_for_status()
            html_path.write_bytes(resp.content)
            txt = clean_page_text(resp.content, getattr(resp, "apparent_encoding", None))
            txt_path.write_text(txt, encoding="utf-8")
            base.update(
                status="downloaded",
                bytes=str(html_path.stat().st_size),
                sha256=sha256_file(html_path),
                downloaded_at=utcnow(),
                # Make both page representations discoverable in one field.
                local_path=f"{html_path.relative_to(self.root).as_posix()};{txt_path.relative_to(self.root).as_posix()}",
            )
        except Exception as exc:
            base.update(status="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            if self.delay:
                time.sleep(self.delay)
        return base


def summarize(rows: list[dict[str, str]]) -> dict:
    def counts(field: str) -> dict[str, int]:
        d: dict[str, int] = {}
        for r in rows:
            val = r.get(field, "") or "(none)"
            d[val] = d.get(val, 0) + 1
        return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))

    total_bytes = 0
    for r in rows:
        try:
            total_bytes += int(r.get("bytes") or 0)
        except ValueError:
            pass
    return {
        "generated_at": utcnow(),
        "items": len(rows),
        "bytes": total_bytes,
        "by_status": counts("status"),
        "by_item_kind": counts("item_kind"),
        "by_resource_type": counts("resource_type"),
        "by_language": counts("language"),
        "by_collection": counts("collection"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Download and organize a MooreBurkina inventory")
    ap.add_argument("--inventory", default="mooreburkina_inventory", help="Directory containing resources.csv/pages.csv")
    ap.add_argument("--out", default="MooreBurkinaCorpus", help="Corpus output directory")
    ap.add_argument("--language", action="append", default=[], help="Language filter; repeat as needed, e.g. --language moore --language dioula")
    ap.add_argument("--type", action="append", default=[], dest="types", help="Resource type filter; repeat as needed, e.g. --type audio --type pdf")
    ap.add_argument("--skip-type", action="append", default=None, dest="skip_types",
                    help=f"Resource type not to download; repeatable (default: {', '.join(sorted(DEFAULT_SKIP_TYPES))})")
    ap.add_argument("--all-types", action="store_true", help="Download every resource type (disables the default skips)")
    ap.add_argument("--limit", type=int, default=0, help="Max assets to process after filtering; 0 = unlimited")
    ap.add_argument("--page-limit", type=int, default=0, help="Max HTML pages to process; 0 = unlimited")
    ap.add_argument("--skip-pages", action="store_true", help="Do not download/save HTML pages and extracted text")
    ap.add_argument("--include-external", action="store_true", help="Also attempt direct HTTP assets hosted outside mooreburkina.com")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--delay", type=float, default=0.25, help="Delay between requests in seconds")
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--max-file-mb", type=float, default=0.0, help="Skip individual files larger than this; 0 = unlimited")
    ap.add_argument("--dry-run", action="store_true", help="Plan paths and manifests without downloading")
    ap.add_argument("--user-agent", default="AI-KING-LanguageCorpus/1.0 (+https://mooreburkina.com/)")
    args = ap.parse_args()

    inventory = Path(args.inventory)
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata").mkdir(parents=True, exist_ok=True)

    resources_path = inventory / "resources.csv"
    pages_path = inventory / "pages.csv"
    if not resources_path.exists():
        print(f"ERROR: {resources_path} not found. Run mooreburkina_crawler.py first.", file=sys.stderr)
        raise SystemExit(2)

    resources = read_csv(resources_path)
    pages = read_csv(pages_path)
    languages = {x.strip().lower() for x in args.language if x.strip()}
    types = {x.strip().lower() for x in args.types if x.strip()}
    skip_types = set() if args.all_types else (
        DEFAULT_SKIP_TYPES if args.skip_types is None else {x.strip().lower() for x in args.skip_types if x.strip()})
    skip_types -= types  # an explicit --type always wins

    selected_assets: list[dict[str, str]] = []
    references: list[dict[str, str]] = []
    for row in resources:
        if not matches_filters(row, languages, types):
            continue
        is_external = not same_site(row.get("resource_url", ""))
        rtype = (row.get("resource_type") or "").lower()
        if is_external and not args.include_external:
            ref = dict(row)
            ref["download_status"] = "external_reference"
            references.append(ref)
            continue
        if rtype in NON_DIRECT_TYPES:
            ref = dict(row)
            ref["download_status"] = "reference_only"
            references.append(ref)
            continue
        if rtype in skip_types:
            ref = dict(row)
            ref["download_status"] = "skipped_type"
            references.append(ref)
            continue
        selected_assets.append(row)

    if args.limit > 0:
        selected_assets = selected_assets[: args.limit]

    # Pages can also be language-filtered. Resource type filters intentionally do not affect pages.
    selected_pages = []
    if not args.skip_pages:
        for row in pages:
            if languages:
                row_langs = {x.lower() for x in split_field(row.get("language"))}
                if not (row_langs & languages):
                    continue
            selected_pages.append(row)
        if args.page_limit > 0:
            selected_pages = selected_pages[: args.page_limit]

    max_bytes = None if args.max_file_mb <= 0 else int(args.max_file_mb * 1024 * 1024)
    session = make_session(args.user_agent, args.retries)
    dl = Downloader(session, root, args.timeout, args.delay, max_bytes, args.dry_run)

    print(f"Assets selected: {len(selected_assets)}")
    print(f"Pages selected:  {len(selected_pages)}")
    print(f"References only: {len(references)}")
    print(f"Output:          {root.resolve()}")

    manifest_path = root / "metadata" / "download_manifest.csv"
    previous = load_existing_manifest(manifest_path)
    results: list[dict[str, str]] = []

    # Process assets.
    for i, row in enumerate(selected_assets, start=1):
        key = f"asset::{row.get('resource_id','')}"
        prev = previous.get(key)
        if prev and prev.get("status") in {"downloaded", "already_present"}:
            pfield = (prev.get("local_path") or "").split(";", 1)[0]
            if pfield and (root / pfield).exists():
                results.append(prev)
                print(f"[{i}/{len(selected_assets)}] SKIP {row.get('resource_url','')}")
                continue
        res = dl.download_asset(row)
        results.append(res)
        print(f"[{i}/{len(selected_assets)}] {res['status']}: {row.get('resource_url','')}")

    # Process pages.
    for i, row in enumerate(selected_pages, start=1):
        key = f"page::{row.get('page_id','')}"
        prev = previous.get(key)
        if prev and prev.get("status") in {"downloaded", "already_present"}:
            first = (prev.get("local_path") or "").split(";", 1)[0]
            if first and (root / first).exists():
                results.append(prev)
                print(f"[page {i}/{len(selected_pages)}] SKIP {row.get('url','')}")
                continue
        res = dl.download_page(row)
        results.append(res)
        print(f"[page {i}/{len(selected_pages)}] {res['status']}: {row.get('url','')}")

    # Preserve unselected previous rows in manifest so incremental/filter runs never erase history.
    result_keys = {f"{r.get('item_kind','')}::{r.get('resource_id','')}" for r in results}
    for key, old in previous.items():
        if key not in result_keys:
            results.append(old)

    # Reference-only URLs from this inventory are kept separately for later specialized collection.
    refs_path = root / "metadata" / "external_and_reference_urls.csv"
    if references:
        ref_fields = sorted({k for r in references for k in r.keys()})
        with refs_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=ref_fields, extrasaction="ignore")
            w.writeheader(); w.writerows(references)
    elif not refs_path.exists():
        refs_path.write_text("", encoding="utf-8")

    results.sort(key=lambda r: (r.get("item_kind", ""), r.get("language", ""), r.get("resource_type", ""), r.get("source_url", "")))
    write_manifest_csv(manifest_path, results)
    write_manifest_jsonl(root / "metadata" / "download_manifest.jsonl", results)
    summary = summarize(results)
    (root / "metadata" / "download_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Copy the exact inventory snapshot used for reproducibility.
    snapshot = root / "metadata" / "inventory_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    for name in ("resources.csv", "pages.csv", "errors.csv", "summary.json"):
        src = inventory / name
        if src.exists():
            shutil.copy2(src, snapshot / name)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""Build a durable, audio-only John Piper Messages RSS archive from Desiring God."""
from __future__ import annotations

import argparse
import email.utils
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BASE = "https://www.desiringgod.org"
ARCHIVE = BASE + "/dates/{year}/messages?page={page}"
UA = "john-piper-messages-archive/1.0 (+https://github.com/; respectful archival RSS builder)"
DATE_RE = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b")
TYPES = ("Conference Message", "Message", "Sermon", "Seminar")
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "atom": "http://www.w3.org/2005/Atom"}
for prefix, uri in NS.items(): ET.register_namespace(prefix, uri)


def canonical(url: str) -> str:
    parts = urlsplit(urljoin(BASE, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def text(node) -> str:
    return " ".join(node.stripped_strings) if node else ""


def request(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=(10, 35), **kwargs)
    if response.status_code == 429:
        time.sleep(3)
        response = session.get(url, timeout=(10, 35), **kwargs)
    return response


def load_catalog(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["url"]: item for item in data.get("items", []) if item.get("url")}


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def archive_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return candidate URL plus surrounding card text; site cards have message links."""
    found, seen = [], set()
    for anchor in soup.select('a[href^="/messages/"]'):
        url = canonical(anchor.get("href", ""))
        if not url or url in seen:
            continue
        # A card is normally a short ancestor; retain text for type/date hints.
        card = anchor.parent
        found.append((url, text(card)))
        seen.add(url)
    return found


def resource_type(hint: str) -> str:
    for kind in TYPES:
        if re.search(r"\b" + re.escape(kind) + r"\b", hint, re.I):
            return kind
    return "Message"


def first_date(value: str) -> datetime | None:
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def is_john_piper(soup: BeautifulSoup) -> bool:
    """Read the speaker next to the visible ``Message by`` header.

    Arbitrary page text is unsafe: author bios and related resource cards can
    mention John Piper on a page whose main speaker is someone else.
    """
    for label in soup.find_all(string=re.compile(r"^\s*Message by\s*$", re.I)):
        for following in label.find_all_next(string=True, limit=12):
            candidate = " ".join(following.split())
            if candidate and candidate.lower() != "message by":
                return candidate == "John Piper"
    return False


def parse_resource(session: requests.Session, url: str, hint: str) -> tuple[dict | None, str | None]:
    response = request(session, url)
    if response.status_code != 200:
        return None, f"resource HTTP {response.status_code}"
    soup = BeautifulSoup(response.text, "html.parser")
    page = text(soup)
    title_node = soup.select_one("h1")
    title = text(title_node)
    if not title:
        return None, "missing title"
    if not is_john_piper(soup):
        return None, "not John Piper"
    published = first_date(page)
    if not published:
        return None, "missing original date"
    audio = None
    for anchor in soup.find_all("a", href=True):
        label, href = text(anchor).lower(), urljoin(url, anchor["href"])
        if ("audio" in label or href.lower().endswith((".mp3", ".m4a", ".aac"))) and \
           any(ext in href.lower() for ext in (".mp3", ".m4a", ".aac")):
            audio = href
            break
    if not audio:
        return None, "no downloadable audio URL"
    description = ""
    meta = soup.select_one('meta[name="description"]')
    if meta:
        description = meta.get("content", "").strip()
    scripture = ""
    scripture_match = re.search(r"Scripture:\s*([^\n]+?)(?=\s+(?:Topic|Series):|\n|$)", page)
    if scripture_match:
        scripture = scripture_match.group(1).strip()
    return {
        "title": title,
        "url": canonical(url),
        "date": published.date().isoformat(),
        "author": "John Piper",
        "type": resource_type(hint),
        "scripture": scripture,
        "description": description,
        "audio_url": audio,
    }, None


def verify_audio(session: requests.Session, url: str) -> tuple[bool, str]:
    try:
        response = request(session, url, headers={"Range": "bytes=0-1"}, stream=True, allow_redirects=True)
        ok = response.status_code in (200, 206) and "text/html" not in response.headers.get("Content-Type", "").lower()
        response.close()
        return ok, "" if ok else f"audio HTTP {response.status_code} ({response.headers.get('Content-Type', '')})"
    except requests.RequestException as exc:
        return False, f"audio request failed: {exc.__class__.__name__}"


def crawl(session: requests.Session, start_year: int, oldest_year: int, max_pages: int, catalog: dict[str, dict]):
    unresolved, discovered, resolved = [], 0, 0
    for year in range(start_year, oldest_year - 1, -1):
        repeated, pages_seen = set(), 0
        for page in range(1, max_pages + 1):
            response = request(session, ARCHIVE.format(year=year, page=page))
            if response.status_code == 404:
                break  # Expected end of a year's pagination.
            if response.status_code != 200:
                unresolved.append({"year": year, "page": page, "reason": f"archive HTTP {response.status_code}"})
                break
            soup = BeautifulSoup(response.text, "html.parser")
            links = archive_links(soup)
            fingerprint = tuple(url for url, _ in links)
            if fingerprint in repeated:
                logging.warning("Repeated archive page detected: %s/%s", year, page)
                break
            repeated.add(fingerprint)
            pages_seen += 1
            if not links:
                break  # An actual empty archive page is a normal terminal page.
            for url, hint in links:
                if not re.search(r"\bJohn Piper\b", hint, re.I) and url not in catalog:
                    continue
                discovered += 1
                item, reason = parse_resource(session, url, hint)
                if item is None:
                    if reason != "not John Piper":
                        unresolved.append({"url": url, "reason": reason})
                    continue
                good, why = verify_audio(session, item["audio_url"])
                if not good:
                    unresolved.append({"url": url, "audio_url": item["audio_url"], "reason": why})
                    continue
                catalog[item["url"]] = item
                resolved += 1
            time.sleep(0.15)
        if pages_seen == max_pages:
            unresolved.append({"year": year, "reason": "pagination safety limit reached"})
    return catalog, unresolved, discovered, resolved


def rss(catalog: dict[str, dict], out: Path, site_url: str) -> None:
    items = sorted(catalog.values(), key=lambda x: (x["date"], x["url"]))
    if len(items) < 10:
        raise RuntimeError(f"Refusing to publish an obviously tiny feed ({len(items)} verified items).")
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "John Piper Messages — Complete Archive"
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = "Complete personal archive of John Piper messages from Desiring God, preserving original historical dates."
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(datetime.now(UTC))
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text = "John Piper"
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit").text = "false"
    ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link", href=site_url, rel="self", type="application/rss+xml")
    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item["title"]
        ET.SubElement(node, "link").text = item["url"]
        ET.SubElement(node, "guid", isPermaLink="true").text = item["url"]
        ET.SubElement(node, "pubDate").text = email.utils.format_datetime(datetime.fromisoformat(item["date"]).replace(tzinfo=UTC))
        desc = item.get("description", "")
        extras = " · ".join(x for x in (item.get("type"), item.get("scripture")) if x)
        ET.SubElement(node, "description").text = (desc + ("\n\n" + extras if extras else "")).strip()
        ET.SubElement(node, "author").text = "John Piper"
        ET.SubElement(node, "enclosure", url=item["audio_url"], type="audio/mpeg")
    ET.indent(root, space="  ")
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    ET.parse(out)  # XML validation is a required success condition.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=datetime.now().year)
    parser.add_argument("--oldest-year", type=int, default=1900, help="Safety floor; does not assume an archive start year.")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    catalog_path, unresolved_path = Path("catalog.json"), Path("unresolved_messages.json")
    catalog = load_catalog(catalog_path)
    session = requests.Session(); session.headers["User-Agent"] = UA
    catalog, unresolved, discovered, resolved = crawl(session, args.start_year, args.oldest_year, args.max_pages, catalog)
    items = sorted(catalog.values(), key=lambda x: (x["date"], x["url"]))
    save_json(catalog_path, {"schema_version": 1, "items": items})
    save_json(unresolved_path, unresolved)
    if not args.dry_run:
        feed_url = os.getenv("FEED_URL", "https://example.invalid/john-piper-messages-complete.rss")
        rss(catalog, Path("public/john-piper-messages-complete.rss"), feed_url)
        Path("public/index.html").write_text("<!doctype html><title>John Piper Messages</title><h1>John Piper Messages — Complete Archive</h1><p><a href=\"john-piper-messages-complete.rss\">RSS feed</a></p>\n", encoding="utf-8")
    dates = [x["date"] for x in items]
    counts = Counter({"discovered_this_run": discovered, "verified_this_run": resolved, "unresolved": len(unresolved), "rss_items": len(items)})
    logging.info("%s earliest=%s latest=%s", dict(counts), min(dates, default="n/a"), max(dates, default="n/a"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.error("Build failed safely: %s", exc)
        raise SystemExit(1)

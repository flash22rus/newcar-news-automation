
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""NEWCAR — сбор и фильтрация кандидатов автомобильных новостей.

Собирает RSS-кандидатов, сохраняет ссылки Google News и метаданные
издателя, если они присутствуют в RSS. Ничего не публикует.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path


WINDOW_HOURS = 24
MAX_CANDIDATES = 20
TIMEOUT_SECONDS = 15
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "news_candidates.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; NEWCAR-NewsBot/1.0; "
    "+https://github.com/flash22rus/newcar-news-automation)"
)

FEEDS = [
    {
        "name": "Google News - автомобильный импорт",
        "query": '"импорт автомобилей" OR "ввоз автомобилей"',
    },
    {
        "name": "Google News - японские автомобили",
        "query": '"автомобили из Японии" OR "японские автомобили"',
    },
    {
        "name": "Google News - утильсбор",
        "query": '"утильсбор" автомобили',
    },
    {
        "name": "Google News - импорт автомобилей из Китая",
        "query": '"автомобили из Китая" импорт OR поставки',
    },
]


def utc_now():
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def clean_text(value):
    """Remove HTML markup and normalize whitespace."""
    if not value:
        return ""

    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value):
    """Parse RSS/Atom dates and normalize them to UTC."""
    if not value:
        return None

    value = value.strip()

    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def build_feed_url(query):
    """Build a Google News RSS search URL."""
    params = {
        "q": query,
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
    }

    return (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode(params, encoding="utf-8")
    )


def fetch_feed(url):
    """Download one RSS feed."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml, text/xml, */*"
            ),
        },
        method="GET",
    )

    with urllib.request.urlopen(
        request, timeout=TIMEOUT_SECONDS
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP status {response.status}")
        body = response.read()

    if not body:
        raise RuntimeError("Empty response")

    return body


def local_name(tag):
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element, names):
    """Return text from the first matching descendant."""
    for child in element.iter():
        if child is element:
            continue

        if local_name(child.tag) in names and child.text:
            return child.text.strip()

    return ""


def atom_link(element):
    """Extract an Atom alternate link."""
    for child in element:
        if local_name(child.tag) != "link":
            continue

        href = child.attrib.get("href", "").strip()
        relation = child.attrib.get("rel", "alternate")

        if href and relation == "alternate":
            return href

    return ""


def source_info(element):
    """Extract the publisher name and URL from an RSS <source> element.

    source_url is publisher metadata. It may point to the publisher's
    homepage rather than to the specific article.
    """
    for child in element:
        if local_name(child.tag) != "source":
            continue

        name = clean_text(child.text or "")
        url = child.attrib.get("url", "").strip()

        if url.startswith("//"):
            url = "https:" + url

        parsed = urllib.parse.urlparse(url)

        if parsed.scheme != "https" or not parsed.netloc:
            url = ""

        return {
            "source_name": name,
            "source_url": url,
        }

    return {
        "source_name": "",
        "source_url": "",
    }


def extract_entries(xml_bytes):
    """Parse RSS or Atom entries into normalized candidate records."""
    root = ET.fromstring(xml_bytes)
    entries = []

    for element in root.iter():
        if local_name(element.tag) not in ("item", "entry"):
            continue

        title = clean_text(child_text(element, {"title"}))
        link = child_text(element, {"link"}).strip()

        published = child_text(
            element,
            {"pubDate", "published", "updated", "date"},
        )

        description = clean_text(
            child_text(element, {"description", "summary", "content"})
        )

        if not link:
            link = atom_link(element)

        if not title or not link:
            continue

        if link.startswith("//"):
            link = "https:" + link

        parsed_link = urllib.parse.urlparse(link)

        if parsed_link.scheme != "https" or not parsed_link.netloc:
            continue

        publisher = source_info(element)

        entries.append({
            "title": title,
            "url": link,
            "source_name": publisher["source_name"],
            "source_url": publisher["source_url"],
            "published_raw": published,
            "description": description[:600],
        })

    return entries


def collect_feed(feed, now):
    """Collect fresh candidates from a single feed."""
    started = time.monotonic()

    result = {
        "feed": feed["name"],
        "status": "error",
        "http_status": None,
        "items_seen": 0,
        "fresh_candidates": 0,
        "duration_seconds": 0,
        "error_type": None,
        "error": None,
    }

    try:
        xml_bytes = fetch_feed(build_feed_url(feed["query"]))
        entries = extract_entries(xml_bytes)

        result["status"] = "ok"
        result["items_seen"] = len(entries)

        cutoff = now - timedelta(hours=WINDOW_HOURS)
        candidates = []

        for entry in entries:
            published = parse_date(entry["published_raw"])

            # Unknown publication dates are not considered fresh.
            if published is None:
                continue

            if published > now + timedelta(minutes=10):
                continue

            if published < cutoff:
                continue

            entry["published_utc"] = published.isoformat()
            entry["feed"] = feed["name"]
            candidates.append(entry)

        result["fresh_candidates"] = len(candidates)
        return result, candidates

    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:500]

    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:500]

    finally:
        result["duration_seconds"] = round(
            time.monotonic() - started, 2
        )

    return result, []


def deduplicate(candidates):
    """Remove duplicate article URLs and normalized titles."""
    seen_urls = set()
    seen_titles = set()
    unique = []

    for item in sorted(
        candidates,
        key=lambda candidate: candidate.get("published_utc", ""),
        reverse=True,
    ):
        url = item["url"].split("#", 1)[0].rstrip("/")
        title_key = re.sub(
            r"[^a-zа-яё0-9]+",
            "",
            item["title"].lower(),
        )

        if url in seen_urls or title_key in seen_titles:
            continue

        seen_urls.add(url)
        seen_titles.add(title_key)

        item["url"] = url
        unique.append(item)

    return unique


def main():
    """Run collection and write the JSON report."""
    now = utc_now()
    all_candidates = []
    feed_statuses = []

    print("NEWCAR RSS collector")
    print("Current UTC:", now.isoformat())
    print("Freshness window:", WINDOW_HOURS, "hours")
    print()

    for feed in FEEDS:
        status, candidates = collect_feed(feed, now)

        feed_statuses.append(status)
        all_candidates.extend(candidates)

        if status["status"] == "ok":
            print(
                "OK:", feed["name"],
                "| items:", status["items_seen"],
                "| fresh:", status["fresh_candidates"],
            )
        else:
            print(
                "ERROR:", feed["name"],
                "| type:", status["error_type"],
                "| details:", status["error"],
            )

    unique_candidates = deduplicate(all_candidates)[:MAX_CANDIDATES]

    successful_feeds = sum(
        1 for item in feed_statuses if item["status"] == "ok"
    )

    candidates_with_source = sum(
        1 for item in unique_candidates
        if item.get("source_url")
    )

    report = {
        "stage": "candidate_collection_only",
        "generated_at": now.isoformat(),
        "window_hours": WINDOW_HOURS,
        "feeds_total": len(FEEDS),
        "feeds_successful": successful_feeds,
        "feeds_failed": len(FEEDS) - successful_feeds,
        "candidate_count": len(unique_candidates),
        "candidates_with_publisher_url": candidates_with_source,
        "publication_ready": False,
        "warning": (
            "RSS results are candidates only. Publisher URLs may point "
            "to homepages rather than individual articles. Verify article "
            "content, publication date and independent sources before use."
        ),
        "feed_statuses": feed_statuses,
        "candidates": unique_candidates,
    }

    try:
        OUTPUT_FILE.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"ERROR: Could not save JSON report: {exc}",
            file=sys.stderr,
        )
        return 3

    print()
    print("JSON file saved:", OUTPUT_FILE.name)
    print("Candidates:", len(unique_candidates))
    print("Candidates with publisher URL:", candidates_with_source)
    print("Publication ready: false")

    if successful_feeds == 0:
        print(
            "ERROR: All RSS feeds failed. Check feed_statuses.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

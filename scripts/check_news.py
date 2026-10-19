
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NEWCAR RSS news candidate collector.

This script collects candidate news only.
It does not verify independent sources, call the Worker, or publish.
"""

import datetime as dt
import email.utils
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape


WINDOW_HOURS = 24
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 3_000_000
MAX_ITEMS_PER_FEED = 30
MAX_OUTPUT_CANDIDATES = 30
USER_AGENT = "Mozilla/5.0 (compatible; NEWCAR-NewsResearch/1.0)"

# Queries use Unicode escape sequences, so this source can be ASCII-only.
SEARCHES = [
    (
        "Google News - auto import Russia",
        "import",
        "\u0438\u043c\u043f\u043e\u0440\u0442 \u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u0435\u0439 \u0420\u043e\u0441\u0441\u0438\u044f",
    ),
    (
        "Google News - Japan cars",
        "japan",
        "\u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u0438 \u042f\u043f\u043e\u043d\u0438\u044f \u0420\u043e\u0441\u0441\u0438\u044f",
    ),
    (
        "Google News - recycling fee",
        "recycling_fee",
        "\u0443\u0442\u0438\u043b\u044c\u0441\u0431\u043e\u0440 \u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u0438",
    ),
    (
        "Google News - China cars",
        "china",
        "\u0438\u043c\u043f\u043e\u0440\u0442 \u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u0435\u0439 \u041a\u0438\u0442\u0430\u0439 \u0420\u043e\u0441\u0441\u0438\u044f",
    ),
    (
        "Google News - Korea cars",
        "korea",
        "\u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u0438 \u041a\u043e\u0440\u0435\u044f \u0438\u043c\u043f\u043e\u0440\u0442 \u0420\u043e\u0441\u0441\u0438\u044f",
    ),
    (
        "Google News - customs and logistics",
        "customs",
        "\u0442\u0430\u043c\u043e\u0436\u043d\u044f \u043b\u043e\u0433\u0438\u0441\u0442\u0438\u043a\u0430 \u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u0438 \u0420\u043e\u0441\u0441\u0438\u044f",
    ),
]


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_date(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def build_feed_url(query):
    # urlencode percent-encodes Cyrillic into an ASCII query string.
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "ru",
            "gl": "RU",
            "ceid": "RU:ru",
        },
        encoding="utf-8",
        errors="strict",
    )
    return "https://news.google.com/rss/search?" + params


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def validate_https_url(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() != "https":
            return None
        if not parts.hostname or parts.username or parts.password:
            return None
        return urllib.parse.urlunsplit(
            (parts.scheme.lower(), parts.netloc, parts.path, parts.query, "")
        )
    except (ValueError, UnicodeError):
        return None


def fetch_feed(name, key, query):
    url = build_feed_url(query)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )

    started = time.monotonic()
    status = {
        "name": name,
        "key": key,
        "ok": False,
        "http_status": None,
        "items_seen": 0,
        "fresh_candidates": 0,
        "error_type": None,
        "error": None,
        "duration_seconds": None,
    }

    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            status["http_status"] = getattr(response, "status", None)
            raw = response.read(MAX_RESPONSE_BYTES + 1)

        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("RSS response exceeded size limit")

        root = ET.fromstring(raw)
        status["ok"] = True

    except urllib.error.HTTPError as exc:
        status["http_status"] = exc.code
        status["error_type"] = type(exc).__name__
        status["error"] = ascii(str(exc))[:500]
        status["duration_seconds"] = round(time.monotonic() - started, 3)
        return [], status

    except Exception as exc:
        status["error_type"] = type(exc).__name__
        status["error"] = ascii(str(exc))[:500]
        status["duration_seconds"] = round(time.monotonic() - started, 3)
        return [], status

    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    now = utc_now()
    cutoff = now - dt.timedelta(hours=WINDOW_HOURS)
    candidates = []

    for entry in entries[:MAX_ITEMS_PER_FEED]:
        title = clean_text(
            entry.findtext("title")
            or entry.findtext("{http://www.w3.org/2005/Atom}title")
        )

        link = entry.findtext("link")
        if not link:
            atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
            if atom_link is not None:
                link = atom_link.attrib.get("href")

        link = validate_https_url(link)
        published_raw = (
            entry.findtext("pubDate")
            or entry.findtext("published")
            or entry.findtext("updated")
            or entry.findtext("{http://www.w3.org/2005/Atom}published")
            or entry.findtext("{http://www.w3.org/2005/Atom}updated")
        )
        published = parse_date(published_raw)

        if not title or not link or not published:
            continue
        if published < cutoff or published > now + dt.timedelta(minutes=5):
            continue

        candidates.append(
            {
                "feed_key": key,
                "feed_name": name,
                "title": title,
                "url": link,
                "published_at": published.isoformat(),
            }
        )

    status["items_seen"] = len(entries)
    status["fresh_candidates"] = len(candidates)
    status["duration_seconds"] = round(time.monotonic() - started, 3)
    return candidates, status


def normalize_title(title):
    title = title.casefold()
    title = re.sub(r"[^\w\s]", " ", title, flags=re.UNICODE)
    return re.sub(r"\s+", " ", title).strip()


def main():
    started_at = utc_now()
    all_candidates = []
    feed_statuses = []

    for name, key, query in SEARCHES:
        candidates, status = fetch_feed(name, key, query)
        all_candidates.extend(candidates)
        feed_statuses.append(status)

    all_candidates.sort(
        key=lambda item: item["published_at"],
        reverse=True,
    )

    unique = []
    seen_urls = set()
    seen_titles = set()

    for item in all_candidates:
        url_key = item["url"].rstrip("/").casefold()
        title_key = normalize_title(item["title"])

        if url_key in seen_urls or title_key in seen_titles:
            continue

        seen_urls.add(url_key)
        seen_titles.add(title_key)
        unique.append(item)

    successful_feeds = sum(1 for feed in feed_statuses if feed["ok"])

    result = {
        "stage": "candidate_collection_only",
        "generated_at": started_at.isoformat(),
        "window_hours": WINDOW_HOURS,
        "feeds_configured": len(SEARCHES),
        "feeds_successful": successful_feeds,
        "feeds_failed": len(SEARCHES) - successful_feeds,
        "candidate_count": len(unique),
        "publication_ready": False,
        "worker_called": False,
        "published": False,
        "validation_note": (
            "RSS items are candidates only. Article content, original "
            "publication date, factual claims and source independence "
            "must be verified separately before drafting."
        ),
        "feed_statuses": feed_statuses,
        "candidates": unique[:MAX_OUTPUT_CANDIDATES],
    }

    print(json.dumps(result, ensure_ascii=True, indent=2))

    if successful_feeds == 0:
        print(
            "ERROR: all RSS feeds failed. Inspect feed_statuses above.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

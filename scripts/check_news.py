
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEWCAR — сбор кандидатов автомобильных новостей.
Только поиск и фильтрация. Ничего не публикует и не отправляет.
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


WINDOW_HOURS = 24
MAX_CANDIDATES = 20
TIMEOUT_SECONDS = 15

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
    return datetime.now(timezone.utc)


def clean_text(value):
    """Удаляет HTML-разметку и лишние пробелы."""
    if not value:
        return ""

    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def parse_date(value):
    """Преобразует дату RSS/Atom в UTC."""
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
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except (TypeError, ValueError, OverflowError):
        return None


def build_feed_url(query):
    """Кодирует кириллицу и специальные символы в URL."""
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
    """Загружает RSS-ленту и возвращает XML."""
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
        request,
        timeout=TIMEOUT_SECONDS,
    ) as response:
        status = response.status
        body = response.read()

    if status != 200:
        raise RuntimeError(f"HTTP status {status}")

    if not body:
        raise RuntimeError("Empty response")

    return body


def local_name(tag):
    """Извлекает имя XML-тега без пространства имён."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]

    return tag


def child_text(element, names):
    """Ищет текст дочернего элемента по имени."""
    for child in element.iter():
        if child is element:
            continue

        if local_name(child.tag) in names and child.text:
            return child.text.strip()

    return ""


def atom_link(element):
    """Извлекает ссылку RSS или Atom."""
    for child in element:
        if local_name(child.tag) != "link":
            continue

        href = child.attrib.get("href", "").strip()

        if href:
            rel = child.attrib.get("rel", "alternate")

            if rel == "alternate":
                return href

    return ""


def extract_entries(xml_bytes):
    """Читает RSS items и Atom entries."""
    root = ET.fromstring(xml_bytes)
    entries = []

    for element in root.iter():
        tag = local_name(element.tag)

        if tag not in ("item", "entry"):
            continue

        title = child_text(element, {"title"})
        link = child_text(element, {"link"})
        published = child_text(
            element,
            {"pubDate", "published", "updated", "date"},
        )
        description = child_text(
            element,
            {"description", "summary", "content"},
        )

        if not link:
            link = atom_link(element)

        title = clean_text(title)
        description = clean_text(description)
        link = link.strip()

        if not title or not link:
            continue

        if link.startswith("//"):
            link = "https:" + link

        parsed = urllib.parse.urlparse(link)

        if parsed.scheme != "https" or not parsed.netloc:
            continue

        entries.append(
            {
                "title": title,
                "url": link,
                "published_raw": published,
                "description": description[:600],
            }
        )

    return entries


def collect_feed(feed, now):
    """Собирает свежие публикации одной ленты."""
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
        url = build_feed_url(feed["query"])
        xml_bytes = fetch_feed(url)
        entries = extract_entries(xml_bytes)

        result["status"] = "ok"
        result["items_seen"] = len(entries)

        cutoff = now - timedelta(hours=WINDOW_HOURS)
        candidates = []

        for entry in entries:
            published = parse_date(entry["published_raw"])

            # Не считаем публикацию свежей, если дата неизвестна.
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

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ET.ParseError,
        ValueError,
        RuntimeError,
        UnicodeError,
    ) as exc:
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
    """Удаляет дубли по URL и нормализованному заголовку."""
    seen_urls = set()
    seen_titles = set()
    unique = []

    for item in sorted(
        candidates,
        key=lambda x: x.get("published_utc", ""),
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
                "OK:",
                feed["name"],
                "| items:",
                status["items_seen"],
                "| fresh:",
                status["fresh_candidates"],
            )
        else:
            print(
                "ERROR:",
                feed["name"],
                "| type:",
                status["error_type"],
                "| details:",
                status["error"],
            )

    unique_candidates = deduplicate(all_candidates)
    unique_candidates = unique_candidates[:MAX_CANDIDATES]

    successful_feeds = sum(
        1 for item in feed_statuses
        if item["status"] == "ok"
    )

    report = {
        "stage": "candidate_collection_only",
        "generated_at": now.isoformat(),
        "window_hours": WINDOW_HOURS,
        "feeds_total": len(FEEDS),
        "feeds_successful": successful_feeds,
        "feeds_failed": len(FEEDS) - successful_feeds,
        "candidate_count": len(unique_candidates),
        "publication_ready": False,
        "warning": (
            "RSS results are candidates only. Verify article content, "
            "publication date and two independent sources before drafting."
        ),
        "feed_statuses": feed_statuses,
        "candidates": unique_candidates,
    }

    print()
    print("JSON REPORT:")
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
    )

    # Ошибка выполнения только если не удалось получить ни одной ленты.
    if successful_feeds == 0:
        print(
            "\nERROR: All RSS feeds failed. "
            "Check feed_statuses in the report.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

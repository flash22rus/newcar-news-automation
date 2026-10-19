from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
#!/usr/bin/env python3
"""NEWCAR: безопасный сбор кандидатов новостей из RSS.
На этом этапе ничего не публикуется и Worker не вызывается.
"""
import datetime as dt
import email.utils
import json
import urllib.request from urllib.parse import quote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

FEEDS = [
    (
        "Google News — импорт автомобилей",
        "https://news.google.com/rss/search?q=импорт+автомобилей+Россия&hl=ru&gl=RU&ceid=RU:ru",
    ),
    (
        "Google News — автомобили Япония",
        "https://news.google.com/rss/search?q=автомобили+Япония+Россия&hl=ru&gl=RU&ceid=RU:ru",
    ),
    (
        "Google News — утильсбор",
        "https://news.google.com/rss/search?q=утильсбор+автомобили&hl=ru&gl=RU&ceid=RU:ru",
    ),
    (
        "Google News — импорт автомобилей Китай",
        "https://news.google.com/rss/search?q=импорт+автомобилей+Китай+Россия&hl=ru&gl=RU&ceid=RU:ru",
    ),
]

WINDOW = dt.timedelta(hours=24)


def parse_date(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None




def fetch_feed(name, url):
    parts = urlsplit(url)
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True))
    encoded_url = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        query,
        parts.fragment,
    ))

    request = urllib.request.Request(
        encoded_url,
        headers={"User-Agent": "Mozilla/5.0 NEWCAR-NewsResearch/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(2_000_000)
        root = ET.fromstring(raw)
    except Exception as exc:
        print(
            f"WARNING: feed unavailable: {name}: "
            f"{type(exc).__name__}: {ascii(str(exc))}"
        )
        return []

    results = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = parse_date(item.findtext("pubDate"))
        if title and link.startswith("https://") and published:
            results.append({
                "feed": name,
                "title": title,
                "url": link,
                "published_at": published.isoformat(),
            })
    return results




def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - WINDOW
    candidates = []

    for name, url in FEEDS:
        candidates.extend(fetch_feed(name, url))

    fresh = [
        item for item in candidates
        if cutoff <= dt.datetime.fromisoformat(item["published_at"]) <= now
    ]

    unique = []
    seen = set()
    for item in sorted(
        fresh, key=lambda candidate: candidate["published_at"], reverse=True
    ):
        key = item["title"].casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    result = {
        "stage": "candidate_collection_only",
        "generated_at": now.isoformat(),
        "window_hours": 24,
        "candidate_count": len(unique),
        "publication_ready": False,
        "warning": (
            "RSS results are candidates only. Verify the article content, "
            "publication date and two independent sources before drafting."
        ),
        "candidates": unique[:20],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

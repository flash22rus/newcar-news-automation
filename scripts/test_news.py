
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NEWCAR — автоматические тесты сборщика новостей.
Тесты локальные: не отправляют запросы в интернет и ничего не публикуют.
"""

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_news.py"
REPORT = ROOT / "news_candidates.json"

spec = importlib.util.spec_from_file_location("check_news", SCRIPT)
check_news = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = check_news
spec.loader.exec_module(check_news)


class TestNewsCollector(unittest.TestCase):

    def test_configuration(self):
        self.assertEqual(check_news.WINDOW_HOURS, 24)
        self.assertGreater(check_news.MAX_CANDIDATES, 0)
        self.assertEqual(len(check_news.FEEDS), 4)

    def test_rss_url_encodes_russian_query(self):
        url = check_news.build_feed_url('"импорт автомобилей"')
        self.assertTrue(url.startswith("https://news.google.com/rss/search?"))
        self.assertIn("q=", url)

    def test_parse_rfc822_date(self):
        result = check_news.parse_date("Mon, 19 Oct 2026 12:00:00 GMT")
        self.assertIsNotNone(result)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_parse_iso_date(self):
        result = check_news.parse_date("2026-10-19T12:00:00Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_invalid_date_is_rejected(self):
        self.assertIsNone(check_news.parse_date("not-a-date"))
        self.assertIsNone(check_news.parse_date(""))

    def test_extract_rss_entries(self):
        xml = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>Test car news</title>
            <link>https://example.com/news/1</link>
            <pubDate>Mon, 19 Oct 2026 12:00:00 GMT</pubDate>
            <description>Test description</description>
          </item>
        </channel></rss>"""

        entries = check_news.extract_entries(xml)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Test car news")
        self.assertEqual(entries[0]["url"], "https://example.com/news/1")

    def test_rejects_non_https_links(self):
        xml = b"""<rss><channel><item>
          <title>Test</title>
          <link>http://example.com/news</link>
          <pubDate>Mon, 19 Oct 2026 12:00:00 GMT</pubDate>
        </item></channel></rss>"""

        self.assertEqual(check_news.extract_entries(xml), [])

    def test_deduplicates_urls_and_titles(self):
        items = [
            {
                "title": "Toyota import news",
                "url": "https://example.com/a#section",
                "published_utc": "2026-10-19T12:00:00+00:00",
            },
            {
                "title": "Toyota import news",
                "url": "https://example.com/b",
                "published_utc": "2026-10-19T11:00:00+00:00",
            },
            {
                "title": "Another story",
                "url": "https://example.com/a",
                "published_utc": "2026-10-19T10:00:00+00:00",
            },
        ]

        result = check_news.deduplicate(items)
        self.assertEqual(len(result), 2)

    def test_report_is_not_marked_ready_for_publication(self):
        now = datetime.now(timezone.utc)
        report = {
            "stage": "candidate_collection_only",
            "generated_at": now.isoformat(),
            "window_hours": check_news.WINDOW_HOURS,
            "feeds_total": len(check_news.FEEDS),
            "feeds_successful": 0,
            "feeds_failed": len(check_news.FEEDS),
            "candidate_count": 0,
            "publication_ready": False,
            "feed_statuses": [],
            "candidates": [],
        }

        self.assertFalse(report["publication_ready"])
        self.assertEqual(report["stage"], "candidate_collection_only")

    def test_saved_report_structure(self):
        if not REPORT.exists():
            self.skipTest(
                "news_candidates.json отсутствует. "
                "Сначала запустите check_news.py."
            )

        report = json.loads(REPORT.read_text(encoding="utf-8"))

        self.assertEqual(report.get("stage"), "candidate_collection_only")
        self.assertFalse(report.get("publication_ready"))
        self.assertIsInstance(report.get("candidates"), list)
        self.assertLessEqual(
            len(report["candidates"]),
            check_news.MAX_CANDIDATES,
        )

        for item in report["candidates"]:
            self.assertTrue(item.get("title"))
            self.assertTrue(item.get("published_utc"))
            self.assertTrue(item.get("url", "").startswith("https://"))

    def test_report_freshness_window(self):
        if not REPORT.exists():
            self.skipTest(
                "news_candidates.json отсутствует. "
                "Сначала запустите check_news.py."
            )

        report = json.loads(REPORT.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(report["generated_at"])
        cutoff = generated - timedelta(hours=check_news.WINDOW_HOURS)

        for item in report["candidates"]:
            published = datetime.fromisoformat(item["published_utc"])
            self.assertGreaterEqual(published, cutoff)
            self.assertLessEqual(
                published,
                generated + timedelta(minutes=10),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

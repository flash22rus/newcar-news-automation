
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Автоматические тесты анализатора новостей NEWCAR."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_news.py"

spec = importlib.util.spec_from_file_location("verify_news", SCRIPT)
verify_news = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify_news
spec.loader.exec_module(verify_news)


class TestNewsAnalyzer(unittest.TestCase):

    def test_relevance_score_has_expected_structure(self):
        result = verify_news.score_relevance({
            "title": "Импорт автомобилей из Японии в Россию",
            "description": "Изменились цены на доставку",
        })

        self.assertIn("relevance_score", result)
        self.assertIn("matched_categories", result)
        self.assertIn("relevance_label", result)
        self.assertGreaterEqual(result["relevance_score"], 0)
        self.assertLessEqual(result["relevance_score"], 100)

    def test_missing_title_gets_zero_relevance(self):
        result = verify_news.score_relevance({
            "title": "",
            "description": "Импорт автомобилей из Японии",
        })

        self.assertEqual(result["relevance_score"], 0)
        self.assertEqual(result["relevance_label"], "low")

    def test_google_news_redirect_is_detected(self):
        self.assertTrue(
            verify_news.is_google_news_redirect(
                "https://news.google.com/rss/articles/example"
            )
        )

    def test_regular_publisher_url_is_not_redirect(self):
        self.assertFalse(
            verify_news.is_google_news_redirect(
                "https://example.com/news/123"
            )
        )

    def test_publication_date_uses_published_utc(self):
        candidate = {
            "title": "Импорт автомобилей из Японии в Россию",
            "url": "https://example.com/news/1",
            "published_utc": "2026-10-19T12:00:00+00:00",
            "description": "Новости автомобильного импорта",
            "feed": "test",
        }

        groups = verify_news.build_groups([candidate])
        self.assertEqual(len(groups), 1)

        member = groups[0]["members"][0]
        self.assertEqual(
            member["published_utc"],
            "2026-10-19T12:00:00+00:00",
        )

    def test_legacy_publication_date_is_supported(self):
        candidate = {
            "title": "Импорт автомобилей из Японии в Россию",
            "url": "https://example.com/news/2",
            "published": "2026-10-19T11:00:00+00:00",
        }

        groups = verify_news.build_groups([candidate])
        member = groups[0]["members"][0]

        self.assertEqual(
            member["published_utc"],
            "2026-10-19T11:00:00+00:00",
        )

    def test_empty_input_returns_no_groups(self):
        self.assertEqual(verify_news.build_groups([]), [])

    def test_group_is_not_publication_ready(self):
        groups = verify_news.build_groups([{
            "title": "Импорт автомобилей из Японии в Россию",
            "url": "https://example.com/news/3",
        }])

        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["publication_ready"])
        self.assertEqual(
            groups[0]["verification_status"],
            "needs_manual_verification",
        )

    def test_group_contains_manual_checks(self):
        groups = verify_news.build_groups([{
            "title": "Новости автомобильного импорта",
            "url": "https://example.com/news/4",
        }])

        member = groups[0]["members"][0]
        self.assertTrue(member["manual_checks"])
        self.assertEqual(
            member["verification_status"],
            "needs_manual_verification",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Automated tests for the NEWCAR news analyzer."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

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
        self.assertTrue(verify_news.is_google_news_redirect(
            "https://news.google.com/rss/articles/example"
        ))

    def test_regular_publisher_url_is_not_redirect(self):
        self.assertFalse(verify_news.is_google_news_redirect(
            "https://example.com/news/123"
        ))

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
        self.assertEqual(
            groups[0]["members"][0]["published_utc"],
            "2026-10-19T12:00:00+00:00",
        )

    def test_legacy_publication_date_is_supported(self):
        candidate = {
            "title": "Импорт автомобилей из Японии в Россию",
            "url": "https://example.com/news/2",
            "published": "2026-10-19T11:00:00+00:00",
        }
        groups = verify_news.build_groups([candidate])
        self.assertEqual(
            groups[0]["members"][0]["published_utc"],
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

    def test_publisher_metadata_is_preserved(self):
        groups = verify_news.build_groups([{
            "title": "Импорт автомобилей из Японии в Россию",
            "url": "https://news.google.com/rss/articles/example",
            "source_name": "Example News",
            "source_url": "https://example.com/",
        }])
        member = groups[0]["members"][0]
        self.assertEqual(member["source_name"], "Example News")
        self.assertEqual(member["source_url"], "https://example.com/")
        self.assertTrue(member["publisher_search_url"])

    def test_publisher_search_url_uses_publisher_domain(self):
        url = verify_news.build_publisher_search_url(
            "Импорт автомобилей из Японии",
            "Example News",
            "https://www.example.com/",
        )
        self.assertIn("google.com/search?q=", url)
        query = parse_qs(urlparse(url).query)["q"][0]
        self.assertIn("site:example.com", query)
        self.assertIn("Импорт", query)

    def test_publisher_search_url_falls_back_to_source_name(self):
        url = verify_news.build_publisher_search_url(
            "Новости автомобильного рынка",
            "Example News",
            "",
        )
        query = parse_qs(urlparse(url).query)["q"][0]
        self.assertIn("Example News", query)

    def test_empty_title_has_no_search_url(self):
        self.assertEqual(
            verify_news.build_publisher_search_url("", "Example", ""),
            "",
        )

    @patch.object(verify_news, "urlopen")
    def test_google_news_url_resolves_to_https_publisher(self, mock_urlopen):
        response = MagicMock()
        response.__enter__.return_value.geturl.return_value = (
            "https://example.com/automotive/news"
        )
        mock_urlopen.return_value = response
        result = verify_news.resolve_google_news_url(
            "https://news.google.com/rss/articles/example"
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["url"], "https://example.com/automotive/news")
        self.assertIsNone(result["error"])

    @patch.object(verify_news, "urlopen", side_effect=TimeoutError)
    def test_google_news_timeout_preserves_original_url(self, mock_urlopen):
        original = "https://news.google.com/rss/articles/example"
        result = verify_news.resolve_google_news_url(original)
        self.assertFalse(result["resolved"])
        self.assertEqual(result["url"], original)
        self.assertEqual(result["original_url"], original)
        self.assertEqual(result["error"], "connection_error")

    @patch.object(verify_news, "urlopen")
    def test_google_news_url_still_on_google_is_unresolved(self, mock_urlopen):
        response = MagicMock()
        response.__enter__.return_value.geturl.return_value = (
            "https://news.google.com/rss/articles/example"
        )
        mock_urlopen.return_value = response
        result = verify_news.resolve_google_news_url(
            "https://news.google.com/rss/articles/example"
        )
        self.assertFalse(result["resolved"])
        self.assertEqual(
            result["error"],
            "redirect_did_not_reach_publisher",
        )

    def test_regular_publisher_url_is_not_requested(self):
        with patch.object(verify_news, "urlopen") as mock_urlopen:
            result = verify_news.resolve_google_news_url(
                "https://example.com/news/123"
            )
        mock_urlopen.assert_not_called()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["url"], "https://example.com/news/123")

    def test_empty_url_is_not_resolved(self):
        result = verify_news.resolve_google_news_url("")
        self.assertFalse(result["resolved"])
        self.assertEqual(result["url"], "")
        self.assertEqual(result["error"], "empty_url")

    def test_non_https_publisher_url_is_rejected(self):
        result = verify_news.resolve_google_news_url("http://example.com/news")
        self.assertFalse(result["resolved"])
        self.assertEqual(result["error"], "invalid_https_url")

    @patch.object(verify_news, "resolve_google_news_url")
    def test_group_url_resolution_updates_report(self, mock_resolve):
        original = "https://news.google.com/rss/articles/example"
        publisher = "https://example.com/news/123"
        mock_resolve.return_value = {
            "original_url": original,
            "url": publisher,
            "resolved": True,
            "error": None,
        }
        groups = verify_news.build_groups([{
            "title": "Импорт автомобилей из Японии в Россию",
            "url": original,
        }])
        stats = verify_news.resolve_group_urls(groups)
        member = groups[0]["members"][0]
        self.assertEqual(member["url"], publisher)
        self.assertEqual(member["original_url"], original)
        self.assertFalse(member["source_url_needs_resolution"])
        self.assertIsNone(member["url_resolution_error"])
        self.assertEqual(stats["google_news_redirects_resolved"], 1)
        self.assertEqual(stats["google_news_redirects_unresolved"], 0)

    @patch.object(verify_news, "resolve_google_news_url")
    def test_failed_resolution_keeps_original_and_search_hint(self, mock_resolve):
        original = "https://news.google.com/rss/articles/example"
        mock_resolve.return_value = {
            "original_url": original,
            "url": original,
            "resolved": False,
            "error": "network_error",
        }
        groups = verify_news.build_groups([{
            "title": "Импорт автомобилей из Японии в Россию",
            "url": original,
            "source_name": "Example News",
            "source_url": "https://example.com/",
        }])
        stats = verify_news.resolve_group_urls(groups)
        member = groups[0]["members"][0]
        self.assertEqual(member["url"], original)
        self.assertEqual(member["original_url"], original)
        self.assertTrue(member["source_url_needs_resolution"])
        self.assertEqual(member["url_resolution_error"], "network_error")
        self.assertTrue(member["publisher_search_url"])
        self.assertEqual(stats["google_news_redirects_unresolved"], 1)

    @patch.object(verify_news, "resolve_google_news_url")
    def test_non_google_urls_are_skipped(self, mock_resolve):
        groups = verify_news.build_groups([{
            "title": "Импорт автомобилей из Японии в Россию",
            "url": "https://example.com/news/123",
        }])
        stats = verify_news.resolve_group_urls(groups)
        mock_resolve.assert_not_called()
        self.assertEqual(stats["google_news_redirects_resolved"], 0)
        self.assertEqual(stats["google_news_redirects_unresolved"], 0)
        self.assertEqual(stats["non_google_urls_skipped"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

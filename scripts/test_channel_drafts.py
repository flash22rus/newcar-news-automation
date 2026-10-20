import unittest
from unittest.mock import patch

import channel_drafts


class ChannelDraftsTests(unittest.TestCase):
    def test_only_high_score_groups_with_resolved_direct_sources_are_selected(self):
        groups = [
            {"title": "medium", "relevance_score": 64, "members": [{"url": "https://a.example/x"}]},
            {"title": "unresolved", "relevance_score": 90, "members": [{"url": "https://news.google.com/rss/x", "source_url_needs_resolution": True}]},
            {"title": "ready", "relevance_score": 80, "members": [{"url": "https://a.example/x"}]},
        ]
        chosen = channel_drafts.choose_groups({"groups": groups})
        self.assertEqual([item["title"] for item in chosen], ["ready"])

    def test_source_selection_requires_four_distinct_publisher_domains(self):
        group = {"members": [
            {"url": "https://a.example/1"},
            {"url": "https://www.a.example/2"},
        ]}
        with self.assertRaisesRegex(channel_drafts.PipelineError, "4 прямые ссылки"):
            channel_drafts.source_urls_for(group, [
                {"url": "https://b.example/3"},
                {"url": "https://c.example/4"},
            ])

    def test_source_selection_excludes_google_redirects(self):
        group = {"members": [{"url": "https://news.google.com/rss/x"}]}
        with self.assertRaises(channel_drafts.PipelineError):
            channel_drafts.source_urls_for(group, [
                {"url": "https://a.example/1"},
                {"url": "https://b.example/2"},
                {"url": "https://c.example/3"},
            ])

    def test_fact_evidence_must_reference_a_web_search_result(self):
        result_urls = ["https://publisher.example/news"]
        facts = [
            {"fact": "Подтверждённое событие", "source_urls": result_urls},
            {"fact": "Ещё один факт", "source_urls": ["https://unknown.example/news"]},
        ]
        with self.assertRaisesRegex(channel_drafts.PipelineError, "не связан"):
            channel_drafts.validate_fact_evidence(facts, result_urls)

    def test_worker_text_contains_exactly_three_additional_sources(self):
        copy = {"telegram_text": "Проверяемая новость."}
        sources = [f"https://publisher{i}.example/news" for i in range(1, 5)]
        text = channel_drafts.build_worker_text(copy, sources)
        self.assertEqual(text.count("Источник: https://"), 3)
        self.assertIn(sources[1], text)
        self.assertNotIn(sources[0], text)

    def test_worker_text_respects_safe_caption_limit(self):
        copy = {"title": "Заголовок", "telegram_text": "x" * 900}
        sources = [f"https://publisher{i}.example/news" for i in range(1, 5)]
        with self.assertRaisesRegex(channel_drafts.PipelineError, "950"):
            channel_drafts.build_worker_text(copy, sources, copy["title"])

    @patch.dict("os.environ", {"NEWCAR_CREATE_DRAFTS": "false"}, clear=True)
    def test_drafts_are_disabled_by_default(self):
        self.assertEqual(channel_drafts.main(), 0)

    @patch.object(channel_drafts, "metricool_request")
    @patch.object(channel_drafts, "request_json")
    def test_metricool_post_is_explicitly_draft_without_autopublish(self, mock_request, mock_api):
        mock_request.return_value = {"mediaId": "media-123"}
        mock_api.return_value = {"id": "draft-456"}
        result = channel_drafts.create_metricool_draft(
            "token", "user", "brand", "caption", "https://cdn.example/img.png",
        )
        body = mock_api.call_args.kwargs["body"]
        self.assertTrue(body["draft"])
        self.assertFalse(body["autoPublish"])
        self.assertFalse(body["instagramData"]["autoPublish"])
        self.assertEqual(result["status"], "draft")
        self.assertFalse(result["auto_publish"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

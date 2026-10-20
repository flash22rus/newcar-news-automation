import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import channel_drafts


class ChannelDraftsTests(unittest.TestCase):
    def test_canonical_source_url_drops_fragment(self):
        self.assertEqual(
            channel_drafts.canonical_source_url("https://News.Example/story#facts"),
            "https://news.example/story",
        )

    def test_source_url_validation_rejects_credentials_and_invalid_https(self):
        self.assertFalse(channel_drafts.is_https_url("https://user:pass@example.com/news"))
        self.assertFalse(channel_drafts.is_https_url("https://"))
        self.assertFalse(channel_drafts.is_https_url("https://bad[host/news"))

    def test_publication_state_prunes_entries_older_than_retention(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        state = {"version": 1, "entries": {
            "https://old.example/news": {"updated_at": (now - timedelta(days=31)).isoformat()},
            "https://recent.example/news": {"updated_at": (now - timedelta(days=2)).isoformat()},
        }}
        state_directory = Path(__file__).resolve().parent / ".test-publication-state"
        state_path = state_directory / "drafts.json"
        state_directory.mkdir(exist_ok=True)
        try:
            channel_drafts.save_publication_state(state, path=state_path, now=now)
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        finally:
            state_path.unlink(missing_ok=True)
            state_path.with_name(state_path.name + ".tmp").unlink(missing_ok=True)
            state_directory.rmdir()
        self.assertEqual(set(loaded["entries"]), {"https://recent.example/news"})

    def test_responses_api_text_is_extracted_from_raw_output_items(self):
        response = {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": '{"title":"Заголовок"}'}],
            }],
        }
        self.assertEqual(channel_drafts._response_text(response), '{"title":"Заголовок"}')

    def test_response_web_search_sources_are_extracted_from_action(self):
        response = {
            "output": [{
                "type": "web_search_call",
                "action": {"sources": [{"type": "url", "url": "https://one.example/news"}]},
            }],
        }
        self.assertEqual(channel_drafts._response_sources(response), ["https://one.example/news"])

    @patch.object(channel_drafts, "urlopen")
    def test_public_image_url_requires_png_content_type(self, mock_urlopen):
        response = mock_urlopen.return_value.__enter__.return_value
        response.status = 200
        response.headers.get_content_type.return_value = "image/png"
        self.assertEqual(
            channel_drafts.verify_public_image_url("https://cdn.example/news.png"),
            "https://cdn.example/news.png",
        )

    @patch.object(channel_drafts, "urlopen")
    def test_private_or_wrong_content_type_image_is_rejected(self, mock_urlopen):
        response = mock_urlopen.return_value.__enter__.return_value
        response.status = 200
        response.headers.get_content_type.return_value = "text/html"
        with self.assertRaisesRegex(channel_drafts.PipelineError, "не отдаёт изображение"):
            channel_drafts.verify_public_image_url("https://cdn.example/news.png")

    @patch.object(channel_drafts, "request_json")
    def test_telegram_worker_duplicate_pending_draft_is_idempotent(self, mock_request):
        mock_request.return_value = {
            "ok": True, "action": "news_duplicate", "draft_id": "draft-123", "status": "pending",
        }
        result = channel_drafts.send_telegram_draft(
            "secret", "https://worker.example/newsdraft", {"title": "Новость"},
        )
        self.assertTrue(result["duplicate"])
        self.assertTrue(result["approval_required"])
        self.assertEqual(result["draft_id"], "draft-123")

    @patch.object(channel_drafts, "request_json")
    def test_telegram_worker_rejected_duplicate_blocks_other_channel(self, mock_request):
        mock_request.return_value = {
            "ok": True, "action": "news_duplicate", "draft_id": "draft-123", "status": "rejected",
        }
        with self.assertRaisesRegex(channel_drafts.PipelineError, "отклонили"):
            channel_drafts.send_telegram_draft(
                "secret", "https://worker.example/newsdraft", {"title": "Новость"},
            )

    def test_only_high_score_groups_with_resolved_direct_sources_are_selected(self):
        groups = [
            {"title": "medium", "relevance_score": 64, "members": [{"url": "https://a.example/x"}]},
            {"title": "threshold", "relevance_score": 74, "members": [{"url": "https://b.example/x"}]},
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

    def test_source_selection_prioritizes_primary_and_fact_evidence(self):
        group = {"members": [{"url": "https://primary.example/article"}]}
        facts = [{"fact": "Событие", "source_urls": [
            "https://evidence-one.example/article",
            "https://evidence-two.example/article",
            "https://evidence-three.example/article",
        ]}]
        result = channel_drafts.source_urls_for(
            group,
            [{"url": "https://unrelated.example/story"}],
            facts,
        )
        self.assertEqual(result, [
            "https://primary.example/article",
            "https://evidence-one.example/article",
            "https://evidence-two.example/article",
            "https://evidence-three.example/article",
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
        result_urls = ["https://publisher.example/news", "https://second.example/news"]
        facts = [
            {"fact": "Подтверждённое событие", "source_urls": result_urls},
            {"fact": "Ещё один факт", "source_urls": ["https://unknown.example/news"]},
        ]
        with self.assertRaisesRegex(channel_drafts.PipelineError, "не связан"):
            channel_drafts.validate_fact_evidence(facts, result_urls)

    def test_fact_evidence_needs_two_independent_publishers(self):
        sources = ["https://one.example/story", "https://two.example/story"]
        facts = [
            {"fact": "Событие", "source_urls": sources},
            {"fact": "Второй факт", "source_urls": sources},
        ]
        self.assertEqual(channel_drafts.validate_fact_evidence(facts, sources), facts)

    def test_fact_evidence_rejects_two_urls_from_same_publisher(self):
        sources = ["https://one.example/story-a", "https://www.one.example/story-b"]
        facts = [
            {"fact": "Событие", "source_urls": sources},
            {"fact": "Второй факт", "source_urls": sources},
        ]
        with self.assertRaisesRegex(channel_drafts.PipelineError, "двумя независимыми"):
            channel_drafts.validate_fact_evidence(facts, sources)

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

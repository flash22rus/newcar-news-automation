
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
                "title": "Honda import news",
                "url": "https://example.com/a",
                "published_utc": "2026-10-19T10:00:00+00:00",
            },
            {
                "title": "Mazda import news",
                "url": "https://example.com/c",
                "published_utc": "2026-10-19T09:00:00+00:00",
            },
        ]

        result = check_news.deduplicate(items)

        # Toyota с одинаковым заголовком остаётся один раз.
        # Honda с URL, совпадающим после удаления #section,
        # тоже исключается. Mazda остаётся.
        self.assertEqual(len(result), 2)
        self.assertEqual(
            {item["title"] for item in result},
            {"Toyota import news", "Mazda import news"},
        )

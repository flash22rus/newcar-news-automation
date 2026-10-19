
#!/usr/bin/env python3
"""NEWCAR news candidate analyzer.

Groups similar headlines, scores relevance to NEWCAR, and prepares a
manual verification report. This script does NOT verify facts, resolve
publisher URLs, or publish news.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse


INPUT_FILE = Path("news_candidates.json")
OUTPUT_FILE = Path("news_verification.json")

# Headlines with similarity at or above this value are grouped together.
# This is a heuristic, not proof that two articles describe the same event.
SIMILARITY_THRESHOLD = 0.72

RELEVANCE_TERMS = {
    "russia_import": [
        "россия", "россии", "россию", "российский", "российские",
        "импорт", "ввоз", "поставки", "растаможка", "таможня",
        "автомобиль", "автомобили", "автомобилей", "машин",
        "легковых автомобилей", "параллельный импорт",
    ],
    "japan": [
        "япония", "японии", "японский", "японские", "японских",
        "аукцион", "аукционы",
    ],
    "china": [
        "китай", "китая", "китае", "китайский", "китайские",
        "китайских",
    ],
    "south_korea": [
        "южная корея", "корея", "кореи", "корейский", "корейские",
        "корейских",
    ],
    "fees_and_rules": [
        "утильсбор", "утилизационный сбор", "пошлина", "пошлины",
        "налог", "закон", "правила", "ограничения", "ставка",
        "ставки", "сбор",
    ],
    "logistics_and_prices": [
        "доставка", "логистика", "цена", "цены", "стоимость",
        "курс", "валюта", "рубль", "рубля", "рублей",
    ],
}

LOW_RELEVANCE_TERMS = [
    "футбол", "хоккей", "погода", "гороскоп", "рецепт",
    "кино", "сериал", "знаменитост",
]

STOP_WORDS = {
    "и", "в", "во", "на", "по", "из", "для", "с", "со", "к",
    "ко", "о", "об", "от", "до", "за", "при", "а", "но", "или",
    "как", "что", "это", "после", "перед", "между", "над", "под",
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
}

GOOGLE_NEWS_HOSTS = {
    "news.google.com",
    "www.news.google.com",
}


def normalize_text(value: str) -> str:
    """Normalize a string for approximate comparison."""
    value = value.lower().replace("ё", "е")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return " ".join(value.split())


def headline_tokens(title: str) -> set[str]:
    """Return informative words from a headline."""
    return {
        word
        for word in normalize_text(title).split()
        if len(word) > 2 and word not in STOP_WORDS
    }


def headline_similarity(first: str, second: str) -> float:
    """Combine character and word overlap into a heuristic score."""
    first_normalized = normalize_text(first)
    second_normalized = normalize_text(second)

    if not first_normalized or not second_normalized:
        return 0.0

    sequence_score = SequenceMatcher(
        None, first_normalized, second_normalized
    ).ratio()

    first_words = headline_tokens(first)
    second_words = headline_tokens(second)
    union = first_words | second_words
    word_score = (
        len(first_words & second_words) / len(union)
        if union
        else 0.0
    )

    return round((sequence_score * 0.45) + (word_score * 0.55), 3)


def get_candidate_text(candidate: dict) -> str:
    """Combine available headline and description text."""
    return " ".join(
        str(candidate.get(field, "") or "")
        for field in ("title", "description")
    ).lower().replace("ё", "е")


def score_relevance(candidate: dict) -> dict:
    """Score topical relevance; this does not establish factual accuracy."""
    text = get_candidate_text(candidate)
    matched_categories = []

    for category, terms in RELEVANCE_TERMS.items():
        if any(term in text for term in terms):
            matched_categories.append(category)

    score = min(100, len(matched_categories) * 18)

    if any(term in text for term in LOW_RELEVANCE_TERMS):
        score = max(0, score - 35)

    if not candidate.get("title"):
        score = 0

    return {
        "relevance_score": score,
        "matched_categories": matched_categories,
        "relevance_label": (
            "high" if score >= 54
            else "medium" if score >= 25
            else "low"
        ),
    }


def is_google_news_redirect(url: str) -> bool:
    """Identify Google News links that may need manual URL resolution."""
    try:
        return urlparse(url).hostname in GOOGLE_NEWS_HOSTS
    except ValueError:
        return False


def build_groups(candidates: list[dict]) -> list[dict]:
    """Group similar headlines using a greedy, explainable heuristic."""
    groups = []

    for candidate in candidates:
        title = str(candidate.get("title", "")).strip()
        if not title:
            title = "(Заголовок отсутствует)"

        best_group = None
        best_similarity = 0.0

        for group in groups:
            similarity = max(
                headline_similarity(title, member["title"])
                for member in group["members"]
            )
            if (
                similarity >= SIMILARITY_THRESHOLD
                and similarity > best_similarity
            ):
                best_group = group
                best_similarity = similarity

        if best_group is None:
            groups.append({
                "members": [dict(candidate, title=title)],
                "similarity_scores": [],
            })
        else:
            best_group["members"].append(dict(candidate, title=title))
            best_group["similarity_scores"].append(best_similarity)

    results = []

    for index, group in enumerate(groups, start=1):
        members = group["members"]
        scored_members = []

        for member in members:
            relevance = score_relevance(member)
            url = str(member.get("url", "") or "")
            redirect = is_google_news_redirect(url)

            scored_members.append({
                "title": member["title"],
                "url": url,
                "published": member.get("published"),
                "description": member.get("description", ""),
                "feed": member.get("feed", ""),
                **relevance,
                "source_url_needs_resolution": redirect,
                "manual_checks": [
                    "Открыть исходную публикацию и проверить полный текст.",
                    "Проверить дату и фактические утверждения.",
                    "Найти независимый источник, подтверждающий это событие.",
                ],
                "verification_status": "needs_manual_verification",
            })

        scored_members.sort(
            key=lambda item: item["relevance_score"],
            reverse=True,
        )

        group_score = max(
            (item["relevance_score"] for item in scored_members),
            default=0,
        )

        results.append({
            "group_id": f"NEWCAR-{index:03d}",
            "representative_title": (
                scored_members[0]["title"] if scored_members else ""
            ),
            "relevance_score": group_score,
            "article_count": len(scored_members),
            "similarity_threshold": SIMILARITY_THRESHOLD,
            "grouping_method": "headline_similarity_heuristic",
            "members": scored_members,
            "verification_status": "needs_manual_verification",
            "publication_ready": False,
        })

    results.sort(
        key=lambda group: (
            group["relevance_score"],
            group["article_count"],
        ),
        reverse=True,
    )

    return results


def main() -> int:
    if not INPUT_FILE.exists():
        print(f"ERROR: Input file not found: {INPUT_FILE}", file=sys.stderr)
        print(
            "Run 'python scripts/check_news.py' first.",
            file=sys.stderr,
        )
        return 2

    try:
        with INPUT_FILE.open("r", encoding="utf-8") as file:
            report = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        print(f"ERROR: Cannot read input JSON: {error}", file=sys.stderr)
        return 2

    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        print(
            "ERROR: Input JSON must contain a 'candidates' list.",
            file=sys.stderr,
        )
        return 2

    groups = build_groups(candidates)

    generated_at = datetime.now(timezone.utc).isoformat()
    total_articles = sum(group["article_count"] for group in groups)
    high_relevance = sum(
        1 for group in groups if group["relevance_score"] >= 54
    )
    needs_url_resolution = sum(
        1
        for group in groups
        for item in group["members"]
        if item["source_url_needs_resolution"]
    )

    output = {
        "stage": "heuristic_analysis_manual_verification_required",
        "generated_at": generated_at,
        "input_file": str(INPUT_FILE),
        "output_file": str(OUTPUT_FILE),
        "input_candidate_count": len(candidates),
        "group_count": len(groups),
        "article_count_after_grouping": total_articles,
        "high_relevance_group_count": high_relevance,
        "google_news_redirect_count": needs_url_resolution,
        "publication_ready": False,
        "automatic_fact_checking_performed": False,
        "independent_sources_verified": False,
        "warnings": [
            "Similarity grouping is heuristic and can merge unrelated stories.",
            "Different headlines in one group are not proof of the same event.",
            "Relevance scores are keyword-based and may be wrong.",
            "Google News redirect links may require manual source resolution.",
            "No facts or independent sources have been verified.",
            "Every group requires human review before use or publication.",
        ],
        "groups": groups,
    }

    try:
        with OUTPUT_FILE.open("w", encoding="utf-8") as file:
            json.dump(output, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except OSError as error:
        print(f"ERROR: Cannot write output JSON: {error}", file=sys.stderr)
        return 3

    print("=== NEWCAR NEWS ANALYSIS ===")
    print(f"Input candidates: {len(candidates)}")
    print(f"Headline groups: {len(groups)}")
    print(f"Articles retained in groups: {total_articles}")
    print(f"High-relevance groups (heuristic): {high_relevance}")
    print(f"Google News redirects to resolve: {needs_url_resolution}")
    print(f"JSON report: {OUTPUT_FILE}")
    print("Publication ready: false")
    print("Fact-checking performed: false")

    print("\n=== TOP GROUPS ===")
    for group in groups[:10]:
        print(
            f"[{group['group_id']}] "
            f"score={group['relevance_score']} "
            f"articles={group['article_count']} "
            f"{group['representative_title']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Analyze NEWCAR RSS candidates and prepare a report for manual verification."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "news_candidates.json"
OUTPUT_FILE = ROOT / "news_verification.json"

TIMEOUT_SECONDS = 12

CATEGORIES = {
    "automotive_import": [
        "импорт автомобилей",
        "ввоз автомобилей",
        "автомобили из японии",
        "автомобили из китая",
        "автомобили из кореи",
        "параллельный импорт",
        "растаможка",
        "таможенная пошлина",
    ],
    "recycling_fee": [
        "утильсбор",
        "утилизационный сбор",
        "утилизационный платеж",
    ],
    "logistics": [
        "доставка автомобилей",
        "автомобильная логистика",
        "владивосток",
        "новосибирск",
    ],
    "market_prices": [
        "цены на автомобили",
        "стоимость автомобилей",
        "авторынок",
        "рынок автомобилей",
        "продажи автомобилей",
    ],
    "regulation": [
        "правила ввоза",
        "закон об автомобилях",
        "изменение пошлин",
        "ограничения импорта",
    ],
}


def safe_https_url(url):
    """Return True only for syntactically valid HTTPS URLs without user info."""
    if not isinstance(url, str) or not url.strip():
        return False

    try:
        parsed = urlparse(url.strip())
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def is_google_news_redirect(url):
    """Detect Google News URLs that may need redirect resolution."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        return host == "news.google.com" or host.endswith(".news.google.com")
    except (TypeError, ValueError):
        return False


def build_publisher_search_url(title, source_name="", source_url=""):
    """Build a Google search hint; this is not a verified article URL."""
    title = str(title or "").strip()
    source_name = str(source_name or "").strip()
    source_url = str(source_url or "").strip()

    if not title:
        return ""

    search_terms = [title]

    if safe_https_url(source_url):
        host = (urlparse(source_url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            search_terms.append(f"site:{host}")
    elif source_name:
        search_terms.append(source_name)

    query = quote_plus(" ".join(search_terms))
    return f"https://www.google.com/search?q={query}"


def score_relevance(candidate):
    """Score how relevant an RSS candidate is to NEWCAR."""
    title = str(candidate.get("title") or "").strip()
    description = str(candidate.get("description") or "").strip()

    if not title:
        return {
            "relevance_score": 0,
            "matched_categories": [],
            "relevance_label": "low",
        }

    text = f"{title} {description}".lower()
    matched = []

    for category, keywords in CATEGORIES.items():
        if any(keyword in text for keyword in keywords):
            matched.append(category)

    score = min(100, len(matched) * 30)
    if any(word in title.lower() for word in (
        "утильсбор",
        "пошлин",
        "импорт",
        "ввоз",
    )):
        score = min(100, score + 10)

    if score >= 60:
        label = "high"
    elif score >= 30:
        label = "medium"
    else:
        label = "low"

    return {
        "relevance_score": score,
        "matched_categories": matched,
        "relevance_label": label,
    }


def normalize_member(candidate):
    """Normalize a candidate without asserting that its facts are verified."""
    title = str(candidate.get("title") or "").strip()
    original_url = str(candidate.get("url") or "").strip()
    source_name = str(candidate.get("source_name") or "").strip()
    source_url = str(candidate.get("source_url") or "").strip()

    published_utc = (
        candidate.get("published_utc")
        or candidate.get("published")
        or ""
    )

    relevance = score_relevance(candidate)

    return {
        "title": title,
        "description": str(candidate.get("description") or "").strip(),
        "url": original_url,
        "original_url": original_url,
        "source_name": source_name,
        "source_url": source_url,
        "publisher_search_url": build_publisher_search_url(
            title, source_name, source_url
        ),
        "published_utc": str(published_utc),
        "feed": str(candidate.get("feed") or ""),
        **relevance,
        "source_url_needs_resolution": is_google_news_redirect(original_url),
        "url_resolution_error": None,
        "manual_checks": [
            "Open the original article and confirm the full text.",
            "Check the publication date and publisher.",
            "Verify important figures against the primary source.",
            "Confirm that the headline reflects the article accurately.",
        ],
        "verification_status": "needs_manual_verification",
    }


def build_groups(candidates):
    """Create one report group per candidate, preserving publisher metadata."""
    if not candidates:
        return []

    groups = []
    seen = set()

    for candidate in candidates:
        member = normalize_member(candidate)
        title_key = re.sub(r"\s+", " ", member["title"].lower()).strip()
        url_key = member["url"].lower()

        dedupe_key = url_key or title_key
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        groups.append({
            "title": member["title"],
            "relevance_score": member["relevance_score"],
            "relevance_label": member["relevance_label"],
            "matched_categories": member["matched_categories"],
            "members": [member],
            "verification_status": "needs_manual_verification",
            "publication_ready": False,
        })

    return groups


def resolve_google_news_url(url):
    """Try resolving Google News redirects, retaining the original on failure."""
    original = str(url or "").strip()

    if not original:
        return {
            "original_url": original,
            "url": original,
            "resolved": False,
            "error": "empty_url",
        }

    if not safe_https_url(original):
        return {
            "original_url": original,
            "url": original,
            "resolved": False,
            "error": "invalid_https_url",
        }

    if not is_google_news_redirect(original):
        return {
            "original_url": original,
            "url": original,
            "resolved": True,
            "error": None,
        }

    request = Request(
        original,
        headers={"User-Agent": "NEWCAR-NewsVerifier/1.0"},
        method="GET",
    )

    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl()

        if not safe_https_url(final_url):
            return {
                "original_url": original,
                "url": original,
                "resolved": False,
                "error": "invalid_redirect_target",
            }

        if is_google_news_redirect(final_url):
            return {
                "original_url": original,
                "url": original,
                "resolved": False,
                "error": "redirect_did_not_reach_publisher",
            }

        return {
            "original_url": original,
            "url": final_url,
            "resolved": True,
            "error": None,
        }

    except TimeoutError:
        error = "connection_error"
    except URLError:
        error = "connection_error"
    except Exception:
        error = "connection_error"

    return {
        "original_url": original,
        "url": original,
        "resolved": False,
        "error": error,
    }


def resolve_group_urls(groups):
    """Resolve Google News links and update aggregate counts."""
    resolved_count = 0
    unresolved_count = 0
    skipped_count = 0

    for group in groups:
        for member in group.get("members", []):
            url = str(member.get("url") or "")

            if not is_google_news_redirect(url):
                skipped_count += 1
                continue

            result = resolve_google_news_url(url)
            member["original_url"] = result["original_url"]
            member["url"] = result["url"]
            member["source_url_needs_resolution"] = not result["resolved"]
            member["url_resolution_error"] = result["error"]

            if result["resolved"]:
                resolved_count += 1
            else:
                unresolved_count += 1

            if not member.get("publisher_search_url"):
                member["publisher_search_url"] = build_publisher_search_url(
                    member.get("title", ""),
                    member.get("source_name", ""),
                    member.get("source_url", ""),
                )

    return {
        "google_news_redirects_resolved": resolved_count,
        "google_news_redirects_unresolved": unresolved_count,
        "non_google_urls_skipped": skipped_count,
    }


def main():
    if not INPUT_FILE.exists():
        print(f"ERROR: input file not found: {INPUT_FILE}", file=sys.stderr)
        return 1

    try:
        with INPUT_FILE.open("r", encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read candidate JSON: {exc}", file=sys.stderr)
        return 1

    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        candidates = raw.get("candidates", [])
        if not isinstance(candidates, list):
            candidates = []
    else:
        candidates = []

    groups = build_groups(candidates)
    url_stats = resolve_group_urls(groups)

    all_members = [
        member
        for group in groups
        for member in group.get("members", [])
    ]
    high_relevance = sum(
        1 for group in groups if group["relevance_label"] == "high"
    )

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "group_count": len(groups),
        "article_count": len(all_members),
        "high_relevance_group_count": high_relevance,
        "url_resolution": url_stats,
        "publication_ready": False,
        "fact_checking_complete": False,
        "warnings": [
            "This report is an aid to manual review, not a verified news feed.",
            "A publisher search URL is a search hint, not a direct article URL.",
            "source_url may refer to a publisher homepage rather than the article.",
            "No item is approved for publication automatically.",
        ],
        "groups": groups,
    }

    try:
        with OUTPUT_FILE.open("w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except OSError as exc:
        print(f"ERROR: cannot write report: {exc}", file=sys.stderr)
        return 1

    print("NEWCAR news analyzer")
    print(f"Candidates: {len(candidates)}")
    print(f"Groups: {len(groups)}")
    print(f"Articles: {len(all_members)}")
    print(f"High-relevance groups: {high_relevance}")
    print(f"Google News redirects resolved: {url_stats['google_news_redirects_resolved']}")
    print(f"Google News redirects unresolved: {url_stats['google_news_redirects_unresolved']}")
    print(f"Non-Google URLs skipped: {url_stats['non_google_urls_skipped']}")
    print("Publication ready: false")
    print(f"JSON report saved: {OUTPUT_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

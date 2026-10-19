
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Rank NEWCAR news and resolve Google News URLs for manual review."""

import base64
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "news_candidates.json"
OUTPUT_FILE = ROOT / "news_verification.json"
TIMEOUT_SECONDS = 12
try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:
    gnewsdecoder = None

CATEGORIES = {
    "import_rules": [
        "импорт автомобилей", "импорт новых автомобилей",
        "ввоз автомобилей", "правила ввоза", "таможенная пошлина",
        "растаможка", "параллельный импорт", "пошлина на автомобиль",
        "таможенные правила", "официальный импорт",
        "откуда теперь везут машины", "везут машины в россию",
        "поставки автомобилей в россию",
    ],
    "recycling_fee": [
        "утильсбор", "утилизационный сбор", "утилизационный платеж",
        "повышения утильсбора", "вырастет утильсбор",
        "утильсбор на машины", "утильсбор на автомобили",
        "расчет утильсбора", "расчёт утильсбора",
        "привязали к длине машины", "длина машины",
        "коэффициент утильсбора",
    ],
    "japan": [
        "япония", "японии", "японских автомобилей",
        "автомобили из японии", "японские автомобили",
        "японские авто", "авто из японии",
        "аукцион японских автомобилей", "аукционы японии",
    ],
    "china": [
        "китай", "китая", "китайских автомобилей",
        "автомобили из китая", "китайские автомобили",
        "китайские авто", "авто из китая",
    ],
    "south_korea": [
        "южной кореи", "южная корея", "корейских автомобилей",
        "автомобили из кореи", "корейские автомобили",
        "корейские авто", "авто из кореи",
    ],
    "prices_and_demand": [
        "цены на автомобили", "стоимость автомобиля",
        "стоимость автомобилей", "рынок автомобилей",
        "продажи автомобилей", "подорожание автомобилей",
        "спрос на автомобили", "авторынок",
        "почему внедорожник стоит дорого", "дорого стоит",
        "стоимость машины",
    ],
    "import_volumes": [
        "импорт вырос", "импорт автомобилей вырос",
        "импорт новых автомобилей", "объем импорта",
        "объём импорта", "объем поставок", "объём поставок",
        "поставки выросли", "поставки автомобилей",
        "число ввезенных автомобилей", "количество ввезенных",
        "потерял первенство", "структура импорта",
        "почти на 40%", "вырос почти на",
    ],
    "logistics": [
        "доставка автомобилей", "автомобильная логистика",
        "владивосток", "новосибирск", "стоимость доставки",
        "перевозка автомобилей", "сроки доставки",
    ],
    "regulation": [
        "минпромторг", "правительство россии", "постановление",
        "новые правила", "изменение законодательства",
        "законодательство об автомобилях", "с 1 января",
        "вступит в силу", "привязали к длине",
        "локализация производства",
    ],
}

WEIGHTS = {
    "import_rules": 24,
    "recycling_fee": 28,
    "japan": 19,
    "china": 18,
    "south_korea": 18,
    "prices_and_demand": 13,
    "import_volumes": 18,
    "logistics": 14,
    "regulation": 16,
}

RUSSIAN_CONTEXT_TERMS = [
    "россия", "россии", "россию", "россией",
    "российский", "российская", "российское", "российские",
    "российском", "российского", "российскому", "российским",
    "российских", "российскую", "российской федерации",
    "в российскую федерацию", "в рф", "по рф",
    "минпромторг", "правительство рф",
]

SIBERIA_TERMS = ["алтайский край", "барнаул", "новосибирск", "сибирь"]

FOREIGN_MARKET_TERMS = [
    "утильсбор в казахстане", "казахстанский утильсбор",
    "авторынок азербайджана", "рынок автомобилей казахстана",
    "автомобильный рынок казахстана", "импорт автомобилей в казахстан",
    "внутренний авторынок казахстана",
]

ACTION_TERMS = [
    "изменится", "изменились", "изменился", "вступил в силу",
    "вступит в силу", "с 1 января", "повысили", "снизили",
    "новые правила", "запретили", "разрешили", "увеличился",
    "сократился", "введут", "отменили", "изменят", "приняли закон",
    "вырастет", "вырос", "выросли", "повышения", "привязали",
    "ужесточил", "ужесточили", "потерял первенство",
]

PRIORITY_TITLE_TERMS = [
    "утильсбор", "правила ввоза", "импорт автомобилей",
    "автомобили из японии", "автомобили из китая",
    "автомобили из кореи", "пошлины на автомобили",
    "импорт новых премиальных автомобилей",
    "откуда теперь везут машины",
    "официальный импорт потерял первенство", "с 1 января",
]


def safe_https_url(url):
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
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
        return host == "news.google.com" or host.endswith(".news.google.com")
    except (TypeError, ValueError):
        return False


def build_publisher_search_url(title, source_name="", source_url=""):
    """Build a search hint, not a verified article URL."""
    title = str(title or "").strip()
    source_name = str(source_name or "").strip()
    source_url = str(source_url or "").strip()

    if not title:
        return ""

    terms = [title]
    if safe_https_url(source_url):
        host = (urlparse(source_url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            terms.append(f"site:{host}")
    elif source_name:
        terms.append(source_name)

    return "https://www.google.com/search?q=" + quote_plus(" ".join(terms))


def score_relevance(candidate):
    title = str(candidate.get("title") or "").strip()
    description = str(candidate.get("description") or "").strip()

    if not title:
        return {
            "relevance_score": 0,
            "matched_categories": [],
            "relevance_label": "low",
            "ranking_reasons": ["missing_title"],
            "geography": "unknown",
        }

    title_lower = title.lower()
    text = f"{title} {description}".lower()
    matched = [
        category for category, keywords in CATEGORIES.items()
        if any(keyword in text for keyword in keywords)
    ]

    reasons = []
    score = 0
    for category in matched:
        score += WEIGHTS[category]
        reasons.append(f"topic:{category}")

    has_russian_context = any(
        term in text for term in RUSSIAN_CONTEXT_TERMS
    )
    if has_russian_context:
        score += 20
        reasons.append("geography:russia")

    if any(term in text for term in SIBERIA_TERMS):
        score += 8
        reasons.append("geography:siberia")

    foreign_only = any(term in text for term in FOREIGN_MARKET_TERMS)
    if foreign_only and not has_russian_context:
        score -= 25
        reasons.append("penalty:foreign_domestic_market")

    if any(term in text for term in ACTION_TERMS):
        score += 8
        reasons.append("actionable_change")

    if any(term in title_lower for term in PRIORITY_TITLE_TERMS):
        score += 8
        reasons.append("title_matches_priority_topic")

    score = max(0, min(100, score))
    label = "high" if score >= 65 else "medium" if score >= 35 else "low"

    return {
        "relevance_score": score,
        "matched_categories": matched,
        "relevance_label": label,
        "ranking_reasons": reasons,
        "geography": "russia" if has_russian_context else "uncertain",
    }


def normalize_member(candidate):
    title = str(candidate.get("title") or "").strip()
    original_url = str(candidate.get("url") or "").strip()
    source_name = str(candidate.get("source_name") or "").strip()
    source_url = str(candidate.get("source_url") or "").strip()
    published_utc = (
        candidate.get("published_utc")
        or candidate.get("published")
        or ""
    )

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
        **score_relevance(candidate),
        "source_url_needs_resolution": is_google_news_redirect(original_url),
        "url_resolution_error": None,
        "manual_checks": [
            "Open the original article and confirm its full text.",
            "Check publication date and publisher.",
            "Verify important figures against a primary source.",
            "Check that the article concerns the Russian market.",
            "Confirm the headline accurately reflects the article.",
        ],
        "verification_status": "needs_manual_verification",
    }


def build_groups(candidates):
    groups = []
    seen_urls = set()
    seen_titles = set()

    for candidate in candidates or []:
        member = normalize_member(candidate)
        title_key = re.sub(
            r"\s+", " ", member["title"].lower()
        ).strip()
        url_key = member["url"].lower().split("#", 1)[0].rstrip("/")

        if not title_key and not url_key:
            continue
        if url_key and url_key in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue

        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)

        groups.append({
            "title": member["title"],
            "relevance_score": member["relevance_score"],
            "relevance_label": member["relevance_label"],
            "matched_categories": member["matched_categories"],
            "ranking_reasons": member["ranking_reasons"],
            "geography": member["geography"],
            "members": [member],
            "verification_status": "needs_manual_verification",
            "publication_ready": False,
        })

    groups.sort(
        key=lambda group: (
            group["relevance_score"],
            group["members"][0].get("published_utc", ""),
        ),
        reverse=True,
    )
    return groups


def extract_publisher_url_from_id(url):
    """Try to find an explicit publisher URL in a Google News article ID."""
    try:
        parsed = urlparse(str(url or ""))
        parts = [part for part in parsed.path.split("/") if part]

        for index, part in enumerate(parts[:-1]):
            if part not in ("articles", "read"):
                continue

            token = parts[index + 1].split("?", 1)[0]
            padded = token + "=" * (-len(token) % 4)

            try:
                decoded = base64.urlsafe_b64decode(
                    padded.encode("ascii")
                )
                decoded_text = decoded.decode("utf-8", errors="ignore")
            except (ValueError, UnicodeError):
                continue

            candidates = re.findall(
                r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
                decoded_text,
            )
            for candidate in candidates:
                candidate = unquote(candidate).rstrip(".,);]")
                if (
                    safe_https_url(candidate)
                    and not is_google_news_redirect(candidate)
                ):
                    return candidate
    except (TypeError, ValueError):
        pass

    return None



def resolve_google_news_url(url):
    """Resolve Google News links and preserve the original on failure."""
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

    # First try the existing lightweight extraction.
    embedded = extract_publisher_url_from_id(original)
    if (
        embedded
        and safe_https_url(embedded)
        and not is_google_news_redirect(embedded)
    ):
        return {
            "original_url": original,
            "url": embedded,
            "resolved": True,
            "error": None,
        }

    # Then use the dedicated Google News decoder.
    if gnewsdecoder is not None:
        try:
            decoded = gnewsdecoder(original, interval=1)

            if isinstance(decoded, dict) and decoded.get("status"):
                candidate = str(
                    decoded.get("decoded_url") or ""
                ).strip()

                if (
                    safe_https_url(candidate)
                    and not is_google_news_redirect(candidate)
                ):
                    return {
                        "original_url": original,
                        "url": candidate,
                        "resolved": True,
                        "error": None,
                    }
        except Exception:
            # Fall back to HTTP resolution below.
            pass

    # Last fallback: follow the URL and inspect the returned HTML.
    request = Request(
        original,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        },
        method="GET",
    )

    error = "redirect_did_not_reach_publisher"

    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            body = response.read(1_500_000).decode(
                "utf-8", errors="ignore"
            )

        if (
            safe_https_url(final_url)
            and not is_google_news_redirect(final_url)
        ):
            return {
                "original_url": original,
                "url": final_url,
                "resolved": True,
                "error": None,
            }

        patterns = [
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
            r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
            r'<meta[^>]+name=["\']twitter:url["\'][^>]+content=["\']([^"\']+)',
        ]

        for pattern in patterns:
            for match in re.findall(
                pattern, body, flags=re.IGNORECASE
            ):
                candidate = unquote(match.strip()).replace("&amp;", "&")

                if (
                    safe_https_url(candidate)
                    and not is_google_news_redirect(candidate)
                ):
                    return {
                        "original_url": original,
                        "url": candidate,
                        "resolved": True,
                        "error": None,
                    }

    except (TimeoutError, URLError):
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
    members = [
        member for group in groups for member in group.get("members", [])
    ]

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "group_count": len(groups),
        "article_count": len(members),
        "high_relevance_group_count": sum(
            group["relevance_label"] == "high" for group in groups
        ),
        "medium_relevance_group_count": sum(
            group["relevance_label"] == "medium" for group in groups
        ),
        "low_relevance_group_count": sum(
            group["relevance_label"] == "low" for group in groups
        ),
        "url_resolution": url_stats,
        "publication_ready": False,
        "fact_checking_complete": False,
        "warnings": [
            "This report is an aid to manual review, not a verified news feed.",
            "A publisher search URL is a search hint, not a direct article URL.",
            "RSS source_url may point to a publisher homepage.",
            "Ranking is keyword-based and can misclassify context.",
            "URL extraction is best-effort; verify every resolved article manually.",
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
    print(f"Articles: {len(members)}")
    print(f"High relevance: {report['high_relevance_group_count']}")
    print(f"Medium relevance: {report['medium_relevance_group_count']}")
    print(f"Low relevance: {report['low_relevance_group_count']}")
    print(
        "Google News redirects resolved:",
        url_stats["google_news_redirects_resolved"],
    )
    print(
        "Google News redirects unresolved:",
        url_stats["google_news_redirects_unresolved"],
    )
    print("Publication ready: false")
    print(f"JSON report saved: {OUTPUT_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

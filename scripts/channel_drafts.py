#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare and submit fact-checkable NEWCAR drafts to Telegram and Metricool.

The workflow is deliberately draft-only. It never schedules or publishes posts.
"""

import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REPORT_FILE = ROOT / "news_verification.json"
OUTPUT_FILE = ROOT / "channel_drafts.json"
TIMEOUT = 60
NEWSCAR_STYLE = (
    "NEWCAR Premium Dark visual identity: premium automotive editorial photo, "
    "dark graphite and black palette, restrained vivid green accent, realistic "
    "lighting, polished composition, no text, no logos, no watermark. "
    "Use a vehicle and scene directly relevant to the news. Portrait 4:5 crop."
)


class PipelineError(RuntimeError):
    pass


def request_json(url, *, method="GET", headers=None, body=None, timeout=TIMEOUT):
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers or {},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        # Do not include request headers or credentials in errors/logs.
        raise PipelineError(f"HTTP request failed ({exc.code}) for {url.split('?')[0]}") from None
    except (URLError, TimeoutError) as exc:
        raise PipelineError(f"Network request failed: {type(exc).__name__}") from None
    try:
        return json.loads(payload.decode("utf-8")) if payload else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PipelineError("Service returned invalid JSON") from None


def is_https_url(url):
    return isinstance(url, str) and url.startswith("https://") and " " not in url


def publisher_key(url):
    match = re.match(r"https://([^/:?#]+)", str(url), flags=re.I)
    return (match.group(1).lower().removeprefix("www.") if match else "")


def source_urls_for(group, web_sources, facts=None):
    member_urls = [
        str(member.get("url") or "").strip()
        for member in (group.get("members") or [])
        if isinstance(member, dict)
    ]
    evidence_urls = [
        str(url or "").strip()
        for fact in (facts or []) if isinstance(fact, dict)
        for url in (fact.get("source_urls") or [])
    ]
    primary_url = next((url for url in member_urls if is_https_url(url) and "news.google.com" not in url.lower()), "")
    urls = [primary_url] + evidence_urls + member_urls
    for item in web_sources or []:
        candidate = item.get("url") if isinstance(item, dict) else item
        candidate = str(candidate or "").strip()
        urls.append(candidate)
    urls = [url for url in urls if is_https_url(url) and "news.google.com" not in url.lower()]
    # Four distinct publisher domains are required by NEWCAR News Draft 2.0.
    selected = []
    domains = set()
    for url in urls:
        domain = publisher_key(url)
        if domain and domain not in domains:
            selected.append(url)
            domains.add(domain)
        if len(selected) == 4:
            break
    if len(selected) != 4:
        raise PipelineError("Для отправки в Telegram нужны 4 прямые ссылки от 4 издателей")
    return selected


def choose_groups(report, minimum_score=75, limit=3):
    """Only high relevance groups with resolved, direct HTTPS sources qualify."""
    chosen = []
    for group in report.get("groups", []):
        if int(group.get("relevance_score") or 0) < minimum_score:
            continue
        members = group.get("members") or []
        if not members or any(m.get("source_url_needs_resolution") for m in members):
            continue
        urls = [m.get("url") for m in members]
        if not any(is_https_url(url) and "news.google.com" not in url.lower() for url in urls):
            continue
        chosen.append(group)
        if len(chosen) >= limit:
            break
    return chosen


def _response_sources(response):
    found = []
    for item in response.get("output", []):
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        for source in action.get("sources", []):
            url = source.get("url")
            if url and url not in found:
                found.append(url)
    return found


def _response_text(response):
    """Read raw Responses API output, while accepting SDK-style test fixtures."""
    convenience_text = response.get("output_text")
    if convenience_text:
        return convenience_text
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text") or "")
    return "\n".join(part for part in parts if part)


def validate_fact_evidence(facts, sources):
    """Require two distinct returned publishers for every machine-drafted fact."""
    known = set(sources)
    if not isinstance(facts, list) or len(facts) < 2:
        raise PipelineError("Для черновика нужны минимум два факта с источниками")
    for fact in facts:
        if not isinstance(fact, dict) or not fact.get("fact"):
            raise PipelineError("Факт должен содержать текст и ссылки на источники")
        links = fact.get("source_urls")
        if not isinstance(links, list) or not links or not all(url in known for url in links):
            raise PipelineError("Факт не связан с найденными веб-поиском источниками")
        domains = {publisher_key(url) for url in links}
        domains.discard("")
        if len(domains) < 2:
            raise PipelineError("Каждый факт должен подтверждаться минимум двумя независимыми издателями")
    return facts


def generate_copy(api_key, model, group):
    title = str(group.get("title") or "").strip()
    if not title:
        raise PipelineError("У новости нет заголовка")
    response = request_json(
        "https://api.openai.com/v1/responses",
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body={
            "model": model,
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "input": (
                "Проверь новость для российского автомобильного импортёра NEWCAR. "
                "Все тексты и факты выдай на русском языке. "
                "Используй веб-поиск, не выдумывай цифры и даты. Подготовь JSON с "
                "title, telegram_text, instagram_text, image_prompt, key_facts. "
                "Для каждого key_fact добавь минимум два source_urls разных издателей, "
                "только из результатов веб-поиска. В тексты включай только факты из key_facts. "
                "В тексте укажи, что требует проверки, если данные расходятся. "
                "Оригинальная новость: " + title + "\n" +
                "Материалы: " + json.dumps(group.get("members", []), ensure_ascii=False)
            ),
            "text": {"format": {
                "type": "json_schema",
                "name": "newcar_news_draft",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "telegram_text": {"type": "string"},
                        "instagram_text": {"type": "string"},
                        "image_prompt": {"type": "string"},
                        "key_facts": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {"fact": {"type": "string"}, "source_urls": {"type": "array", "items": {"type": "string"}}},
                            "required": ["fact", "source_urls"],
                        }},
                    },
                    "required": ["title", "telegram_text", "instagram_text", "image_prompt", "key_facts"],
                },
            }},
        },
    )
    output_text = _response_text(response)
    try:
        copy = json.loads(output_text)
    except json.JSONDecodeError:
        raise PipelineError("Модель вернула текст не в ожидаемом JSON формате") from None
    sources = _response_sources(response)
    if not sources:
        raise PipelineError("Не удалось получить проверяемые факты и источники веб-поиска")
    copy["key_facts"] = validate_fact_evidence(copy.get("key_facts"), sources)
    if re.search(r"https?://", copy.get("telegram_text", ""), flags=re.I):
        raise PipelineError("В тексте Telegram обнаружена ссылка вне строк источников")
    if len(copy.get("instagram_text", "")) > 2100:
        raise PipelineError("Instagram-текст превышает безопасный лимит 2100 символов")
    return copy, sources


def generate_image(api_key, model, prompt, output_path):
    response = request_json(
        "https://api.openai.com/v1/images/generations",
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body={"model": model, "prompt": f"{NEWSCAR_STYLE} {prompt}", "size": "1024x1280", "output_format": "png"},
    )
    image_b64 = ((response.get("data") or [{}])[0]).get("b64_json")
    if not image_b64:
        raise PipelineError("Генератор не вернул файл изображения")
    content = base64.b64decode(image_b64, validate=True)
    if len(content) < 2048:
        raise PipelineError("Сгенерированное изображение слишком мало")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return content


def upload_r2(content, object_name):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise PipelineError("Для Cloudflare R2 установите зависимости из requirements.txt") from None
    account = os.environ["R2_ACCOUNT_ID"]
    bucket = os.environ["R2_BUCKET"]
    access = os.environ["R2_ACCESS_KEY_ID"]
    secret = os.environ["R2_SECRET_ACCESS_KEY"]
    public_base = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")
    if not is_https_url(public_base):
        raise PipelineError("Публичный домен Cloudflare R2 должен быть HTTPS")
    client = boto3.client(
        "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=access, aws_secret_access_key=secret,
        region_name="auto", config=Config(signature_version="s3v4"),
    )
    client.put_object(Bucket=bucket, Key=object_name, Body=content, ContentType="image/png")
    public_url = f"{public_base}/{quote(object_name)}"
    if not is_https_url(public_url):
        raise PipelineError("Cloudflare R2 не сформировал публичный HTTPS URL")
    return public_url


def verify_public_image_url(image_url):
    if not is_https_url(image_url):
        raise PipelineError("Ссылка на изображение должна быть HTTPS")
    request = Request(image_url, method="HEAD", headers={"User-Agent": "NEWCAR-News-Automation/1.0"})
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            if response.status < 200 or response.status >= 300:
                raise PipelineError("Публичный URL изображения вернул ошибочный статус")
            if content_type != "image/png":
                raise PipelineError("Публичный URL не отдаёт изображение PNG")
    except HTTPError as exc:
        raise PipelineError(f"Публичный URL изображения недоступен (HTTP {exc.code})") from None
    except (URLError, TimeoutError) as exc:
        raise PipelineError(f"Проверка URL изображения не прошла: {type(exc).__name__}") from None
    return image_url


def send_telegram_draft(api_key, endpoint, payload):
    if not is_https_url(endpoint):
        raise PipelineError("Telegram Worker endpoint должен быть HTTPS")
    result = request_json(
        endpoint,
        method="POST",
        headers={"X-NEWCAR-SECRET": api_key, "Content-Type": "application/json"},
        body=payload,
    )
    if (result.get("action") != "news_draft_created" or
            result.get("status") != "pending" or
            result.get("approval_required") is not True):
        raise PipelineError("Telegram Worker не подтвердил создание ожидающего черновика")
    return {"status": result.get("status"), "draft_id": result.get("draft_id"), "approval_required": True}


def metricool_request(token, user_id, blog_id, endpoint, *, method="GET", body=None):
    query = urlencode({"userId": user_id, "blogId": blog_id})
    url = f"https://app.metricool.com/api/{endpoint}?{query}"
    return request_json(
        url, method=method,
        headers={"X-Mc-Auth": token, "Content-Type": "application/json"}, body=body,
    )


def create_metricool_draft(token, user_id, blog_id, text, image_url):
    normalized = request_json(
        "https://app.metricool.com/api/actions/normalize/image/url?" +
        urlencode({"url": image_url, "userId": user_id, "blogId": blog_id}),
        headers={"X-Mc-Auth": token, "Content-Type": "application/json"},
    )
    media_data = normalized.get("data") if isinstance(normalized.get("data"), dict) else {}
    media_id = normalized.get("mediaId") or normalized.get("id") or media_data.get("mediaId") or media_data.get("id")
    if not media_id:
        raise PipelineError("Metricool не вернул mediaId для изображения")
    # Metricool requires a publicationDate even for a saved draft. The date is a
    # placeholder only: draft=true and both auto-publish switches stay disabled.
    draft_date = (datetime.now(ZoneInfo("Asia/Barnaul")) + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0,
    )
    body = {
        "publicationDate": {
            "dateTime": draft_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": "Asia/Barnaul",
        },
        "text": text,
        "providers": [{"network": "instagram"}],
        "media": {"mediaId": str(media_id)},
        "draft": True,
        "autoPublish": False,
        "instagramData": {"autoPublish": False},
    }
    response = metricool_request(
        token, user_id, blog_id, "v2/scheduler/posts", method="POST", body=body,
    )
    post_id = response.get("id") or response.get("uuid")
    if not post_id:
        raise PipelineError("Metricool не подтвердил создание Instagram Draft")
    return {"status": "draft", "id": post_id, "auto_publish": False}


def build_worker_text(copy, sources, title=""):
    # Worker contract: one primary URL in sourceUrl and three additional Source lines.
    extra = "\n".join(f"Источник: {url}" for url in sources[1:4])
    text = (copy["telegram_text"].strip() + "\n\n" + extra).strip()
    caption = f"{title}\n\n{text}\n\nИсточник: {sources[0]}".strip()
    if len(caption) > 950:
        raise PipelineError("Подпись Telegram длиннее безопасного лимита 950 символов")
    return text


def main():
    if os.environ.get("NEWCAR_CREATE_DRAFTS", "false").lower() != "true":
        print("Создание черновиков выключено. Для ручного запуска workflow установите NEWCAR_CREATE_DRAFTS=true.")
        return 0
    required = ["OPENAI_API_KEY", "OPENAI_TEXT_MODEL", "OPENAI_IMAGE_MODEL", "TELEGRAM_WORKER_URL", "TELEGRAM_WORKER_SECRET", "METRICOOL_API_TOKEN", "METRICOOL_USER_ID", "METRICOOL_BLOG_ID", "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_PUBLIC_BASE_URL"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise PipelineError("Не настроены обязательные GitHub Secrets/variables: " + ", ".join(missing))
    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    groups = choose_groups(report, minimum_score=int(os.getenv("NEWCAR_MIN_RELEVANCE", "75")), limit=int(os.getenv("NEWCAR_MAX_DRAFTS", "3")))
    results = []
    write_results(results, len(groups))
    for group in groups:
        item = {"title": group.get("title"), "status": "incomplete", "telegram": None, "instagram": None}
        try:
            copy, web_sources = generate_copy(os.environ["OPENAI_API_KEY"], os.environ["OPENAI_TEXT_MODEL"], group)
            sources = source_urls_for(group, web_sources, copy["key_facts"])
            item["source_url"] = sources[0]
            item["verification_sources"] = sources
            item["title"] = copy["title"]
            item["key_facts"] = copy["key_facts"]
            source_hash = hashlib.sha256(sources[0].encode("utf-8")).hexdigest()[:20]
            image_path = ROOT / "generated" / f"{source_hash}.png"
            image = generate_image(os.environ["OPENAI_API_KEY"], os.environ["OPENAI_IMAGE_MODEL"], copy["image_prompt"], image_path)
            image_url = upload_r2(image, f"newcar-news/{source_hash}.png")
            verify_public_image_url(image_url)
            item["image_url"] = image_url
            telegram = send_telegram_draft(os.environ["TELEGRAM_WORKER_SECRET"], os.environ["TELEGRAM_WORKER_URL"], {
                "title": copy["title"], "text": build_worker_text(copy, sources, copy["title"]),
                "sourceUrl": sources[0], "imageUrl": image_url,
            })
            item["telegram"] = telegram
            instagram = create_metricool_draft(os.environ["METRICOOL_API_TOKEN"], os.environ["METRICOOL_USER_ID"], os.environ["METRICOOL_BLOG_ID"], copy["instagram_text"], image_url)
            item["instagram"] = instagram
            item["status"] = "drafts_created"
            item["fact_checking_complete"] = False
            item["publication_ready"] = False
        except (PipelineError, KeyError, ValueError) as exc:
            # Keep completed channel state visible if the other integration fails.
            item["error"] = str(exc)
        except Exception as exc:
            # Third-party SDK exceptions may include request details; keep logs redacted.
            item["error"] = f"Ошибка интеграции: {type(exc).__name__}"
        results.append(item)
        write_results(results, len(groups))
    failed = sum(item["status"] != "drafts_created" for item in results)
    print(f"Готово черновиков в обоих каналах: {len(results) - failed}; ошибок/пропусков: {failed}. Публикация не выполнялась.")
    return 1 if failed else 0


def write_results(results, eligible_count):
    OUTPUT_FILE.write_text(json.dumps({
        "drafts": results,
        "eligible_group_count": eligible_count,
        "completed_group_count": sum(item.get("status") == "drafts_created" for item in results),
        "publication_ready": False,
        "fact_checking_complete": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PipelineError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

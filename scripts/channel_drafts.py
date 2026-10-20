#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare and submit fact-checkable NEWCAR drafts to Telegram and Metricool.

The workflow is deliberately draft-only. It never schedules or publishes posts.
"""

import base64
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REPORT_FILE = ROOT / "news_verification.json"
OUTPUT_FILE = ROOT / "channel_drafts.json"
STATE_FILE = ROOT / ".newcar-state" / "drafts.json"
STATE_RETENTION_DAYS = 30
TIMEOUT = 60
NEWSCAR_STYLE = (
    "NEWCAR Premium Dark visual identity: premium automotive editorial photo, "
    "dark graphite and black palette, restrained vivid green accent, realistic "
    "lighting, polished composition, no text, no logos, no watermark. "
    "Use a vehicle and scene directly relevant to the news. Portrait 4:5 crop."
)
CAROUSEL_SIZE = (1080, 1350)
MAX_CAROUSEL_IMAGE_BYTES = 12 * 1024 * 1024


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
    if not isinstance(url, str) or " " in url:
        return False
    try:
        parsed = urlsplit(url.strip())
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def canonical_source_url(url):
    if not is_https_url(url):
        return ""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def primary_source_url(group):
    for member in group.get("members") or []:
        url = canonical_source_url(member.get("url") or "") if isinstance(member, dict) else ""
        if url and "news.google.com" not in url.lower():
            return url
    return ""


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
    """Only high relevance, Russia-focused groups with direct sources qualify."""
    chosen = []
    for group in report.get("groups", []):
        if int(group.get("relevance_score") or 0) < minimum_score:
            continue
        if group.get("geography") != "russia":
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
                "title, telegram_text, instagram_text, buyer_impact, image_prompt, key_facts. "
                "Для каждого key_fact добавь минимум два source_urls разных издателей, "
                "только из результатов веб-поиска. В тексты включай только факты из key_facts. "
                "buyer_impact: не более 25 слов о возможном влиянии на российского покупателя; "
                "если влияние не подтверждается материалами, прямо так и скажи. Не выдумывай цены и сроки. "
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
                        "buyer_impact": {"type": "string"},
                        "image_prompt": {"type": "string"},
                        "key_facts": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {"fact": {"type": "string"}, "source_urls": {"type": "array", "items": {"type": "string"}}},
                            "required": ["fact", "source_urls"],
                        }},
                    },
                    "required": ["title", "telegram_text", "instagram_text", "buyer_impact", "image_prompt", "key_facts"],
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
    impact = str(copy.get("buyer_impact") or "").strip()
    if not impact or len(impact.split()) > 25:
        raise PipelineError("Влияние новости на покупателя должно быть описано не более чем в 25 словах")
    copy["buyer_impact"] = impact
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


def fetch_public_image(image_url):
    """Read a previously uploaded R2 PNG for editorial slide composition."""
    if not is_https_url(image_url):
        raise PipelineError("Ссылка на изображение должна быть HTTPS")
    base_url = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url or urlsplit(image_url).netloc.lower() != urlsplit(base_url).netloc.lower():
        raise PipelineError("Изображение карусели должно находиться в настроенном публичном домене R2")
    request = Request(image_url, headers={"User-Agent": "NEWCAR-News-Automation/1.0"})
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            if response.status < 200 or response.status >= 300:
                raise PipelineError("Не удалось получить изображение для карусели")
            if response.headers.get_content_type() != "image/png":
                raise PipelineError("Исходное изображение карусели должно быть PNG")
            content = response.read(MAX_CAROUSEL_IMAGE_BYTES + 1)
    except HTTPError as exc:
        raise PipelineError(f"Изображение карусели недоступно (HTTP {exc.code})") from None
    except (URLError, TimeoutError) as exc:
        raise PipelineError(f"Не удалось загрузить изображение карусели: {type(exc).__name__}") from None
    if len(content) > MAX_CAROUSEL_IMAGE_BYTES:
        raise PipelineError("Исходное изображение карусели превышает лимит 12 МБ")
    return content


def _carousel_font(size, bold=False):
    from PIL import ImageFont

    candidates = (
        [r"C:\Windows\Fonts\arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        [r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    raise PipelineError("Не найден шрифт с поддержкой кириллицы для карусели")


def _wrapped_lines(draw, text, font, max_width, max_lines):
    words = str(text or "").split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    else:
        if current:
            lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    consumed_words = sum(len(line.split()) for line in lines)
    if consumed_words < len(words) and lines:
        lines[-1] = lines[-1].rstrip("…") + "…"
    return lines


def _draw_wrapped(draw, text, font, x, y, max_width, *, max_lines, color, line_gap=8):
    lines = _wrapped_lines(draw, text, font, max_width, max_lines)
    line_height = font.size + line_gap
    for line in lines:
        draw.text((x, y), line, font=font, fill=color)
        y += line_height
    return y


def _short_words(text, limit):
    words = str(text or "").split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(".,:;—-") + "…"


def render_carousel_slide(kind, *, story=None, image_content=None, index=0):
    """Render one 1080x1350 NEWCAR Premium Dark carousel slide."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise PipelineError("Для карусели установите Pillow из requirements.txt") from None

    if kind not in {"cover", "story", "cta"}:
        raise PipelineError("Неизвестный тип слайда Instagram-карусели")
    if kind == "story":
        if not isinstance(story, dict) or not image_content:
            raise PipelineError("Для слайда новости нужны проверенный текст и изображение")
        try:
            base = Image.open(io.BytesIO(image_content)).convert("RGB")
        except Exception:
            raise PipelineError("Не удалось прочитать PNG-изображение новости") from None
        base = base.resize(CAROUSEL_SIZE, Image.Resampling.LANCZOS)
        shade = Image.new("RGBA", CAROUSEL_SIZE, (5, 7, 9, 68))
        base = Image.alpha_composite(base.convert("RGBA"), shade)
        panel = Image.new("RGBA", CAROUSEL_SIZE, (0, 0, 0, 0))
        panel_draw = ImageDraw.Draw(panel)
        panel_draw.rounded_rectangle((54, 548, 1026, 1292), radius=34, fill=(21, 24, 28, 238))
        base = Image.alpha_composite(base, panel).convert("RGB")
    else:
        base = Image.new("RGB", CAROUSEL_SIZE, "#0B0D0F")
        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle((54, 66, 1026, 1284), radius=42, outline="#252A30", width=3)
        draw.rectangle((96, 126, 112, 252), fill="#39D353")

    draw = ImageDraw.Draw(base)
    green = "#39D353"
    white = "#F5F7F8"
    muted = "#A7ADB4"
    small_bold = _carousel_font(25, bold=True)
    title_font = _carousel_font(58 if kind == "story" else 78, bold=True)
    body_font = _carousel_font(31 if kind == "story" else 36)
    small_font = _carousel_font(22)

    if kind == "cover":
        draw.text((148, 135), "АВТОИМПОРТ · НОВОСТИ", font=small_bold, fill=green)
        _draw_wrapped(draw, "ГЛАВНОЕ ОБ АВТОИМПОРТЕ", title_font, 96, 390, 880,
                      max_lines=3, color=white, line_gap=22)
        _draw_wrapped(draw, "Что меняется для российского рынка и покупателей",
                      body_font, 100, 790, 850, max_lines=3, color=muted, line_gap=18)
        draw.text((100, 1180), "ПОДБОРКА НОВОСТЕЙ NEWCAR", font=small_bold, fill=green)
    elif kind == "cta":
        draw.text((148, 135), "NEWCAR · АВТОИМПОРТ", font=small_bold, fill=green)
        _draw_wrapped(draw, "СЛЕДИТЕ ЗА ИЗМЕНЕНИЯМИ", title_font, 96, 390, 900,
                      max_lines=3, color=white, line_gap=22)
        _draw_wrapped(draw, "Сохраняйте подборку и проверяйте детали перед покупкой автомобиля.",
                      body_font, 100, 790, 850, max_lines=4, color=muted, line_gap=18)
        draw.text((100, 1180), "НОВОСТИ И РАЗБОРЫ · NEWCAR", font=small_bold, fill=green)
    else:
        title = _short_words(story.get("title"), 8)
        fact = _short_words((story.get("key_facts") or [{}])[0].get("fact"), 25)
        impact = _short_words(story.get("buyer_impact"), 25)
        source = str(story.get("source_url") or "")
        if not title or not fact or not impact or not is_https_url(source):
            raise PipelineError("Слайд новости не прошёл проверку текста или прямого источника")
        publisher = (urlsplit(source).hostname or "источник").removeprefix("www.")
        draw.text((94, 590), f"НОВОСТЬ {index:02d} · АВТОИМПОРТ", font=small_bold, fill=green)
        y = _draw_wrapped(draw, title, title_font, 94, 642, 892,
                          max_lines=3, color=white, line_gap=8)
        draw.text((94, max(830, y + 22)), "ЧТО ПРОИЗОШЛО", font=small_bold, fill=green)
        _draw_wrapped(draw, fact, body_font, 94, max(874, y + 64), 892,
                      max_lines=3, color=white, line_gap=6)
        draw.text((94, 1068), "ВОЗМОЖНОЕ ВЛИЯНИЕ", font=small_bold, fill=green)
        _draw_wrapped(draw, impact, small_font, 94, 1112, 892,
                      max_lines=3, color=muted, line_gap=5)
        draw.text((94, 1252), f"Источник: {publisher}", font=_carousel_font(18), fill=muted)

    output = io.BytesIO()
    base.save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_carousel_image_urls(stories, digest):
    if not stories or len(stories) > 8:
        raise PipelineError("Карусель может включать от 1 до 8 новостных слайдов")
    images = [render_carousel_slide("cover")]
    for index, story in enumerate(stories, start=1):
        source_image = fetch_public_image(story["image_url"])
        images.append(render_carousel_slide("story", story=story, image_content=source_image, index=index))
    images.append(render_carousel_slide("cta"))
    urls = []
    labels = ["cover"] + [f"story-{index:02d}" for index in range(1, len(stories) + 1)] + ["cta"]
    for label, content in zip(labels, images):
        image_url = upload_r2(content, f"newcar-news/carousels/{digest}/{label}.png")
        urls.append(verify_public_image_url(image_url))
    return urls


def build_carousel_caption(stories):
    titles = [_short_words(story.get("title"), 8) for story in stories]
    if not titles:
        raise PipelineError("Для подписи карусели нет новостей")
    headlines = "; ".join(titles)
    caption = (
        f"Главное об автоимпорте в России: {headlines}. "
        "В карусели — что произошло и какое это может иметь значение для покупателя. "
        "Сохраняйте подборку. #автоимпорт #автоновости #автомобили #NEWCAR"
    )
    if len(caption) > 2100:
        raise PipelineError("Подпись Instagram превышает безопасный лимит 2100 символов")
    return caption


def send_telegram_draft(api_key, endpoint, payload):
    if not is_https_url(endpoint):
        raise PipelineError("Telegram Worker endpoint должен быть HTTPS")
    result = request_json(
        endpoint,
        method="POST",
        headers={"X-NEWCAR-SECRET": api_key, "Content-Type": "application/json"},
        body=payload,
    )
    action = result.get("action")
    status = result.get("status")
    draft_id = result.get("draft_id")
    if action == "news_draft_created" and status == "pending" and result.get("approval_required") is True:
        return {"status": status, "draft_id": draft_id, "approval_required": True, "duplicate": False}
    if action == "news_duplicate" and draft_id and status in {"pending", "published"}:
        return {
            "status": status,
            "draft_id": draft_id,
            "approval_required": status == "pending",
            "duplicate": True,
        }
    if action == "news_duplicate" and status == "rejected":
        raise PipelineError("Telegram: эту новость уже отклонили; второй канал не создавался")
    raise PipelineError("Telegram Worker не подтвердил создание или наличие ожидающего черновика")


def metricool_request(token, user_id, blog_id, endpoint, *, method="GET", body=None):
    query = urlencode({"userId": user_id, "blogId": blog_id})
    url = f"https://app.metricool.com/api/{endpoint}?{query}"
    return request_json(
        url, method=method,
        headers={"X-Mc-Auth": token, "Content-Type": "application/json"}, body=body,
    )


def create_metricool_draft(token, user_id, blog_id, text, image_urls):
    """Create an Instagram draft with one image or an ordered carousel."""
    if isinstance(image_urls, str):
        image_urls = [image_urls]
    if not isinstance(image_urls, (list, tuple)) or not 1 <= len(image_urls) <= 10:
        raise PipelineError("Для Instagram нужно от 1 до 10 изображений")
    if any(not is_https_url(url) for url in image_urls):
        raise PipelineError("Все изображения Metricool должны иметь публичный HTTPS URL")

    media_ids = []
    for image_url in image_urls:
        normalized = request_json(
            "https://app.metricool.com/api/actions/normalize/image/url?" +
            urlencode({"url": image_url, "userId": user_id, "blogId": blog_id}),
            headers={"X-Mc-Auth": token, "Content-Type": "application/json"},
        )
        media_data = normalized.get("data") if isinstance(normalized.get("data"), dict) else {}
        media_id = normalized.get("mediaId") or normalized.get("id") or media_data.get("mediaId") or media_data.get("id")
        if not media_id:
            raise PipelineError("Metricool не вернул mediaId для изображения")
        media_ids.append(str(media_id))
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
        # The current OpenAPI ScheduledPost schema defines `media` as an
        # ordered array of media ID strings, including single-image posts.
        "media": media_ids,
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
    state = load_publication_state()
    carousel_stories = []
    carousel_result = {"status": "not_created", "auto_publish": False}
    write_results(results, len(groups), carousel_result)
    for group in groups:
        item = {"title": group.get("title"), "status": "incomplete", "telegram": None, "instagram": None}
        try:
            state_source = primary_source_url(group)
            if not state_source:
                raise PipelineError("У новости нет прямого HTTPS первоисточника")
            saved = state["entries"].get(state_source, {})
            copy = saved.get("copy")
            sources = saved.get("channel_sources")
            web_sources = saved.get("web_sources") or []
            image_url = saved.get("image_url")
            if (not isinstance(copy, dict) or not copy.get("buyer_impact") or
                    not isinstance(sources, list) or len(sources) != 4 or not is_https_url(image_url)):
                copy, web_sources = generate_copy(os.environ["OPENAI_API_KEY"], os.environ["OPENAI_TEXT_MODEL"], group)
                sources = source_urls_for(group, web_sources, copy["key_facts"])
                source_hash = hashlib.sha256(state_source.encode("utf-8")).hexdigest()[:20]
                image_path = ROOT / "generated" / f"{source_hash}.png"
                image = generate_image(os.environ["OPENAI_API_KEY"], os.environ["OPENAI_IMAGE_MODEL"], copy["image_prompt"], image_path)
                image_url = upload_r2(image, f"newcar-news/{source_hash}.png")
                verify_public_image_url(image_url)
                saved.update({
                    "copy": copy,
                    "web_sources": web_sources,
                    "channel_sources": sources,
                    "image_url": image_url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                state["entries"][state_source] = saved
                save_publication_state(state)
            else:
                verify_public_image_url(image_url)
            item["source_url"] = state_source
            item["verification_sources"] = sources
            item["title"] = copy["title"]
            item["key_facts"] = copy["key_facts"]
            item["buyer_impact"] = copy["buyer_impact"]
            item["image_url"] = image_url
            telegram = saved.get("telegram")
            if not telegram:
                telegram = send_telegram_draft(os.environ["TELEGRAM_WORKER_SECRET"], os.environ["TELEGRAM_WORKER_URL"], {
                    "title": copy["title"], "text": build_worker_text(copy, sources, copy["title"]),
                    "sourceUrl": state_source, "imageUrl": image_url,
                })
                saved["telegram"] = telegram
                saved["updated_at"] = datetime.now(timezone.utc).isoformat()
                state["entries"][state_source] = saved
                save_publication_state(state)
            item["telegram"] = telegram
            item["status"] = "telegram_pending"
            carousel_stories.append({
                "source_url": state_source,
                "title": copy["title"],
                "key_facts": copy["key_facts"],
                "buyer_impact": copy["buyer_impact"],
                "image_url": image_url,
            })
            item["fact_checking_complete"] = False
            item["publication_ready"] = False
        except (PipelineError, KeyError, ValueError) as exc:
            item["error"] = str(exc)
        except Exception as exc:
            item["error"] = f"Ошибка интеграции: {type(exc).__name__}"
        results.append(item)
        write_results(results, len(groups), carousel_result)

    if groups and len(carousel_stories) == len(groups):
        try:
            digest_source = json.dumps([story["source_url"] for story in carousel_stories], ensure_ascii=False)
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:24]
            saved_carousel = state.setdefault("carousels", {}).get(digest)
            if isinstance(saved_carousel, dict) and saved_carousel.get("status") == "draft" and saved_carousel.get("id"):
                carousel_result = {key: value for key, value in saved_carousel.items() if key != "updated_at"}
            else:
                legacy_drafts = [
                    state["entries"].get(story["source_url"], {}).get("instagram")
                    for story in carousel_stories
                ]
                if any(isinstance(draft, dict) and draft.get("id") for draft in legacy_drafts):
                    raise PipelineError(
                        "В кэше найдены прежние отдельные Instagram Draft. Проверьте их в Metricool "
                        "перед созданием общей карусели, чтобы не получить дубли."
                    )
                image_urls = build_carousel_image_urls(carousel_stories, digest)
                carousel_result = create_metricool_draft(
                    os.environ["METRICOOL_API_TOKEN"], os.environ["METRICOOL_USER_ID"],
                    os.environ["METRICOOL_BLOG_ID"], build_carousel_caption(carousel_stories), image_urls,
                )
                carousel_result["story_count"] = len(carousel_stories)
                carousel_result["media_count"] = len(image_urls)
                carousel_result["image_urls"] = image_urls
                carousel_result["digest"] = digest
                saved_carousel = dict(carousel_result)
                saved_carousel["updated_at"] = datetime.now(timezone.utc).isoformat()
                state.setdefault("carousels", {})[digest] = saved_carousel
                save_publication_state(state)
            for item in results:
                if item.get("source_url") in {story["source_url"] for story in carousel_stories}:
                    item["instagram"] = {
                        "status": "draft", "id": carousel_result["id"], "auto_publish": False,
                    }
                    item["status"] = "drafts_created" if item.get("telegram") else "instagram_draft_created"
            carousel_result = {key: value for key, value in carousel_result.items() if key != "digest"}
        except (PipelineError, KeyError, ValueError) as exc:
            carousel_result = {"status": "error", "auto_publish": False, "error": str(exc)}
        except Exception as exc:
            carousel_result = {"status": "error", "auto_publish": False, "error": f"Ошибка интеграции: {type(exc).__name__}"}
    elif groups:
        carousel_result = {
            "status": "not_created", "auto_publish": False,
            "error": "Карусель не создана: не все выбранные новости прошли подготовку.",
        }

    write_results(results, len(groups), carousel_result)
    failed = sum(item["status"] != "drafts_created" for item in results)
    print(f"Подготовлено комплектов Telegram и Instagram: {len(results) - failed}; ошибок: {failed}. Публикация не выполнялась.")
    return 1 if failed else 0


def write_results(results, eligible_count, instagram_carousel=None):
    OUTPUT_FILE.write_text(json.dumps({
        "drafts": results,
        "instagram_carousel": instagram_carousel or {"status": "not_created", "auto_publish": False},
        "eligible_group_count": eligible_count,
        "completed_group_count": sum(item.get("status") in {"drafts_created", "already_drafted"} for item in results),
        "publication_ready": False,
        "fact_checking_complete": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_publication_state(path=STATE_FILE):
    if not path.exists():
        return {"version": 2, "entries": {}, "carousels": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise PipelineError("Не удалось прочитать кэш идемпотентности черновиков") from None
    if not isinstance(state, dict) or state.get("version") not in {1, 2} or not isinstance(state.get("entries"), dict):
        raise PipelineError("Кэш идемпотентности черновиков имеет неподдерживаемый формат")
    if not isinstance(state.get("carousels", {}), dict):
        raise PipelineError("Кэш каруселей Instagram имеет неподдерживаемый формат")
    state.setdefault("carousels", {})
    state["version"] = 2
    return state


def save_publication_state(state, path=STATE_FILE, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    entries = state.get("entries", {})
    retained = {}
    for source_url, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        try:
            updated_at = datetime.fromisoformat(entry["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if updated_at >= cutoff:
            retained[source_url] = entry
    carousels = {}
    for digest, carousel in state.get("carousels", {}).items():
        if not isinstance(carousel, dict):
            continue
        try:
            updated_at = datetime.fromisoformat(carousel["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if updated_at >= cutoff:
            carousels[digest] = carousel
    payload = {"version": 2, "entries": retained, "carousels": carousels}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    state["entries"] = retained
    state["carousels"] = carousels
    state["version"] = 2
    return state


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PipelineError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

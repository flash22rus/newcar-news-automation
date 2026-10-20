
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Создание редакционного отчёта NEWCAR для ручной проверки."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "news_verification.json"
OUTPUT_FILE = ROOT / "news_editorial_report.md"


def render(report):
    lines = [
        "# NEWCAR — редакционный отчёт",
        "",
        f"Сформирован (UTC): {report.get('generated_utc', 'не указано')}",
        f"Кандидатов: {report.get('candidate_count', 0)}",
        f"Групп новостей: {report.get('group_count', 0)}",
        "",
        "> Это очередь для ручной проверки, а не готовая к публикации лента.",
        "> Публикация не разрешена автоматически.",
        "",
        "## Кандидаты по приоритету",
        "",
    ]

    groups = report.get("groups", [])

    if not groups:
        lines.extend(["Кандидатов нет.", ""])

    for index, group in enumerate(groups, start=1):
        member = (group.get("members") or [{}])[0]

        title = str(
            group.get("title")
            or member.get("title")
            or "Без заголовка"
        ).replace("\n", " ").strip()

        source = member.get("source_name") or "Источник не указан"
        published = member.get("published_utc") or "Дата не указана"
        url = member.get("url") or "Ссылка не найдена"
        original_url = member.get("original_url") or ""
        search_url = member.get("publisher_search_url") or ""
        status = member.get("verification_status") or (
            "needs_manual_verification"
        )

        lines.extend([
            f"### {index}. {title}",
            "",
            f"- **Релевантность:** "
            f"{group.get('relevance_score', 0)}/100 "
            f"({group.get('relevance_label', 'low')})",
            "- Оценка автоматическая, по ключевым словам.",
            f"- **Источник:** {source}",
            f"- **Материалов в группе:** {len(group.get('members') or [])}; "
            f"изданий: {group.get('source_count', 1)}",
            f"- **Дата публикации (UTC):** {published}",
            f"- **Статус проверки:** {status}",
            f"- **Ссылка на материал:** {url}",
        ])

        for related in (group.get("members") or [])[1:]:
            related_source = related.get("source_name") or "Источник не указан"
            related_url = related.get("url") or "Ссылка не найдена"
            related_date = related.get("published_utc") or "Дата не указана"
            lines.append(
                f"- **Дополнительная публикация:** {related_source}; "
                f"{related_date}; {related_url}"
            )

        if original_url and original_url != url:
            lines.append(
                f"- **Исходная ссылка Google News:** {original_url}"
            )

        if search_url:
            lines.append(
                "- **Поиск у издателя (подсказка, не подтверждение):** "
                f"{search_url}"
            )

        if member.get("source_url_needs_resolution"):
            lines.append(
                "- **Внимание:** ссылка Google News не разрешена "
                "до страницы издателя."
            )

        if member.get("source_url"):
            lines.append(
                f"- **Сайт издателя из RSS:** {member['source_url']} "
                "(может быть главной страницей, а не статьёй)"
            )

        lines.extend([
            "",
            "**Перед использованием проверьте:** полный текст и дату; "
            "первоисточник цифр; независимое подтверждение; соответствие "
            "заголовка содержанию; связь с рынком России.",
            "",
        ])

    lines.extend([
        "## Статус публикации",
        "",
        "- publication_ready: false",
        "- fact_checking_complete: false",
        "- Автоматическая отправка в Telegram не выполняется.",
        "",
    ])

    return "\n".join(lines)


def main():
    if not INPUT_FILE.exists():
        raise SystemExit(f"Не найден входной файл: {INPUT_FILE}")

    with INPUT_FILE.open("r", encoding="utf-8") as handle:
        report = json.load(handle)

    OUTPUT_FILE.write_text(render(report), encoding="utf-8")
    print(f"Editorial report saved: {OUTPUT_FILE.name}")
    print("Publication ready: false")


if __name__ == "__main__":
    main()

"""Подписи, хештеги и чек-лист правил для каждого клипа под каждую площадку.

Каждая подпись уникальна (разные вторые строки и ротация хештегов) — большинство кампаний
отклоняют одинаковые подписи и один и тот же монтаж на разных аккаунтах.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from .campaign import Campaign
from .segment import Candidate


SECOND_LINES = {
    "ru": [
        "Сохрани, чтобы не потерять.",
        "А вы с этим согласны? Пишите в комментариях.",
        "Полный выпуск — у автора.",
        "Самое важное — в последних секундах.",
        "Кому это нужно услышать — отметьте.",
        "Коротко о главном, без воды.",
    ],
    "en": [
        "Save this one for later.",
        "Agree or disagree? Tell me in the comments.",
        "Full episode on the creator's channel.",
        "The last 5 seconds are the point.",
        "Tag someone who needs to hear this.",
        "No fluff, just the part that matters.",
    ],
    "uz": [
        "Saqlab qo'ying, kerak bo'ladi.",
        "Siz qanday fikrdasiz? Izohlarda yozing.",
        "To'liq suhbat — muallif sahifasida.",
    ],
}

PLATFORM_LIMITS = {
    "tiktok": {"caption": 2200, "title": 0},
    "instagram": {"caption": 2200, "title": 0},
    "youtube": {"caption": 5000, "title": 100},
    "vk": {"caption": 2000, "title": 0},
    "x": {"caption": 280, "title": 0},
}


@dataclass
class CaptionSet:
    platform: str
    title: str
    caption: str
    hashtags: list[str]


def _rotate(items: list[str], offset: int, count: int) -> list[str]:
    if not items:
        return []
    out = []
    for i in range(min(count, len(items))):
        out.append(items[(offset + i) % len(items)])
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def build_captions(campaign: Campaign, cand: Candidate, hook: str, variant: int) -> list[CaptionSet]:
    lang = campaign.lang if campaign.lang in SECOND_LINES else "en"
    second = cand.caption_line or _rotate(SECOND_LINES[lang], variant, 1)[0]
    if not cand.caption_line:
        sents = _sentences(cand.text)
        if len(sents) >= 2 and 20 <= len(sents[1]) <= 140:
            second = sents[1] + " " + second
    base_tags = list(campaign.hashtags)
    niche = _rotate(campaign.niche_hashtags, variant, 3)
    mentions = " ".join(campaign.mentions).strip()
    disclosure = campaign.disclosure.strip()
    tail = campaign.caption_tail.strip()

    result: list[CaptionSet] = []
    for platform in campaign.platforms:
        tags = []
        for t in base_tags + niche:
            t = t if t.startswith("#") else "#" + t
            if t not in tags:
                tags.append(t)
        if platform == "youtube":
            tags = tags[:5]
        if disclosure and disclosure not in tags:
            tags.append(disclosure)
        lines = [hook.strip()]
        if second:
            lines.append(second)
        if mentions:
            lines.append(mentions)
        if tail:
            lines.append(tail)
        lines.append(" ".join(tags))
        caption = "\n\n".join(l for l in lines if l)
        limits = PLATFORM_LIMITS.get(platform, PLATFORM_LIMITS["tiktok"])
        if limits["caption"] and len(caption) > limits["caption"]:
            caption = caption[: limits["caption"] - 1].rstrip() + "…"
        title = hook.strip()
        if limits["title"] and len(title) > limits["title"]:
            title = title[: limits["title"] - 1].rstrip() + "…"
        if platform == "youtube" and disclosure and disclosure not in title and len(title) + len(disclosure) + 1 <= 100:
            title = f"{title} {disclosure}"
        result.append(CaptionSet(platform=platform, title=title, caption=caption, hashtags=tags))
    return result


def checklist_markdown(campaign: Campaign, cand: Candidate, hook: str) -> str:
    lines = [f"# Чек-лист перед публикацией — {campaign.name}", ""]
    lines.append(f"Клип: {cand.start:.1f}–{cand.end:.1f} с ({cand.duration:.0f} с). Хук: {hook}")
    if campaign.url:
        lines.append(f"Карточка кампании: {campaign.url}")
    lines.append(f"Ставка: {campaign.rate_per_1k} $ за 1000, порог {campaign.threshold_views}, потолок "
                 f"{campaign.cap_per_clip_usd or 'нет'} $, дедлайн {campaign.deadline or 'не указан'}")
    lines.append("")
    lines.append("Правила кампании:")
    for r in campaign.rules:
        lines.append(f"- [ ] {r}")
    lines.append("")
    lines.append("Общие правила конвейера:")
    lines += [
        "- [ ] Подпись уникальна для этой площадки (не копия с другого аккаунта)",
        "- [ ] Пометка рекламы стоит, если кампания её требует",
        "- [ ] Ссылка на публикацию отправлена в кампанию сразу после выхода",
        "- [ ] Публикация остаётся открытой до выплаты",
        "- [ ] Ссылка записана в трекер: python -m pipeline post --clip-id <id> --account <акк> --platform <площадка> --url <ссылка>",
    ]
    return "\n".join(lines) + "\n"


def write_sidecars(out_base: Path, campaign: Campaign, cand: Candidate, hook: str, captions: list[CaptionSet],
                   clip_id: int, source_id: int) -> tuple[Path, Path, Path]:
    """Три файла рядом с клипом: .json (всё), .txt (для копирования с телефона), .checklist.md"""
    data = {
        "clip_id": clip_id,
        "source_id": source_id,
        "campaign_id": campaign.id,
        "start": cand.start,
        "end": cand.end,
        "duration": round(cand.duration, 2),
        "hook": hook,
        "score": cand.score,
        "llm_score": cand.llm_score,
        "transcript_excerpt": cand.text,
        "captions": [asdict(c) for c in captions],
    }
    json_path = out_base.with_suffix(".json")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    txt_lines = [f"CLIP {clip_id} · {campaign.name} · {cand.duration:.0f} с", ""]
    for c in captions:
        txt_lines.append(f"=== {c.platform.upper()} ===")
        if c.title:
            txt_lines.append(f"Заголовок: {c.title}")
        txt_lines.append(c.caption)
        txt_lines.append("")
    txt_path = out_base.with_suffix(".txt")
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")
    md_path = out_base.with_name(out_base.name + ".checklist.md")
    md_path.write_text(checklist_markdown(campaign, cand, hook), encoding="utf-8")
    return json_path, txt_path, md_path

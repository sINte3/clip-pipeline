"""Недельный отчёт: те самые две цифры (доля роликов выше порога и средний засчитанный охват),
начисления по кампаниям и топ клипов. Выход — markdown, который вставляется в документ плана.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .campaign import Campaign
from .tracker import Tracker


def _fmt_money(x: float) -> str:
    return f"{_n(x)} $"


def _n(x: float) -> str:
    """Число с пробелами между разрядами: 1 234 567."""
    return f"{x:,.0f}".replace(",", " ")


def build_report(tracker: Tracker, campaigns: dict[str, Campaign], since: Optional[str] = None) -> str:
    posts = tracker.latest_post_stats(since)
    lines: list[str] = []
    period = f"с {since}" if since else "за всё время"
    lines.append(f"# Отчёт по клиппингу {period} — {datetime.now().strftime('%Y-%m-%d')}")
    lines.append("")
    if not posts:
        lines.append("Публикаций нет. Залогируйте их командой `python -m pipeline post ...` и добавьте просмотры через `stats`.")
        return "\n".join(lines) + "\n"

    live = [p for p in posts if p.status == "live"]
    removed = [p for p in posts if p.status != "live"]

    def threshold_for(campaign_id: str) -> int:
        c = campaigns.get(campaign_id)
        return c.threshold_views if c else 1000

    counted = [p for p in live if p.views >= threshold_for(p.campaign_id)]
    share = len(counted) / len(posts) if posts else 0.0
    mean_counted = (sum(p.views for p in counted) / len(counted)) if counted else 0.0
    total_counted = sum(p.views for p in counted)
    median_all = sorted(p.views for p in posts)[len(posts) // 2] if posts else 0

    lines.append("## Две главные цифры")
    lines.append("")
    lines.append(f"- Роликов опубликовано: {len(posts)} (живых {len(live)}, снято или отклонено {len(removed)})")
    lines.append(f"- Доля роликов выше порога: {share:.0%} ({len(counted)} из {len(posts)}) — цель ворот G2: 35 %")
    lines.append(f"- Средний охват засчитанного ролика: {_n(mean_counted)} просмотров; медиана по всем роликам: {_n(median_all)}")
    lines.append(f"- Засчитанных просмотров всего: {_n(total_counted)} — цель ворот G2: 200 000")
    lines.append("")

    lines.append("## По кампаниям")
    lines.append("")
    by_campaign: dict[str, list] = defaultdict(list)
    for p in live:
        by_campaign[p.campaign_id].append(p)
    est_total = 0.0
    for cid, ps in sorted(by_campaign.items()):
        c = campaigns.get(cid)
        thr = c.threshold_views if c else 1000
        rate = c.rate_per_1k if c else 0.0
        cap = c.cap_per_clip_usd if c else 0.0
        cnt = [p for p in ps if p.views >= thr]
        est = 0.0
        for p in cnt:
            amount = p.views / 1000.0 * rate
            if cap:
                amount = min(amount, cap)
            est += amount
        est_total += est
        name = c.name if c else cid
        lines.append(f"- {name}: {len(ps)} роликов, выше порога {len(cnt)}, засчитано {_n(sum(p.views for p in cnt))} просмотров, "
                     f"оценка начисления {_fmt_money(est)} при ставке {rate} $")
    lines.append("")
    lines.append(f"Оценка начислений всего: {_fmt_money(est_total)} (до комиссии площадки; реальная сумма — только в кабинете кампании).")
    lines.append("")

    lines.append("## По аккаунтам")
    lines.append("")
    by_acc: dict[str, list] = defaultdict(list)
    for p in posts:
        by_acc[p.account].append(p)
    for acc, ps in sorted(by_acc.items()):
        cnt = [p for p in ps if p.views >= threshold_for(p.campaign_id)]
        mean_v = (sum(p.views for p in ps) / len(ps)) if ps else 0
        lines.append(f"- {acc}: {len(ps)} роликов, выше порога {len(cnt)} ({(len(cnt) / len(ps)):.0%}), средний охват {_n(mean_v)}")
    lines.append("")

    lines.append("## Топ-5 роликов")
    lines.append("")
    for p in sorted(posts, key=lambda x: x.views, reverse=True)[:5]:
        lines.append(f"- {_n(p.views)} просмотров — {p.hook or 'без хука'} ({p.account}, {p.platform}) {p.url}")
    lines.append("")

    pays = tracker.payouts(since)
    lines.append("## Выплаты")
    lines.append("")
    if pays:
        for r in pays:
            lines.append(f"- {r['received_at'][:10]}: {_fmt_money(r['amount_usd'])} по кампании {r['campaign_id']} ({r['method'] or 'способ не указан'})")
    else:
        lines.append("- Полученных выплат пока нет (ворота G3 не пройдены).")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_report(text: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"report_{datetime.now().strftime('%Y-%m-%d')}.md"
    p.write_text(text, encoding="utf-8")
    return p

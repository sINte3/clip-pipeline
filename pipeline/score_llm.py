"""Оценка кандидатов языковой моделью и ручной отбор через JSON.

Два пути:
  1. Автоматический — если задан ANTHROPIC_API_KEY и установлен пакет anthropic:
     модель ставит оценку 1–10, пишет хук (до 7 слов) и первую строку подписи.
  2. Ручной — `candidates` выгружает candidates.json; человек или Claude в сессии правит
     поля chosen/hook/caption_line; `render --selection` читает результат.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .segment import Candidate


SYSTEM_PROMPT = (
    "You are a short-form video editor who picks viral-worthy clips from podcast and interview transcripts. "
    "Rate each candidate 1-10 for its chance to stop a scroller in the first 3 seconds and hold attention to the end. "
    "Prefer: a complete thought, a bold claim, a number, a story with a turn, strong emotion. "
    "Penalize: context-dependent fragments, slow starts, filler. Always answer with JSON only."
)


def _build_user_prompt(cands: list[Candidate], lang: str, niche: str, max_chars: int = 700) -> str:
    lang_name = {"ru": "Russian", "en": "English", "uz": "Uzbek"}.get(lang, "English")
    items = []
    for c in cands:
        snippet = c.text if len(c.text) <= max_chars else c.text[:max_chars] + "…"
        items.append({"id": c.id, "duration_s": round(c.duration, 1), "text": snippet})
    return (
        f"Account niche: {niche or 'business, money, technology'}. Clip language: {lang_name}.\n"
        f"For each candidate return an object {{\"id\", \"score\" (1-10), \"hook\" (<=7 words, {lang_name}, no quotes, "
        f"no clickbait lies, may end with ? or !), \"caption_line\" (one sentence for the caption, {lang_name})}}.\n"
        f"Return a JSON array only.\n\nCandidates:\n{json.dumps(items, ensure_ascii=False)}"
    )


def _extract_json_array(text: str) -> list[dict]:
    text = text.strip()
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not m:
        raise ValueError("в ответе модели нет JSON-массива")
    return json.loads(m.group(0))


def llm_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def score_with_llm(cands: list[Candidate], lang: str, niche: str, model: str, batch: int = 20) -> list[Candidate]:
    """Заполняет llm_score, llm_hook, caption_line у кандидатов. Ошибки сети не роняют конвейер."""
    if not llm_available() or not cands:
        return cands
    import anthropic  # type: ignore

    client = anthropic.Anthropic()
    by_id = {c.id: c for c in cands}
    for i in range(0, len(cands), batch):
        chunk = cands[i:i + batch]
        try:
            msg = client.messages.create(
                model=model, max_tokens=4000, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_prompt(chunk, lang, niche)}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content)
            rows = _extract_json_array(text)
        except Exception as e:  # сеть, лимиты, формат — продолжаем без оценки этой пачки
            print(f"[score_llm] пачка {i // batch + 1}: пропущена ({e})")
            continue
        for row in rows:
            try:
                c = by_id[int(row["id"])]
            except (KeyError, ValueError, TypeError):
                continue
            try:
                c.llm_score = float(row.get("score", 0))
            except (TypeError, ValueError):
                c.llm_score = None
            hook = str(row.get("hook", "")).strip().strip('"')
            if hook:
                c.llm_hook = hook
            line = str(row.get("caption_line", "")).strip()
            if line:
                c.caption_line = line
    return cands


def export_candidates(cands: list[Candidate], path: Path, source_id: int, campaign_id: str) -> Path:
    """Файл для ручного отбора: поставьте chosen=true и при желании поправьте hook/caption_line."""
    payload = {
        "source_id": source_id,
        "campaign_id": campaign_id,
        "how_to": "Поставьте chosen: true у нужных кандидатов, поправьте hook и caption_line, затем: python -m pipeline render --selection <этот файл>",
        "candidates": [
            {**c.to_dict(), "chosen": False, "hook": c.llm_hook or c.hook, "caption_line": c.caption_line or ""}
            for c in cands
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_selection(path: Path) -> tuple[int, str, list[Candidate]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    chosen: list[Candidate] = []
    for row in raw["candidates"]:
        if not row.get("chosen"):
            continue
        c = Candidate(id=int(row["id"]), start=float(row["start"]), end=float(row["end"]), text=row.get("text", ""),
                      hook=row.get("hook") or "", score=float(row.get("score", 0)), features=row.get("features", {}))
        c.llm_hook = row.get("hook") or None
        c.caption_line = row.get("caption_line") or None
        chosen.append(c)
    return int(raw["source_id"]), str(raw["campaign_id"]), chosen


def best_hook(c: Candidate) -> str:
    return (c.llm_hook or c.hook or "").strip()


def default_niche(lang: str) -> str:
    return "ИИ, технологии и деньги" if lang == "ru" else "money, business and technology"


def summary_line(c: Candidate) -> str:
    llm = f", модель {c.llm_score:.0f}/10" if c.llm_score is not None else ""
    return f"#{c.id} {c.start:7.1f}–{c.end:7.1f} ({c.duration:4.0f} с) рейтинг {c.score:.2f}{llm} — {best_hook(c)}"


def ensure_hooks(cands: list[Candidate], lang: str) -> None:
    from .segment import make_hook
    for c in cands:
        if not best_hook(c):
            c.hook = make_hook(c.text.split(".")[0], lang)


__all__ = ["score_with_llm", "export_candidates", "load_selection", "best_hook", "default_niche",
           "summary_line", "ensure_hooks", "llm_available"]

_ = Optional  # noqa: F841 (подсказка типов для читателей)

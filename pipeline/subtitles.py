"""Сборка ASS-субтитров из слов: короткие строки по 2–4 слова, крупный жирный шрифт с обводкой.

Времена в файле — относительно начала клипа. Шрифт берётся из настроек: libass ищет его по имени
семейства в каталоге fontsdir, который передаётся фильтру subtitles в render.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from .transcribe import Word


def font_family_from_path(font_path: str | None) -> str:
    name = (font_path or "").lower()
    if "arialbd" in name or "arial" in name:
        return "Arial"
    if "segoe" in name:
        return "Segoe UI"
    if "calibri" in name:
        return "Calibri"
    if "dejavu" in name:
        return "DejaVu Sans"
    if "liberation" in name:
        return "Liberation Sans"
    return "Arial"


def _fmt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def group_words(words: list[Word], clip_start: float, max_words: int = 3, max_line_s: float = 1.6,
                min_line_s: float = 0.35) -> list[tuple[float, float, str]]:
    """Группирует слова в строки: до max_words слов, не дольше max_line_s, разрыв на знаках препинания."""
    lines: list[tuple[float, float, str]] = []
    buf: list[Word] = []

    def flush() -> None:
        if not buf:
            return
        start = buf[0].start - clip_start
        end = buf[-1].end - clip_start
        if end - start < min_line_s:
            end = start + min_line_s
        lines.append((start, end, " ".join(w.text for w in buf)))
        buf.clear()

    for w in words:
        buf.append(w)
        dur = w.end - buf[0].start
        ends_punct = w.text.endswith((".", "!", "?", ",", ";", ":", "…"))
        if len(buf) >= max_words or dur >= max_line_s or ends_punct:
            flush()
    flush()
    # Убираем мерцание: строка держится до начала следующей, если пауза короче 0.3 с
    fixed: list[tuple[float, float, str]] = []
    for i, (s, e, t) in enumerate(lines):
        if i + 1 < len(lines):
            ns = lines[i + 1][0]
            if 0 < ns - e < 0.3:
                e = ns
            if e > ns:
                e = ns
        fixed.append((s, e, t))
    return fixed


def build_ass(words: list[Word], clip_start: float, clip_end: float, font_family: str,
              font_size: int = 64, margin_v: int = 560, play_w: int = 1080, play_h: int = 1920,
              uppercase: bool = False) -> str:
    inside = [w for w in words if w.end > clip_start and w.start < clip_end]
    lines = group_words(inside, clip_start)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_family},{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    clip_len = clip_end - clip_start
    for s, e, t in lines:
        s = max(0.0, min(s, clip_len))
        e = max(s + 0.2, min(e, clip_len))
        text = _escape(t.upper() if uppercase else t)
        text = re.sub(r"\s+", " ", text).strip()
        events.append(f"Dialogue: 0,{_fmt_time(s)},{_fmt_time(e)},Default,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


def write_ass(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path

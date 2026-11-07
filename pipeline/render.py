"""Рендер клипа ffmpeg: кадрирование 9:16 (по лицу или с размытым фоном), субтитры, хук,
баннер кампании, финальная плашка «Подпишись», нормализация громкости.

Все вспомогательные файлы (subs.ass, hook.txt, subscribe.txt) лежат в рабочем каталоге клипа,
а ffmpeg запускается из него — так пути в фильтрах остаются короткими и не требуют экранирования
(особенно важно на Windows, где двоеточие в C:\\ ломает синтаксис фильтров).
"""

from __future__ import annotations

import os
import platform
import statistics
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .campaign import Campaign
from .config import Settings
from .segment import Candidate
from .subtitles import build_ass, font_family_from_path, write_ass
from .transcribe import Transcript


@dataclass
class RenderResult:
    path: Path
    duration_s: float
    face_center_x: Optional[float]
    command: str


def _ff_escape_path(p: str) -> str:
    """Путь для использования внутри filter_complex: прямые слэши, экранированное двоеточие."""
    p = p.replace("\\", "/")
    return p.replace(":", "\\:")


def detect_face_center(video: Path, start: float, end: float, samples: int = 8) -> Optional[float]:
    """Нормированная (0..1) горизонтальная координата самого крупного лица; None, если лиц нет."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    bundled = Path(__file__).resolve().parent / "assets" / "haarcascade_frontalface_default.xml"
    candidates = [bundled]
    try:
        candidates.append(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    except Exception:
        pass
    cascade = None
    for cp in candidates:
        if cp.exists():
            cc = cv2.CascadeClassifier(str(cp))
            if not cc.empty():
                cascade = cc
                break
    if cascade is None:
        return None
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    centers: list[float] = []
    try:
        width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
        if width <= 0:
            return None
        span = max(0.1, end - start)
        for k in range(samples):
            t = start + span * (k + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48))
            if len(faces) == 0:
                continue
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            centers.append((x + w / 2.0) / width)
    finally:
        cap.release()
    if len(centers) < max(2, samples // 3):
        return None
    return float(statistics.median(centers))


def _frame_chain(mode: str, src_w: int, src_h: int, out_w: int, out_h: int, cx: Optional[float]) -> str:
    """Цепочка фильтров, приводящая кадр к out_w×out_h."""
    target_ratio = out_w / out_h
    if mode == "blur":
        return (
            f"split[bg][fg];[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},boxblur=24:4[bgb];[fg]scale={out_w}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
    if src_w <= 0 or src_h <= 0:
        return f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        crop_w = int(round(src_h * target_ratio)) // 2 * 2
        crop_h = src_h // 2 * 2
        center = cx if cx is not None else 0.5
        x = int(round(center * src_w - crop_w / 2))
        x = max(0, min(src_w - crop_w, x))
        return f"crop={crop_w}:{crop_h}:{x}:0,scale={out_w}:{out_h}:flags=lanczos"
    # Исходник уже вертикальный или квадратный — вписываем по ширине и подрезаем/дополняем по высоте
    return f"scale={out_w}:-2,crop={out_w}:'min(ih,{out_h})',pad={out_w}:{out_h}:0:(oh-ih)/2:color=black"


def _wrap_hook(text: str, width: int = 22) -> str:
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False)) or text


def _overlay_xy(position: str, margin: int) -> tuple[str, str]:
    table = {
        "top-left": (f"{margin}", f"{margin}"),
        "top-right": (f"W-w-{margin}", f"{margin}"),
        "bottom-left": (f"{margin}", f"H-h-{margin}"),
        "bottom-right": (f"W-w-{margin}", f"H-h-{margin}"),
        "top-center": ("(W-w)/2", f"{margin}"),
        "bottom-center": ("(W-w)/2", f"H-h-{margin}"),
    }
    return table.get(position, table["top-right"])


def render_clip(settings: Settings, campaign: Campaign, source_path: Path, src_w: int, src_h: int,
                transcript: Transcript, cand: Candidate, hook_text: str, workdir: Path, out_path: Path,
                banner_path: Optional[Path] = None) -> RenderResult:
    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = cand.end - cand.start
    out_w, out_h = settings.out_width, settings.out_height

    cx = None
    if campaign.frame_mode == "crop" and src_w > src_h:
        cx = detect_face_center(source_path, cand.start, cand.end)

    chain = [f"[0:v]{_frame_chain(campaign.frame_mode, src_w, src_h, out_w, out_h, cx)},setsar=1,fps={settings.fps}[base]"]
    last = "base"
    idx = 0

    font_dir = _ff_escape_path(str(Path(settings.font_bold).parent)) if settings.font_bold else ""
    font_file = _ff_escape_path(settings.font_bold) if settings.font_bold else ""

    if campaign.subtitles and transcript.words:
        ass = build_ass(transcript.words, cand.start, cand.end, font_family_from_path(settings.font_bold),
                        font_size=settings.subtitle_font_size, margin_v=settings.subtitle_margin_v,
                        play_w=out_w, play_h=out_h)
        write_ass(workdir / "subs.ass", ass)
        fonts_opt = f":fontsdir='{font_dir}'" if font_dir else ""
        chain.append(f"[{last}]subtitles=filename=subs.ass{fonts_opt}[s{idx}]")
        last = f"s{idx}"
        idx += 1

    if campaign.hook_overlay and hook_text and font_file:
        (workdir / "hook.txt").write_text(_wrap_hook(hook_text), encoding="utf-8", newline="\n")
        chain.append(
            f"[{last}]drawtext=fontfile='{font_file}':textfile=hook.txt:fontsize={settings.hook_font_size}:"
            f"fontcolor=white:line_spacing=6:box=1:boxcolor=black@0.6:boxborderw=20:"
            f"x=(w-text_w)/2:y={settings.hook_y}:enable='between(t,0,{settings.hook_seconds:.2f})'[s{idx}]"
        )
        last = f"s{idx}"
        idx += 1

    # -ss и -t обязательно ДО -i: так они относятся к исходнику. Если поставить -t после -i,
    # при наличии баннера ffmpeg применит его ко второму входу, и клип растянется до конца исходника.
    inputs = ["-ss", f"{cand.start:.3f}", "-t", f"{duration:.3f}", "-i", str(source_path)]
    if banner_path and campaign.banner:
        inputs += ["-i", str(banner_path)]
        b = campaign.banner
        bw = int(out_w * b.width_frac)
        x, y = _overlay_xy(b.position, b.margin)
        end = b.end if b.end and b.end > 0 else duration
        chain.append(f"[1:v]scale={bw}:-1[bn]")
        chain.append(f"[{last}][bn]overlay={x}:{y}:enable='between(t,{b.start:.2f},{end:.2f})'[s{idx}]")
        last = f"s{idx}"
        idx += 1

    if campaign.subscribe_card and font_file:
        text = campaign.subscribe_text or (settings.subscribe_text_ru if campaign.lang == "ru" else settings.subscribe_text_en)
        (workdir / "subscribe.txt").write_text(text, encoding="utf-8", newline="\n")
        start_at = max(0.0, duration - settings.subscribe_last_seconds)
        chain.append(
            f"[{last}]drawtext=fontfile='{font_file}':textfile=subscribe.txt:fontsize=56:fontcolor=black:"
            f"box=1:boxcolor=white@0.9:boxborderw=22:x=(w-text_w)/2:y={int(out_h * 0.78)}:"
            f"enable='gte(t,{start_at:.2f})'[s{idx}]"
        )
        last = f"s{idx}"
        idx += 1

    chain.append(f"[{last}]format=yuv420p[vout]")
    filter_complex = ";".join(chain)

    cmd = [settings.ffmpeg, "-v", "error", "-y", *inputs,
           "-filter_complex", filter_complex, "-map", "[vout]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf), "-maxrate", "8M", "-bufsize", "16M",
           "-r", str(settings.fps), "-t", f"{duration:.3f}",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
           "-movflags", "+faststart", "-shortest", str(out_path)]
    proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg завершился с ошибкой:\n{proc.stderr}\nКоманда: {' '.join(cmd)}")
    return RenderResult(path=out_path, duration_s=duration, face_center_x=cx, command=" ".join(cmd))


def is_windows() -> bool:
    return platform.system() == "Windows"

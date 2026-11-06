"""Получение исходника: локальный файл или ссылка (YouTube, Twitch VOD, Kick, прямой URL) через yt-dlp.

Исходники кампаний скачиваются только те, которые кампания прикладывает или разрешает —
конвейер не предназначен для чужого контента без права на нарезку.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SourceFile:
    path: Path
    title: str
    duration_s: float
    width: int
    height: int
    origin: str


def _probe(ffprobe: str, path: Path) -> dict:
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def probe_video(ffprobe: str, path: Path) -> tuple[float, int, int]:
    info = _probe(ffprobe, path)
    duration = float(info.get("format", {}).get("duration", 0.0) or 0.0)
    width = height = 0
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            width, height = int(s.get("width", 0)), int(s.get("height", 0))
            break
    return duration, width, height


def _stable_name(origin: str) -> str:
    return hashlib.sha1(origin.encode("utf-8")).hexdigest()[:12]


def fetch_source(origin: str, sources_dir: Path, ffprobe: str = "ffprobe",
                 max_height: int = 1080) -> SourceFile:
    """Возвращает локальный файл исходника. Ссылки скачиваются через yt-dlp в sources_dir."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    local = Path(origin)
    if local.exists():
        duration, w, h = probe_video(ffprobe, local)
        return SourceFile(path=local.resolve(), title=local.stem, duration_s=duration, width=w, height=h, origin=origin)

    if not (origin.startswith("http://") or origin.startswith("https://")):
        raise FileNotFoundError(f"исходник не найден: {origin}")

    try:
        import yt_dlp  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("для скачивания по ссылке нужен yt-dlp: pip install yt-dlp") from e

    base = sources_dir / _stable_name(origin)
    final = base.with_suffix(".mp4")
    meta_file = base.with_suffix(".info.json")
    if final.exists() and meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        duration, w, h = probe_video(ffprobe, final)
        return SourceFile(path=final, title=meta.get("title", final.stem), duration_s=duration, width=w, height=h, origin=origin)

    ydl_opts = {
        "format": f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/b[height<={max_height}][ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(base) + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(origin, download=True)
    title = info.get("title") or final.stem
    # yt-dlp может сохранить под другим расширением, если mp4 недоступен
    produced: Optional[Path] = None
    for cand in sources_dir.glob(base.name + ".*"):
        if cand.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            produced = cand
            break
    if produced is None:
        raise RuntimeError("yt-dlp не сохранил видеофайл")
    if produced.suffix.lower() != ".mp4":
        shutil.move(str(produced), str(final.with_suffix(produced.suffix)))
        final = final.with_suffix(produced.suffix)
    meta_file.write_text(json.dumps({"title": title, "origin": origin}, ensure_ascii=False), encoding="utf-8")
    duration, w, h = probe_video(ffprobe, final)
    return SourceFile(path=final, title=title, duration_s=duration, width=w, height=h, origin=origin)

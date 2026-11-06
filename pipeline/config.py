"""Настройки конвейера.

Порядок приоритета: аргументы команды > переменные окружения CLIP_* > файл pipeline.json
в корне проекта > значения по умолчанию ниже. Файл создаётся командой `python -m pipeline init`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "pipeline.json"


def _first_existing(paths: list[str]) -> Optional[str]:
    for p in paths:
        if p and Path(p).exists():
            return p
    return None


def default_font_bold() -> Optional[str]:
    """Жирный шрифт с кириллицей и латиницей: Windows → Arial Bold, Linux → DejaVu Sans Bold."""
    candidates = []
    if platform.system() == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates += [
            os.path.join(windir, "Fonts", "arialbd.ttf"),
            os.path.join(windir, "Fonts", "segoeuib.ttf"),
            os.path.join(windir, "Fonts", "calibrib.ttf"),
        ]
    elif platform.system() == "Darwin":
        candidates += [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    return _first_existing(candidates)


@dataclass
class Settings:
    # Каталоги
    data_dir: str = str(PROJECT_ROOT / "data")            # исходники, расшифровки, рабочие файлы
    out_dir: str = str(PROJECT_ROOT / "out")              # готовые клипы «к публикации»
    campaigns_dir: str = str(PROJECT_ROOT / "campaigns")  # спецификации кампаний
    db_path: str = str(PROJECT_ROOT / "data" / "tracker.sqlite3")

    # Инструменты
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    font_bold: Optional[str] = field(default_factory=default_font_bold)

    # Распознавание речи: "faster" (faster-whisper, модель скачивается с Hugging Face)
    # или "sherpa" (sherpa-onnx, модель лежит локально в sherpa_model_dir)
    whisper_backend: str = "faster"
    whisper_model: str = "small"           # tiny | base | small | medium | large-v3 | large-v3-turbo
    whisper_compute: str = "int8"          # int8 на процессоре; float16 только на GPU с CUDA
    whisper_threads: int = max(1, (os.cpu_count() or 4) - 2)
    sherpa_model_dir: str = ""             # каталог с small-encoder.int8.onnx, small-decoder.int8.onnx, small-tokens.txt

    # Языковая модель для оценки фрагментов и хуков (необязательно)
    llm_model: str = "claude-haiku-5-5"
    llm_enabled: bool = True               # используется только если задан ANTHROPIC_API_KEY

    # Выходной формат
    out_width: int = 1080
    out_height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "veryfast"

    # Оформление по умолчанию (переопределяется кампанией)
    subtitle_font_size: int = 64
    subtitle_margin_v: int = 560           # отступ снизу в пикселях при высоте 1920
    hook_font_size: int = 70
    hook_seconds: float = 4.0
    hook_y: int = 300
    subscribe_text_ru: str = "Подпишись"
    subscribe_text_en: str = "Follow for more"
    subscribe_last_seconds: float = 2.0

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.out_dir, self.campaigns_dir, Path(self.db_path).parent):
            Path(d).mkdir(parents=True, exist_ok=True)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def load_settings(overrides: Optional[dict] = None) -> Settings:
    """Собирает настройки: умолчания → pipeline.json → переменные окружения → overrides."""
    values: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            values.update(json.load(f))
    env_map = {
        "CLIP_DATA_DIR": "data_dir",
        "CLIP_OUT_DIR": "out_dir",
        "CLIP_FFMPEG": "ffmpeg",
        "CLIP_FFPROBE": "ffprobe",
        "CLIP_FONT_BOLD": "font_bold",
        "CLIP_WHISPER_BACKEND": "whisper_backend",
        "CLIP_WHISPER_MODEL": "whisper_model",
        "CLIP_SHERPA_MODEL_DIR": "sherpa_model_dir",
        "CLIP_LLM_MODEL": "llm_model",
    }
    for env_key, attr in env_map.items():
        if os.environ.get(env_key):
            values[attr] = os.environ[env_key]
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})
    settings = Settings(**{k: v for k, v in values.items() if k in Settings.__dataclass_fields__})
    return settings


def write_default_config() -> Path:
    settings = Settings()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(settings.to_json())
    settings.ensure_dirs()
    return CONFIG_FILE


def check_tools(settings: Settings) -> list[tuple[str, bool, str]]:
    """Проверка окружения для команды doctor: (название, ок?, подробность)."""
    results: list[tuple[str, bool, str]] = []
    ff = shutil.which(settings.ffmpeg)
    results.append(("ffmpeg", ff is not None, ff or "не найден в PATH — установите ffmpeg (Windows: winget install Gyan.FFmpeg)"))
    fp = shutil.which(settings.ffprobe)
    results.append(("ffprobe", fp is not None, fp or "не найден в PATH"))
    results.append(("шрифт", bool(settings.font_bold), settings.font_bold or "жирный TTF-шрифт не найден — укажите font_bold в pipeline.json"))
    try:
        import faster_whisper  # noqa: F401
        results.append(("faster-whisper", True, "установлен"))
    except Exception as e:  # pragma: no cover
        results.append(("faster-whisper", False, f"не установлен: {e}"))
    try:
        import sherpa_onnx  # noqa: F401
        ok = bool(settings.sherpa_model_dir) and Path(settings.sherpa_model_dir).exists()
        results.append(("sherpa-onnx", True, "установлен; модель " + ("найдена" if ok else "не указана (нужна только для backend=sherpa)")))
    except Exception:
        results.append(("sherpa-onnx", True, "не установлен (нужен только для backend=sherpa)"))
    try:
        import cv2  # noqa: F401
        results.append(("opencv", True, "установлен (кадрирование по лицу)"))
    except Exception as e:
        results.append(("opencv", False, f"не установлен: {e}"))
    try:
        import yt_dlp  # noqa: F401
        results.append(("yt-dlp", True, "установлен"))
    except Exception as e:
        results.append(("yt-dlp", False, f"не установлен: {e}"))
    key = os.environ.get("ANTHROPIC_API_KEY")
    results.append(("ANTHROPIC_API_KEY", bool(key), "задан — хуки и оценку делает языковая модель" if key else "не задан — работают эвристики, хуки правятся вручную"))
    return results

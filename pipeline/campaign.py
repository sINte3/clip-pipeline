"""Спецификация кампании: один JSON-файл в campaigns/ на каждую кампанию биржи.

Пример — campaigns/example_en.json. Обязательные поля: id, name, lang.
Всё остальное имеет значения по умолчанию, чтобы файл можно было завести за минуту.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


VALID_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "top-center", "bottom-center"}
VALID_PLATFORMS = {"tiktok", "instagram", "youtube", "vk", "x"}


@dataclass
class Banner:
    path: str                       # PNG с прозрачностью, относительно корня проекта или абсолютный путь
    position: str = "top-right"
    width_frac: float = 0.28        # доля ширины кадра
    margin: int = 40                # отступ от краёв в пикселях (при 1080×1920)
    start: float = 1.0              # секунда появления
    end: float = 0.0                # секунда исчезновения; 0 = до конца клипа

    def validate(self) -> None:
        if self.position not in VALID_POSITIONS:
            raise ValueError(f"banner.position должен быть одним из {sorted(VALID_POSITIONS)}")
        if not (0.05 <= self.width_frac <= 0.6):
            raise ValueError("banner.width_frac должен быть в пределах 0.05–0.6")


@dataclass
class Campaign:
    id: str
    name: str
    lang: str                                   # ru | en | uz — язык речи в исходнике и подписей
    exchange: str = ""                          # whop | darebay | clipping.net | ...
    url: str = ""                               # ссылка на карточку кампании
    rate_per_1k: float = 0.0                    # ставка, $ за 1000 просмотров
    threshold_views: int = 1000                 # порог, после которого просмотры засчитываются
    cap_per_clip_usd: float = 0.0               # потолок выплаты за ролик, 0 = нет
    budget_usd: float = 0.0
    deadline: str = ""                          # ISO-дата окончания
    platforms: list[str] = field(default_factory=lambda: ["tiktok", "instagram", "youtube"])
    sources: list[str] = field(default_factory=list)   # ссылки или пути к исходникам кампании
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)  # @аккаунты, которые требует бренд
    disclosure: str = ""                        # "#ad", "#реклама", "" если не требуется
    caption_tail: str = ""                      # фиксированная строка в конце подписи (ссылка, промокод)
    rules: list[str] = field(default_factory=list)      # правила карточки — попадают в чек-лист
    banner: Optional[Banner] = None
    clip_min_s: float = 20.0
    clip_max_s: float = 60.0
    clips_per_source: int = 10
    frame_mode: str = "crop"                    # crop (кадрирование по лицу) | blur (вписать с размытым фоном)
    subtitles: bool = True
    hook_overlay: bool = True
    subscribe_card: bool = True
    subscribe_text: str = ""                    # пусто = по языку из настроек
    niche_hashtags: list[str] = field(default_factory=list)  # хештеги темы аккаунта
    # Исправления распознавания: {"Boxable": "BOXABL", "Box Able": "BOXABL"} — ключ из 1–2 слов,
    # регистр не важен; применяются к субтитрам, хукам и тексту кандидатов
    word_fixes: dict[str, str] = field(default_factory=dict)
    # Фрагменты, где встречается любое из этих слов/фраз, не берутся в клипы (запрещённые темы бренда)
    exclude_keywords: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.id or not self.name:
            raise ValueError("campaign.id и campaign.name обязательны")
        if self.lang not in {"ru", "en", "uz"}:
            raise ValueError("campaign.lang должен быть ru, en или uz")
        bad = [p for p in self.platforms if p not in VALID_PLATFORMS]
        if bad:
            raise ValueError(f"неизвестные площадки: {bad}; допустимы {sorted(VALID_PLATFORMS)}")
        if self.clip_min_s >= self.clip_max_s:
            raise ValueError("clip_min_s должен быть меньше clip_max_s")
        if self.frame_mode not in {"crop", "blur"}:
            raise ValueError("frame_mode: crop или blur")
        if self.banner:
            self.banner.validate()

    @staticmethod
    def load(path: str | Path) -> "Campaign":
        p = Path(path)
        with open(p, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        banner_raw = raw.pop("banner", None)
        known = {k: v for k, v in raw.items() if k in Campaign.__dataclass_fields__}
        unknown = set(raw) - set(known)
        if unknown:
            raise ValueError(f"{p.name}: неизвестные поля {sorted(unknown)}")
        c = Campaign(**known)
        if banner_raw:
            c.banner = Banner(**banner_raw)
        c.validate()
        return c

    def resolve_banner_path(self, project_root: Path) -> Optional[Path]:
        if not self.banner:
            return None
        bp = Path(self.banner.path)
        if not bp.is_absolute():
            bp = project_root / bp
        if not bp.exists():
            raise FileNotFoundError(f"баннер не найден: {bp}")
        return bp


EXAMPLE_EN = {
    "id": "example_en",
    "name": "Example Podcast Clipping (Whop)",
    "lang": "en",
    "exchange": "whop",
    "url": "https://whop.com/...",
    "rate_per_1k": 1.5,
    "threshold_views": 1000,
    "cap_per_clip_usd": 200,
    "budget_usd": 5000,
    "deadline": "2026-12-15",
    "platforms": ["tiktok", "instagram", "youtube"],
    "sources": ["https://www.youtube.com/watch?v=EXAMPLE"],
    "hashtags": ["#podcast", "#business"],
    "mentions": ["@examplepodcast"],
    "disclosure": "#ad",
    "caption_tail": "",
    "rules": [
        "Vertical 9:16 only, 15–60 s",
        "Unique caption on every post",
        "No reposts of the same clip on the same platform",
        "Keep posts public until the campaign is paid",
        "Use #ad in the caption"
    ],
    "banner": {
        "path": "banners/example_banner.png",
        "position": "top-right",
        "width_frac": 0.28,
        "margin": 40,
        "start": 1.0,
        "end": 0
    },
    "clip_min_s": 20,
    "clip_max_s": 60,
    "clips_per_source": 10,
    "frame_mode": "crop",
    "subtitles": True,
    "hook_overlay": True,
    "subscribe_card": True,
    "niche_hashtags": ["#entrepreneur", "#money", "#tech"]
}

EXAMPLE_RU = {
    "id": "example_ru",
    "name": "Пример задания DareBay (приложение)",
    "lang": "ru",
    "exchange": "darebay",
    "url": "https://darebay.com/...",
    "rate_per_1k": 2.0,
    "threshold_views": 1000,
    "cap_per_clip_usd": 500,
    "budget_usd": 1000,
    "deadline": "2026-12-01",
    "platforms": ["tiktok", "instagram", "youtube"],
    "sources": ["data/sources/example.mp4"],
    "hashtags": ["#приложение", "#ии"],
    "mentions": [],
    "disclosure": "#реклама",
    "caption_tail": "Ссылка в профиле",
    "rules": [
        "Только вертикальный формат",
        "Ролик остаётся открытым до конца задания",
        "Уникальная подпись на каждую публикацию",
        "Без накрутки просмотров"
    ],
    "banner": None,
    "clip_min_s": 15,
    "clip_max_s": 45,
    "clips_per_source": 10,
    "frame_mode": "crop",
    "subtitles": True,
    "hook_overlay": True,
    "subscribe_card": True,
    "niche_hashtags": ["#технологии", "#нейросети", "#бизнес"]
}


def write_examples(campaigns_dir: Path) -> list[Path]:
    campaigns_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for name, data in (("example_en.json", EXAMPLE_EN), ("example_ru.json", EXAMPLE_RU)):
        p = campaigns_dir / name
        if not p.exists():
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        out.append(p)
    return out

"""Командная строка конвейера.

    python -m pipeline init                       — создать pipeline.json, каталоги и примеры кампаний
    python -m pipeline doctor                     — проверить ffmpeg, шрифты, бэкенды
    python -m pipeline campaigns                  — список кампаний из campaigns/
    python -m pipeline ingest --campaign ID --source URL|FILE [--lang ru]
    python -m pipeline candidates --source-id N [--n 10] [--no-llm]
    python -m pipeline render --selection FILE.json | --source-id N [--n 5] [--ids 3,7,12]
    python -m pipeline clip --campaign ID --source URL|FILE [--n 5]   — ingest + candidates + render
    python -m pipeline post --clip-id N --account ru_tiktok --platform tiktok --url URL
    python -m pipeline stats --post-id N --views V [--likes L --comments C] | --import FILE.csv
    python -m pipeline payout --campaign ID --amount 42.5 [--method usdt-ton]
    python -m pipeline report [--since 2026-11-01]
    python -m pipeline export                     — выгрузить таблицы трекера в CSV
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .audio_features import energy_profile
from .campaign import Campaign, write_examples
from .captions import build_captions, write_sidecars
from .config import PROJECT_ROOT, Settings, check_tools, load_settings, write_default_config
from .download import fetch_source
from .render import render_clip
from .report import build_report, write_report
from .score_llm import (best_hook, default_niche, ensure_hooks, export_candidates, llm_available,
                        load_selection, score_with_llm, summary_line)
from .segment import Candidate, build_candidates, select_non_overlapping
from .tracker import Tracker
from .transcribe import Transcript, apply_word_fixes, extract_audio, transcribe


# ----------------------------------------------------------------------------- helpers

def _campaign_files(settings: Settings) -> list[Path]:
    return sorted(Path(settings.campaigns_dir).glob("*.json"))


def find_campaign(settings: Settings, ref: str) -> Campaign:
    p = Path(ref)
    if p.suffix == ".json" and p.exists():
        return Campaign.load(p)
    for f in _campaign_files(settings):
        try:
            c = Campaign.load(f)
        except Exception as e:
            print(f"[campaigns] {f.name}: пропущен ({e})")
            continue
        if c.id == ref or f.stem == ref:
            return c
    raise SystemExit(f"кампания '{ref}' не найдена в {settings.campaigns_dir}")


def load_all_campaigns(settings: Settings) -> dict[str, Campaign]:
    out: dict[str, Campaign] = {}
    for f in _campaign_files(settings):
        try:
            c = Campaign.load(f)
            out[c.id] = c
        except Exception:
            continue
    return out


def _file_key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}|{st.st_size}".encode("utf-8")).hexdigest()[:16]


def ensure_audio_and_transcript(settings: Settings, source_path: Path, lang: Optional[str]) -> tuple[Path, Path, Transcript]:
    data = Path(settings.data_dir)
    key = _file_key(source_path)
    wav = data / "audio" / f"{key}.wav"
    tr_path = data / "transcripts" / f"{key}.json"
    if not wav.exists():
        print(f"[audio] извлекаю звук → {wav.name}")
        extract_audio(settings.ffmpeg, source_path, wav)
    if tr_path.exists():
        tr = Transcript.load(tr_path)
        print(f"[transcript] из кэша: {len(tr.words)} слов, {tr.backend}")
    else:
        print(f"[transcript] распознаю речь ({settings.whisper_backend}/{settings.whisper_model}) — это может занять несколько минут")
        tr = transcribe(wav, settings, language=lang)
        tr_path.parent.mkdir(parents=True, exist_ok=True)
        tr.save(tr_path)
        print(f"[transcript] готово: {len(tr.words)} слов, язык {tr.language}")
    return wav, tr_path, tr


def compute_candidates(settings: Settings, campaign: Campaign, source_row, use_llm: bool) -> list[Candidate]:
    source_path = Path(source_row["path"])
    wav, _, tr = ensure_audio_and_transcript(settings, source_path, campaign.lang)
    tr = apply_word_fixes(tr, campaign.word_fixes)
    energy = energy_profile(wav)
    cands = build_candidates(tr, campaign.lang, energy, campaign.clip_min_s, campaign.clip_max_s)
    if campaign.exclude_keywords:
        banned = [k.lower() for k in campaign.exclude_keywords]
        before = len(cands)
        cands = [c for c in cands if not any(b in c.text.lower() for b in banned)]
        print(f"[filter] исключено по запрещённым темам: {before - len(cands)} из {before} окон")
    if not cands:
        raise SystemExit("кандидатов нет: расшифровка пуста, клипы длиннее исходника или всё отсеяно exclude_keywords")
    # Сначала грубый отбор лучших по эвристике (в 3 раза больше нужного), потом модель переранжирует
    shortlist = select_non_overlapping(cands, n=max(campaign.clips_per_source * 3, 15), max_overlap_frac=0.5)
    if use_llm and settings.llm_enabled and llm_available():
        print(f"[llm] оценка {len(shortlist)} кандидатов моделью {settings.llm_model}")
        shortlist = score_with_llm(shortlist, campaign.lang, default_niche(campaign.lang), settings.llm_model)
    ensure_hooks(shortlist, campaign.lang)
    return shortlist


def _out_dir_for(settings: Settings, campaign: Campaign) -> Path:
    return Path(settings.out_dir) / campaign.id / datetime.now().strftime("%Y-%m-%d")


def render_candidates(settings: Settings, campaign: Campaign, tracker: Tracker, source_row, chosen: list[Candidate]) -> list[tuple[int, Path]]:
    source_path = Path(source_row["path"])
    _, _, tr = ensure_audio_and_transcript(settings, source_path, campaign.lang)
    tr = apply_word_fixes(tr, campaign.word_fixes)
    banner = campaign.resolve_banner_path(PROJECT_ROOT)
    out_dir = _out_dir_for(settings, campaign)
    results: list[tuple[int, Path]] = []
    for i, cand in enumerate(chosen):
        hook = best_hook(cand)
        clip_id = tracker.add_clip(int(source_row["id"]), campaign.id, cand.start, cand.end, hook, cand.score, cand.llm_score, None)
        base = out_dir / f"{campaign.id}_s{source_row['id']}_c{clip_id}"
        workdir = Path(settings.data_dir) / "work" / f"clip_{clip_id}"
        print(f"[render] клип {clip_id}: {cand.start:.1f}–{cand.end:.1f} с — {hook}")
        try:
            res = render_clip(settings, campaign, source_path, int(source_row["width"]), int(source_row["height"]),
                              tr, cand, hook, workdir, base.with_suffix(".mp4"), banner)
        except Exception as e:
            tracker.set_clip_status(clip_id, "failed")
            print(f"[render] клип {clip_id} не собран: {e}")
            continue
        tracker.set_clip_path(clip_id, str(res.path))
        captions = build_captions(campaign, cand, hook, variant=clip_id + i)
        write_sidecars(base, campaign, cand, hook, captions, clip_id, int(source_row["id"]))
        face = f", лицо x={res.face_center_x:.2f}" if res.face_center_x is not None else ""
        print(f"[render] готово → {res.path.name} ({res.duration_s:.0f} с{face})")
        results.append((clip_id, res.path))
    return results


# ----------------------------------------------------------------------------- commands

def cmd_init(args, settings: Settings) -> int:
    cfg = write_default_config()
    settings = load_settings()
    settings.ensure_dirs()
    examples = write_examples(Path(settings.campaigns_dir))
    banners = PROJECT_ROOT / "banners"
    banners.mkdir(exist_ok=True)
    example_banner = banners / "example_banner.png"
    if not example_banner.exists():
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, 599, 199), radius=36, fill=(20, 110, 255, 235))
            d.text((40, 70), "BRAND  BANNER", fill=(255, 255, 255, 255))
            img.save(example_banner)
        except Exception:
            pass
    Tracker(settings.db_path)
    print(f"Настройки: {cfg}")
    print(f"Кампании-примеры: {', '.join(p.name for p in examples)}")
    print("Дальше: python -m pipeline doctor, затем python -m pipeline clip --campaign example_ru --source <файл или ссылка>")
    return 0


def cmd_doctor(args, settings: Settings) -> int:
    print(f"clip-pipeline {__version__}")
    ok_all = True
    for name, ok, detail in check_tools(settings):
        mark = "OK " if ok else "!! "
        ok_all = ok_all and (ok or name in {"ANTHROPIC_API_KEY", "sherpa-onnx"})
        print(f"{mark}{name}: {detail}")
    print(f"бэкенд распознавания: {settings.whisper_backend}/{settings.whisper_model}, потоков {settings.whisper_threads}")
    print(f"каталоги: data={settings.data_dir} out={settings.out_dir} campaigns={settings.campaigns_dir}")
    return 0 if ok_all else 1


def cmd_campaigns(args, settings: Settings) -> int:
    files = _campaign_files(settings)
    if not files:
        print("кампаний нет — python -m pipeline init создаст примеры")
        return 0
    for f in files:
        try:
            c = Campaign.load(f)
            print(f"{c.id:24s} {c.lang:3s} {c.exchange:12s} {c.rate_per_1k:5.2f} $/1k  порог {c.threshold_views:6d}  {c.name}")
        except Exception as e:
            print(f"{f.name}: ошибка — {e}")
    return 0


def cmd_ingest(args, settings: Settings) -> int:
    campaign = find_campaign(settings, args.campaign)
    tracker = Tracker(settings.db_path)
    src = fetch_source(args.source, Path(settings.data_dir) / "sources", settings.ffprobe)
    print(f"[source] {src.title} — {src.duration_s / 60:.1f} мин, {src.width}×{src.height}")
    _, tr_path, tr = ensure_audio_and_transcript(settings, src.path, args.lang or campaign.lang)
    source_id = tracker.upsert_source(campaign.id, args.source, str(src.path), src.title, src.duration_s,
                                      src.width, src.height, str(tr_path))
    print(f"source_id = {source_id}")
    return 0


def cmd_candidates(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    row = tracker.get_source(args.source_id)
    if not row:
        raise SystemExit(f"источник {args.source_id} не найден — сначала ingest")
    campaign = find_campaign(settings, row["campaign_id"])
    shortlist = compute_candidates(settings, campaign, row, use_llm=not args.no_llm)
    top = select_non_overlapping(shortlist, n=args.n or campaign.clips_per_source, key="final")
    print(f"\nЛучшие {len(top)} из {len(shortlist)} кандидатов:")
    for c in top:
        print("  " + summary_line(c))
    out = Path(settings.data_dir) / "candidates" / f"{campaign.id}_s{row['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    export_candidates(shortlist, out, int(row["id"]), campaign.id)
    print(f"\nФайл для ручного отбора: {out}")
    print("Отметьте chosen: true (и поправьте hook) → python -m pipeline render --selection " + str(out))
    return 0


def cmd_render(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    if args.selection:
        source_id, campaign_id, chosen = load_selection(Path(args.selection))
        row = tracker.get_source(source_id)
        if not row:
            raise SystemExit(f"источник {source_id} не найден")
        campaign = find_campaign(settings, campaign_id)
        if not chosen:
            raise SystemExit("в файле выбора нет кандидатов с chosen: true")
    else:
        if args.source_id is None:
            raise SystemExit("укажите --selection FILE или --source-id N")
        row = tracker.get_source(args.source_id)
        if not row:
            raise SystemExit(f"источник {args.source_id} не найден")
        campaign = find_campaign(settings, row["campaign_id"])
        shortlist = compute_candidates(settings, campaign, row, use_llm=not args.no_llm)
        if args.ids:
            wanted = {int(x) for x in args.ids.split(",") if x.strip()}
            chosen = [c for c in shortlist if c.id in wanted]
        else:
            chosen = select_non_overlapping(shortlist, n=args.n or campaign.clips_per_source, key="final")
    results = render_candidates(settings, campaign, tracker, row, chosen)
    print(f"\nСобрано клипов: {len(results)} → {_out_dir_for(settings, campaign)}")
    for clip_id, path in results:
        print(f"  clip_id {clip_id}: {path.name}  (подписи: {path.with_suffix('.txt').name})")
    return 0


def cmd_clip(args, settings: Settings) -> int:
    campaign = find_campaign(settings, args.campaign)
    tracker = Tracker(settings.db_path)
    src = fetch_source(args.source, Path(settings.data_dir) / "sources", settings.ffprobe)
    print(f"[source] {src.title} — {src.duration_s / 60:.1f} мин, {src.width}×{src.height}")
    _, tr_path, _ = ensure_audio_and_transcript(settings, src.path, args.lang or campaign.lang)
    source_id = tracker.upsert_source(campaign.id, args.source, str(src.path), src.title, src.duration_s,
                                      src.width, src.height, str(tr_path))
    row = tracker.get_source(source_id)
    shortlist = compute_candidates(settings, campaign, row, use_llm=not args.no_llm)
    chosen = select_non_overlapping(shortlist, n=args.n or campaign.clips_per_source, key="final")
    print(f"\nВыбрано {len(chosen)} фрагментов:")
    for c in chosen:
        print("  " + summary_line(c))
    results = render_candidates(settings, campaign, tracker, row, chosen)
    print(f"\nГотово: {len(results)} клипов → {_out_dir_for(settings, campaign)}")
    return 0 if results else 1


def cmd_post(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    post_id = tracker.add_post(args.clip_id, args.account, args.platform, args.url, args.notes or "")
    print(f"post_id = {post_id}")
    return 0


def cmd_stats(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    if args.import_file:
        n = tracker.import_stats_csv(Path(args.import_file))
        print(f"импортировано замеров: {n}")
        return 0
    if args.post_id is None or args.views is None:
        raise SystemExit("нужно --post-id и --views либо --import FILE.csv")
    sid = tracker.add_stat(args.post_id, args.views, args.likes or 0, args.comments or 0, args.shares or 0)
    print(f"stat_id = {sid}")
    return 0


def cmd_payout(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    pid = tracker.add_payout(args.campaign, args.amount, args.method or "", args.notes or "")
    print(f"payout_id = {pid}")
    return 0


def cmd_report(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    text = build_report(tracker, load_all_campaigns(settings), args.since)
    p = write_report(text, Path(settings.out_dir))
    print(text)
    print(f"сохранено: {p}")
    return 0


def cmd_export(args, settings: Settings) -> int:
    tracker = Tracker(settings.db_path)
    files = tracker.export_csv(Path(settings.out_dir) / "tracker_csv")
    for f in files:
        print(f)
    return 0


# ----------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline", description="clip-pipeline — нарезка видео под CPM-кампании")
    p.add_argument("--backend", choices=["faster", "sherpa"], help="бэкенд распознавания (переопределяет pipeline.json)")
    p.add_argument("--model", help="модель Whisper: tiny|base|small|medium|large-v3|large-v3-turbo")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать настройки, каталоги и примеры кампаний").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="проверить окружение").set_defaults(fn=cmd_doctor)
    sub.add_parser("campaigns", help="список кампаний").set_defaults(fn=cmd_campaigns)

    s = sub.add_parser("ingest", help="скачать/проверить исходник и расшифровать речь")
    s.add_argument("--campaign", required=True)
    s.add_argument("--source", required=True, help="ссылка или путь к файлу")
    s.add_argument("--lang", help="язык речи (ru|en|uz), по умолчанию — из кампании")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("candidates", help="подобрать кандидатов и выгрузить файл для ручного отбора")
    s.add_argument("--source-id", type=int, required=True)
    s.add_argument("--n", type=int)
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("render", help="собрать клипы")
    s.add_argument("--selection", help="candidates.json с отмеченными chosen: true")
    s.add_argument("--source-id", type=int)
    s.add_argument("--n", type=int)
    s.add_argument("--ids", help="id кандидатов через запятую")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("clip", help="ingest + candidates + render одной командой")
    s.add_argument("--campaign", required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--n", type=int)
    s.add_argument("--lang")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_clip)

    s = sub.add_parser("post", help="записать публикацию")
    s.add_argument("--clip-id", type=int, required=True)
    s.add_argument("--account", required=True, help="например ru_tiktok, en_instagram")
    s.add_argument("--platform", required=True, choices=["tiktok", "instagram", "youtube", "vk", "x"])
    s.add_argument("--url", required=True)
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_post)

    s = sub.add_parser("stats", help="записать просмотры публикации")
    s.add_argument("--post-id", type=int)
    s.add_argument("--views", type=int)
    s.add_argument("--likes", type=int)
    s.add_argument("--comments", type=int)
    s.add_argument("--shares", type=int)
    s.add_argument("--import", dest="import_file", help="CSV: post_id,views[,likes,comments,shares,ts]")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("payout", help="записать полученную выплату")
    s.add_argument("--campaign", required=True)
    s.add_argument("--amount", type=float, required=True, help="сумма в долларах")
    s.add_argument("--method")
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_payout)

    s = sub.add_parser("report", help="отчёт по ключевым цифрам")
    s.add_argument("--since", help="ISO-дата, например 2026-11-01")
    s.set_defaults(fn=cmd_report)

    sub.add_parser("export", help="выгрузить трекер в CSV").set_defaults(fn=cmd_export)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {"whisper_backend": args.backend, "whisper_model": args.model}
    settings = load_settings(overrides)
    settings.ensure_dirs()
    try:
        return int(args.fn(args, settings) or 0)
    except KeyboardInterrupt:
        print("прервано")
        return 130


if __name__ == "__main__":
    sys.exit(main())

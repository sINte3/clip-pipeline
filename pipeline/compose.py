"""Монтаж клипа из нескольких фрагментов под закадровый голос.

Нужен для кампаний, где исходники — короткие готовые ролики и экранные записи бренда, а не длинное
видео (например, make.fun): клип собирается из кусков разных исходников, поверх идёт свой голос,
субтитры строятся по расшифровке этого голоса, сверху — хук и метки вроде «#ad».

Спецификация — JSON-файл (python -m pipeline compose --spec data/compose/mf_01.json):

{
  "campaign_id": "makefun",
  "name": "mf_01_agent_enters",
  "voiceover": "data/voice/mf_01.m4a",        // необязательно; без голоса звук берётся из фрагментов
  "voice_offset": 0.3,                          // с какой секунды начинается голос
  "source_audio_db": null,                      // громкость звука фрагментов под голосом; null — выключить
  "tail_s": 0.6,                                // сколько держать картинку после конца голоса
  "hook": "Your AI agent can enter contests for you",
  "hook_seconds": 3.0,
  "subtitles": true,
  "badges": [{"text": "#ad", "position": "top-right"},
             {"text": "Free to enter · make.fun", "position": "bottom-center", "start": -3}],   // start<0 — за N с до конца
  "segments": [
    {"source_id": 30, "start": 0.0, "end": 5.5},
    {"source": "data/sources/fc83f028bdbd.mp4", "start": 0.0, "end": 6.0}
  ]
}

Если фрагментов не хватает на всю длину голоса, последний кадр удерживается; если хватает с избытком —
видео обрезается по концу голоса плюс tail_s.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .campaign import Campaign
from .config import PROJECT_ROOT, Settings
from .download import probe_video
from .render import _ff_escape_path, _frame_chain
from .subtitles import build_ass, font_family_from_path, write_ass
from .transcribe import Transcript, Word, apply_word_fixes, extract_audio, transcribe


@dataclass
class Segment:
    path: Path
    start: float
    end: float
    width: int = 0
    height: int = 0
    has_audio: bool = True

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Badge:
    text: str
    position: str = "top-right"
    font_size: int = 44
    start: float = 0.0      # с какой секунды показывать; отрицательное — за столько секунд до конца
    end: float = 0.0        # до какой секунды; 0 — до конца клипа
    y: int = 0              # своя вертикальная позиция в пикселях (0 — по position)

    def window(self, total: float) -> tuple[float, float]:
        s = total + self.start if self.start < 0 else self.start
        e = self.end if self.end > 0 else total
        return max(0.0, s), max(0.0, e)


@dataclass
class ComposeSpec:
    campaign_id: str
    name: str
    segments: list[dict]
    voiceover: Optional[str] = None
    voice_offset: float = 0.3
    source_audio_db: Optional[float] = None
    tail_s: float = 0.6
    hook: str = ""
    hook_seconds: float = 3.0
    hook_box_opacity: float = 0.85             # плотная подложка, чтобы текст исходника под хуком не просвечивал
    subtitles: bool = True
    badges: list[dict] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "ComposeSpec":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        known = {k: v for k, v in raw.items() if k in ComposeSpec.__dataclass_fields__}
        unknown = set(raw) - set(known)
        if unknown:
            raise ValueError(f"{path.name}: неизвестные поля {sorted(unknown)}")
        spec = ComposeSpec(**known)
        if not spec.segments:
            raise ValueError("segments пуст")
        return spec


@dataclass
class ComposeResult:
    path: Path
    duration_s: float
    transcript_text: str
    segments: list[Segment]


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def has_audio_stream(ffprobe: str, path: Path) -> bool:
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return bool(out.strip())


def media_duration(ffprobe: str, path: Path) -> float:
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0.0)


def resolve_segments(settings: Settings, spec: ComposeSpec, source_lookup) -> list[Segment]:
    """source_lookup(source_id) -> путь к файлу исходника (из трекера)."""
    segs: list[Segment] = []
    for i, raw in enumerate(spec.segments):
        if "source_id" in raw:
            path = Path(source_lookup(int(raw["source_id"])))
        elif "source" in raw:
            path = _resolve(str(raw["source"]))
        else:
            raise ValueError(f"segments[{i}]: нужен source_id или source")
        if not path.exists():
            raise FileNotFoundError(f"segments[{i}]: файл не найден: {path}")
        dur, w, h = probe_video(settings.ffprobe, path)
        start = float(raw.get("start", 0.0))
        end = float(raw.get("end", dur))
        end = min(end, dur)
        if end - start < 0.2:
            raise ValueError(f"segments[{i}]: пустой интервал {start}–{end} (длина файла {dur:.1f} с)")
        segs.append(Segment(path=path, start=start, end=end, width=w, height=h,
                            has_audio=has_audio_stream(settings.ffprobe, path)))
    return segs


def _render_segment(settings: Settings, seg: Segment, out: Path, frame_mode: str) -> None:
    """Один фрагмент → 1080×1920, 30 к/с, стерео 48 кГц (тишина, если у исходника нет звука)."""
    w, h, fps = settings.out_width, settings.out_height, settings.fps
    vchain = f"[0:v]{_frame_chain(frame_mode, seg.width, seg.height, w, h, None)},setsar=1,fps={fps},format=yuv420p[v]"
    cmd = [settings.ffmpeg, "-v", "error", "-y", "-ss", f"{seg.start:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)]
    if seg.has_audio:
        # короткие фейды на стыках, чтобы склейка фраз из разных роликов не щёлкала
        fade_out_at = max(0.0, seg.duration - 0.06)
        achain = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                  f"afade=t=in:d=0.03,afade=t=out:st={fade_out_at:.3f}:d=0.06[a]")
    else:
        cmd += ["-f", "lavfi", "-t", f"{seg.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        achain = "[1:a]anull[a]"
    cmd += ["-filter_complex", f"{vchain};{achain}", "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", settings.preset, "-crf", "18", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-t", f"{seg.duration:.3f}", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg (фрагмент {seg.path.name}) завершился с ошибкой:\n{proc.stderr}")


def _concat(settings: Settings, parts: list[Path], out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    cmd = [settings.ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg (склейка) завершился с ошибкой:\n{proc.stderr}")


def _badge_xy(position: str, margin: int = 60) -> tuple[str, str]:
    table = {
        "top-left": (f"{margin}", f"{margin}"),
        "top-right": (f"w-text_w-{margin}", f"{margin}"),
        "bottom-left": (f"{margin}", f"h-text_h-{margin}"),
        "bottom-right": (f"w-text_w-{margin}", f"h-text_h-{margin}"),
        "top-center": ("(w-text_w)/2", f"{margin}"),
        "bottom-center": ("(w-text_w)/2", f"h-text_h-{margin}"),
    }
    return table.get(position, table["top-right"])


def _shift_words(words: list[Word], offset: float) -> list[Word]:
    return [Word(start=w.start + offset, end=w.end + offset, text=w.text, prob=w.prob) for w in words]


def compose_clip(settings: Settings, campaign: Campaign, spec: ComposeSpec, segments: list[Segment],
                 workdir: Path, out_path: Path, lang: Optional[str] = None) -> ComposeResult:
    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Фрагменты в единый формат и склейка
    parts = []
    for i, seg in enumerate(segments):
        part = workdir / f"seg_{i:02d}.mp4"
        _render_segment(settings, seg, part, campaign.frame_mode)
        parts.append(part)
    concat = workdir / "concat.mp4"
    _concat(settings, parts, concat)
    video_len = sum(s.duration for s in segments)

    # 2. Голос, его длина и расшифровка (или расшифровка звука самих фрагментов)
    voice: Optional[Path] = _resolve(spec.voiceover) if spec.voiceover else None
    if voice is not None and not voice.exists():
        raise FileNotFoundError(f"файл голоса не найден: {voice}")
    words: list[Word] = []
    if voice is not None:
        voice_len = media_duration(settings.ffprobe, voice)
        total = spec.voice_offset + voice_len + spec.tail_s
        if spec.subtitles:
            wav = workdir / "voice.wav"
            extract_audio(settings.ffmpeg, voice, wav)
            tr = transcribe(wav, settings, language=lang or campaign.lang)
            tr = apply_word_fixes(tr, campaign.word_fixes)
            words = _shift_words(tr.words, spec.voice_offset)
    else:
        total = video_len
        if spec.subtitles:
            wav = workdir / "concat.wav"
            extract_audio(settings.ffmpeg, concat, wav)
            tr = transcribe(wav, settings, language=lang or campaign.lang)
            tr = apply_word_fixes(tr, campaign.word_fixes)
            words = tr.words
    pad = max(0.0, total - video_len)

    # 3. Видеоцепочка: удержание последнего кадра, субтитры, хук, метки
    font_file = _ff_escape_path(settings.font_bold) if settings.font_bold else ""
    font_dir = _ff_escape_path(str(Path(settings.font_bold).parent)) if settings.font_bold else ""
    chain = []
    last = "0:v"
    if pad > 0.01:
        chain.append(f"[{last}]tpad=stop_mode=clone:stop_duration={pad:.3f}[vp]")
        last = "vp"
    idx = 0
    if words:
        ass = build_ass(words, 0.0, total, font_family_from_path(settings.font_bold),
                        font_size=settings.subtitle_font_size, margin_v=settings.subtitle_margin_v,
                        play_w=settings.out_width, play_h=settings.out_height)
        write_ass(workdir / "subs.ass", ass)
        fonts_opt = f":fontsdir='{font_dir}'" if font_dir else ""
        chain.append(f"[{last}]subtitles=filename=subs.ass{fonts_opt}[s{idx}]")
        last = f"s{idx}"
        idx += 1
    if spec.hook and font_file:
        hook_txt = "\n".join(textwrap.wrap(spec.hook, width=22, break_long_words=False)) or spec.hook
        (workdir / "hook.txt").write_text(hook_txt, encoding="utf-8", newline="\n")
        chain.append(
            f"[{last}]drawtext=fontfile='{font_file}':textfile=hook.txt:fontsize={settings.hook_font_size}:"
            f"fontcolor=white:line_spacing=6:box=1:boxcolor=black@{spec.hook_box_opacity:.2f}:boxborderw=20:"
            f"x=(w-text_w)/2:y={settings.hook_y}:enable='between(t,0,{spec.hook_seconds:.2f})'[s{idx}]"
        )
        last = f"s{idx}"
        idx += 1
    for bi, braw in enumerate(spec.badges):
        b = Badge(**braw)
        if not font_file:
            break
        (workdir / f"badge_{bi}.txt").write_text(b.text, encoding="utf-8", newline="\n")
        x, y = _badge_xy(b.position)
        if b.y > 0:
            y = str(b.y)
        bs, be = b.window(total)
        chain.append(
            f"[{last}]drawtext=fontfile='{font_file}':textfile=badge_{bi}.txt:fontsize={b.font_size}:"
            f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:x={x}:y={y}:"
            f"enable='between(t,{bs:.2f},{be:.2f})'[s{idx}]"
        )
        last = f"s{idx}"
        idx += 1
    chain.append(f"[{last}]format=yuv420p[vout]")

    # 4. Звук: голос поверх (приглушённого или выключенного) звука фрагментов
    inputs = ["-i", str(concat)]
    if voice is not None:
        inputs += ["-i", str(voice)]
        delay_ms = int(round(spec.voice_offset * 1000))
        vchain = f"[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay_ms}|{delay_ms}[va]"
        if spec.source_audio_db is None:
            chain.append(vchain)
            chain.append(f"[va]apad,loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        else:
            chain.append(f"[0:a]volume={spec.source_audio_db}dB,apad[ba]")
            chain.append(vchain)
            chain.append("[ba][va]amix=inputs=2:duration=longest:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    else:
        chain.append("[0:a]apad,loudnorm=I=-16:TP=-1.5:LRA=11[aout]")

    cmd = [settings.ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain),
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf), "-maxrate", "8M", "-bufsize", "16M",
           "-r", str(settings.fps), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-t", f"{total:.3f}", "-movflags", "+faststart", str(out_path)]
    proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg (сборка) завершился с ошибкой:\n{proc.stderr}\nКоманда: {' '.join(cmd)}")
    text = " ".join(w.text for w in words)
    return ComposeResult(path=out_path, duration_s=total, transcript_text=text, segments=segments)

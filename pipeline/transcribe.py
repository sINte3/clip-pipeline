"""Извлечение звука и расшифровка речи с временными метками слов.

Два бэкенда:
  faster  — faster-whisper (CTranslate2). Даёт пословные метки. Модель скачивается с Hugging Face
            при первом запуске и кэшируется. Рекомендуемый вариант на домашнем ПК.
  sherpa  — sherpa-onnx с Whisper-моделью из каталога sherpa_model_dir (small/medium/turbo).
            Пословных меток не даёт: слова распределяются равномерно внутри 25-секундного окна
            и помечаются approx=True. Пригоден там, где Hugging Face недоступен.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .config import Settings


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Transcript:
    language: str
    duration_s: float
    words: list[Word]
    approx_timestamps: bool = False
    backend: str = ""

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def save(self, path: Path) -> None:
        payload = {
            "language": self.language,
            "duration_s": self.duration_s,
            "approx_timestamps": self.approx_timestamps,
            "backend": self.backend,
            "words": [asdict(w) for w in self.words],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "Transcript":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Transcript(
            language=raw["language"],
            duration_s=float(raw["duration_s"]),
            words=[Word(**w) for w in raw["words"]],
            approx_timestamps=bool(raw.get("approx_timestamps", False)),
            backend=raw.get("backend", ""),
        )


def extract_audio(ffmpeg: str, video: Path, wav: Path, sample_rate: int = 16000) -> Path:
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", str(sample_rate),
           "-c:a", "pcm_s16le", str(wav)]
    subprocess.run(cmd, check=True)
    return wav


def _wav_duration(wav: Path) -> float:
    import soundfile as sf
    info = sf.info(str(wav))
    return float(info.frames) / float(info.samplerate)


def _transcribe_faster(wav: Path, settings: Settings, language: Optional[str]) -> Transcript:
    from faster_whisper import WhisperModel  # type: ignore

    model = WhisperModel(settings.whisper_model, device="cpu", compute_type=settings.whisper_compute,
                         cpu_threads=settings.whisper_threads)
    segments, info = model.transcribe(
        str(wav), language=language, beam_size=5, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
    )
    words: list[Word] = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                token = w.word.strip()
                if token:
                    words.append(Word(start=float(w.start), end=float(w.end), text=token, prob=float(w.probability or 1.0)))
        elif seg.text.strip():
            # На случай, если бэкенд не вернул слова для сегмента — распределяем равномерно
            tokens = seg.text.strip().split()
            step = (seg.end - seg.start) / max(1, len(tokens))
            for i, t in enumerate(tokens):
                words.append(Word(start=seg.start + i * step, end=seg.start + (i + 1) * step, text=t))
    return Transcript(language=info.language, duration_s=_wav_duration(wav), words=words,
                      approx_timestamps=False, backend=f"faster-whisper/{settings.whisper_model}")


def _transcribe_sherpa(wav: Path, settings: Settings, language: Optional[str]) -> Transcript:
    import numpy as np
    import sherpa_onnx  # type: ignore
    import soundfile as sf

    d = Path(settings.sherpa_model_dir)
    if not d.exists():
        raise FileNotFoundError(f"sherpa_model_dir не найден: {d}")
    enc = next(iter(sorted(d.glob("*encoder.int8.onnx"))), None) or next(iter(sorted(d.glob("*encoder.onnx"))))
    dec = next(iter(sorted(d.glob("*decoder.int8.onnx"))), None) or next(iter(sorted(d.glob("*decoder.onnx"))))
    tok = next(iter(sorted(d.glob("*tokens.txt"))))
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(enc), decoder=str(dec), tokens=str(tok),
        language=language or "", task="transcribe", num_threads=settings.whisper_threads, tail_paddings=2000,
    )
    audio, sr = sf.read(str(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    chunk = 25 * sr
    step = 25 * sr
    words: list[Word] = []
    pos = 0
    while pos < len(audio):
        seg = audio[pos:pos + chunk]
        # Границу окна сдвигаем к ближайшей тишине, чтобы не резать слово пополам
        if len(seg) == chunk and pos + chunk < len(audio):
            tail = seg[-int(2 * sr):]
            frame = int(0.05 * sr)
            energies = [float(np.abs(tail[i:i + frame]).mean()) for i in range(0, len(tail) - frame, frame)]
            if energies:
                quietest = int(np.argmin(energies))
                cut = len(seg) - len(tail) + quietest * frame
                seg = seg[:cut]
        s = rec.create_stream()
        s.accept_waveform(sr, seg)
        rec.decode_stream(s)
        text = s.result.text.strip()
        seg_start = pos / sr
        seg_dur = len(seg) / sr
        tokens = text.split()
        if tokens:
            # Равномерное распределение по длительности окна; реальные паузы неизвестны
            per = seg_dur / len(tokens)
            for i, t in enumerate(tokens):
                words.append(Word(start=seg_start + i * per, end=seg_start + (i + 1) * per, text=t, prob=0.5))
        pos += len(seg) if len(seg) > 0 else step
    return Transcript(language=language or "auto", duration_s=_wav_duration(wav), words=words,
                      approx_timestamps=True, backend=f"sherpa-onnx/{d.name}")


def transcribe(wav: Path, settings: Settings, language: Optional[str] = None) -> Transcript:
    backend = settings.whisper_backend
    if backend == "faster":
        return _transcribe_faster(wav, settings, language)
    if backend == "sherpa":
        return _transcribe_sherpa(wav, settings, language)
    raise ValueError(f"неизвестный whisper_backend: {backend} (faster | sherpa)")

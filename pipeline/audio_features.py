"""Энергия звука по окнам — один из признаков для оценки фрагментов.

Громкие и неровные участки (смех, спор, эмоциональный ответ) чаще дают залётные клипы,
чем ровное монотонное чтение. Признак грубый, но дешёвый.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class EnergyProfile:
    window_s: float
    rms: np.ndarray          # среднеквадратичная громкость по окнам
    mean: float
    std: float

    def between(self, start: float, end: float) -> tuple[float, float]:
        """Средняя и стандартное отклонение громкости на отрезке, нормированные к целому файлу."""
        a = int(max(0, start // self.window_s))
        b = int(min(len(self.rms), max(a + 1, end // self.window_s)))
        seg = self.rms[a:b]
        if len(seg) == 0 or self.mean == 0:
            return 0.0, 0.0
        return float(seg.mean() / self.mean), float(seg.std() / (self.std + 1e-9))


def energy_profile(wav: Path, window_s: float = 0.5) -> EnergyProfile:
    import soundfile as sf
    audio, sr = sf.read(str(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    n = int(window_s * sr)
    if n <= 0 or len(audio) == 0:
        return EnergyProfile(window_s, np.zeros(1), 0.0, 0.0)
    count = len(audio) // n
    trimmed = audio[: count * n] if count > 0 else audio
    if count == 0:
        rms = np.array([float(np.sqrt(np.mean(trimmed ** 2)))])
    else:
        frames = trimmed.reshape(count, n)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return EnergyProfile(window_s=window_s, rms=rms, mean=float(rms.mean()), std=float(rms.std()))

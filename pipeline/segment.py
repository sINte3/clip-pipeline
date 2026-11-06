"""Разбиение расшифровки на предложения и подбор кандидатов в клипы.

Идея: клип должен начинаться с цепляющей фразы, заканчиваться на границе мысли, быть
плотным по речи и попадать в 20–60 секунд. Эвристики ниже дают первичный рейтинг;
языковая модель (score_llm) или человек могут его переранжировать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from .audio_features import EnergyProfile
from .transcribe import Transcript, Word


TERMINAL = (".", "!", "?", "…")

QUESTION_WORDS = {
    "ru": {"почему", "как", "что", "зачем", "сколько", "кто", "какой", "какая", "какие", "когда", "где", "откуда", "правда", "знаете", "представьте"},
    "en": {"why", "how", "what", "when", "who", "which", "where", "imagine", "nobody", "never", "ever", "guess", "did", "do", "does", "is", "are", "can", "should", "would"},
    "uz": {"nega", "qanday", "nima", "qancha", "kim", "qaysi", "qachon", "qayerda", "tasavvur"},
}
STRONG_WORDS = {
    "ru": {"секрет", "ошибка", "ошибки", "никогда", "главное", "правда", "деньги", "миллион", "миллионов", "тысяч", "бесплатно",
           "запрещено", "нельзя", "опасно", "самое", "самый", "лучший", "худший", "первый", "последний", "вся", "весь", "честно",
           "шок", "страшно", "рухнул", "взлетел", "провал", "успех", "доход", "зарплата", "бизнес", "ии", "нейросеть", "нейросети"},
    "en": {"secret", "mistake", "mistakes", "never", "truth", "money", "million", "millions", "thousand", "free", "illegal", "banned",
           "dangerous", "biggest", "worst", "best", "first", "last", "honestly", "insane", "crazy", "failed", "failure", "success",
           "income", "salary", "business", "ai", "startup", "rich", "broke", "billion", "hack", "trick"},
    "uz": {"sir", "xato", "hech qachon", "pul", "million", "bepul", "eng", "birinchi", "oxirgi", "biznes"},
}
FILLERS = {
    "ru": {"ну", "э", "эм", "ээ", "короче", "типа", "вот", "как бы", "это самое", "значит", "блин"},
    "en": {"um", "uh", "like", "you know", "i mean", "kind of", "sort of", "basically", "literally"},
    "uz": {"ya'ni", "xullas", "demak"},
}
LEADING_CONJ = {
    "ru": {"и", "а", "но", "потому", "поэтому", "или", "то", "что", "чтобы", "же", "ведь", "так", "который", "которая"},
    "en": {"and", "but", "so", "because", "or", "then", "which", "that", "also"},
    "uz": {"va", "lekin", "ammo", "chunki", "shuning"},
}


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    n_words: int


@dataclass
class Candidate:
    id: int
    start: float
    end: float
    text: str
    hook: str
    score: float
    features: dict = field(default_factory=dict)
    llm_score: Optional[float] = None
    llm_hook: Optional[str] = None
    caption_line: Optional[str] = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        return d


def _lang_key(lang: str) -> str:
    return lang if lang in QUESTION_WORDS else "en"


def _normalize_token(t: str) -> str:
    return re.sub(r"[^\w'’-]+", "", t.lower(), flags=re.UNICODE)


def split_sentences(tr: Transcript, max_sentence_s: float = 12.0, gap_s: float = 0.7) -> list[Sentence]:
    """Предложения: конец по знаку препинания, по паузе (если метки точные) или по длине."""
    sentences: list[Sentence] = []
    buf: list[Word] = []

    def flush() -> None:
        if buf:
            text = " ".join(w.text for w in buf).strip()
            sentences.append(Sentence(start=buf[0].start, end=buf[-1].end, text=text, n_words=len(buf)))
            buf.clear()

    for i, w in enumerate(tr.words):
        buf.append(w)
        nxt = tr.words[i + 1] if i + 1 < len(tr.words) else None
        ends_punct = w.text.rstrip("»\")”").endswith(TERMINAL)
        long_enough = (w.end - buf[0].start) >= max_sentence_s
        pause = (nxt is not None) and (not tr.approx_timestamps) and (nxt.start - w.end >= gap_s)
        comma_break = long_enough and w.text.endswith((",", ";", ":"))
        if ends_punct or pause or comma_break or (w.end - buf[0].start) >= max_sentence_s * 1.5:
            flush()
    flush()
    return sentences


def _hook_features(first: Sentence, lang: str) -> float:
    key = _lang_key(lang)
    tokens = [_normalize_token(t) for t in first.text.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0.0
    score = 0.0
    if tokens[0] in QUESTION_WORDS[key]:
        score += 0.45
    if first.text.strip().endswith("?"):
        score += 0.25
    if re.search(r"\d|\$|€|₽|%", first.text):
        score += 0.25
    if any(t in STRONG_WORDS[key] for t in tokens):
        score += 0.3
    if len(tokens) <= 14:
        score += 0.15
    if tokens[0] in LEADING_CONJ[key]:
        score -= 0.35
    return max(0.0, min(1.0, score))


def _content_features(text: str, lang: str) -> float:
    key = _lang_key(lang)
    tokens = [_normalize_token(t) for t in text.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0.0
    strong = sum(1 for t in tokens if t in STRONG_WORDS[key])
    numbers = len(re.findall(r"\d+", text))
    questions = text.count("?")
    raw = (strong * 1.0 + numbers * 0.8 + questions * 0.6) / max(1.0, len(tokens) / 25.0)
    return max(0.0, min(1.0, raw / 3.0))


def _filler_ratio(text: str, lang: str) -> float:
    key = _lang_key(lang)
    tokens = [_normalize_token(t) for t in text.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0.0
    fillers = sum(1 for t in tokens if t in FILLERS[key])
    return fillers / len(tokens)


def make_hook(text: str, lang: str, max_words: int = 8) -> str:
    """Короткий заголовок из первой фразы: убираем слова-паразиты, режем до max_words."""
    key = _lang_key(lang)
    tokens = text.split()
    cleaned = [t for t in tokens if _normalize_token(t) not in FILLERS[key]]
    cleaned = cleaned[:max_words]
    hook = " ".join(cleaned).strip()
    hook = re.sub(r"[,;:]+$", "", hook)
    if hook and not hook.endswith(("?", "!")):
        hook = hook.rstrip(".…")
    if hook:
        hook = hook[0].upper() + hook[1:]
    return hook


def build_candidates(tr: Transcript, lang: str, energy: Optional[EnergyProfile],
                     min_s: float, max_s: float, limit_windows: int = 4000) -> list[Candidate]:
    sentences = split_sentences(tr)
    cands: list[Candidate] = []
    n = len(sentences)
    cid = 0
    for i in range(n):
        j = i
        while j < n:
            dur = sentences[j].end - sentences[i].start
            if dur > max_s:
                break
            if dur >= min_s:
                window = sentences[i:j + 1]
                text = " ".join(s.text for s in window)
                words = sum(s.n_words for s in window)
                wps = words / max(1e-6, dur)
                hook = _hook_features(sentences[i], lang)
                content = _content_features(text, lang)
                density = max(0.0, 1.0 - abs(wps - 2.9) / 2.9)
                e_mean, e_std = energy.between(sentences[i].start, sentences[j].end) if energy else (1.0, 0.0)
                energy_score = max(0.0, min(1.0, (min(e_mean, 1.5) - 0.6) / 0.9)) * 0.7 + min(1.0, e_std) * 0.3
                completeness = 1.0 if sentences[j].text.rstrip("»\")”").endswith(TERMINAL) else 0.5
                first_tok = _normalize_token(sentences[i].text.split()[0]) if sentences[i].text.split() else ""
                if first_tok in LEADING_CONJ[_lang_key(lang)]:
                    completeness *= 0.6
                length_pref = max(0.0, 1.0 - abs(dur - 35.0) / 40.0)
                filler = _filler_ratio(text, lang)
                penalty = max(0.0, filler - 0.08) * 2.0
                score = (0.30 * hook + 0.15 * content + 0.15 * density + 0.15 * energy_score
                         + 0.15 * completeness + 0.10 * length_pref - penalty)
                cands.append(Candidate(
                    id=cid, start=round(sentences[i].start, 2), end=round(sentences[j].end, 2), text=text,
                    hook=make_hook(sentences[i].text, lang), score=round(max(0.0, score), 4),
                    features={"hook": round(hook, 3), "content": round(content, 3), "density": round(density, 3),
                              "energy": round(energy_score, 3), "completeness": round(completeness, 3),
                              "length": round(length_pref, 3), "filler_ratio": round(filler, 3), "wps": round(wps, 2)},
                ))
                cid += 1
                if len(cands) >= limit_windows:
                    return cands
            j += 1
    return cands


def select_non_overlapping(cands: list[Candidate], n: int, max_overlap_frac: float = 0.15,
                           key: str = "score") -> list[Candidate]:
    """Жадный отбор: лучшие по рейтингу, почти не пересекающиеся между собой."""
    def rank(c: Candidate) -> float:
        if key == "final":
            if c.llm_score is not None:
                return 0.5 * c.score + 0.5 * (c.llm_score / 10.0)
            return c.score
        return c.score

    chosen: list[Candidate] = []
    for c in sorted(cands, key=rank, reverse=True):
        ok = True
        for ch in chosen:
            overlap = max(0.0, min(c.end, ch.end) - max(c.start, ch.start))
            if overlap > max_overlap_frac * min(c.duration, ch.duration):
                ok = False
                break
        if ok:
            chosen.append(c)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda c: c.start)

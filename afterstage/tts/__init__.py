"""Text-to-speech backends.

* chatterbox – Resemble AI Chatterbox Multilingual (23 languages incl. Korean, zero-shot voice cloning,
               emotion "exaggeration" control). Runs in-process on the GPU. MIT licensed.
* dummy      – speech-shaped tone of plausible length; lets the whole pipeline run without a GPU.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Protocol

import numpy as np

from ..log import log


class TTS(Protocol):
    sample_rate: int

    def synthesize(self, text: str, out: Path, *, language: str, voice_ref: Path | None,
                   emotion: str = "neutral") -> Path: ...

    def close(self) -> None: ...


EMOTION_EXAGGERATION = {
    "neutral": 0.5, "warm": 0.55, "calm": 0.4, "excited": 0.8, "happy": 0.7, "playful": 0.7,
    "emotional": 0.75, "sad": 0.6, "surprised": 0.8, "serious": 0.4, "shy": 0.45,
}


def write_wav(path: Path, samples: np.ndarray, sr: int) -> Path:
    data = np.clip(samples, -1.0, 1.0)
    pcm = (data * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


class DummyTTS:
    sample_rate = 24000

    def synthesize(self, text, out, *, language, voice_ref, emotion="neutral"):
        # ~14 chars/s for Korean/CJK, ~15 chars/s for latin text; syllable-like amplitude envelope
        seconds = max(1.5, len(text) / 14.0)
        t = np.arange(int(seconds * self.sample_rate)) / self.sample_rate
        pitch = 180 + (hash(str(voice_ref)) % 80)
        env = 0.5 * (1 + np.sin(2 * math.pi * 4.0 * t)) * (np.sin(2 * math.pi * 0.5 * t) ** 2)
        sig = 0.25 * env * np.sin(2 * math.pi * pitch * t)
        return write_wav(Path(out), sig, self.sample_rate)

    def close(self):
        pass


class ChatterboxTTS:
    def __init__(self, cfg: dict) -> None:
        import torch  # noqa: F401
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        self.cfg = cfg
        log.info("loading Chatterbox Multilingual TTS…")
        self.model = ChatterboxMultilingualTTS.from_pretrained(device=cfg.get("device", "cuda"))
        self.sample_rate = self.model.sr

    def synthesize(self, text, out, *, language, voice_ref, emotion="neutral"):
        import torchaudio

        exaggeration = EMOTION_EXAGGERATION.get(emotion, self.cfg.get("exaggeration", 0.5))
        kwargs = dict(language_id=language, exaggeration=exaggeration, cfg_weight=self.cfg.get("cfg_weight", 0.5),
                      temperature=self.cfg.get("temperature", 0.8))
        if voice_ref:
            kwargs["audio_prompt_path"] = str(voice_ref)
        wav = self.model.generate(text, **kwargs)
        torchaudio.save(str(out), wav.cpu(), self.sample_rate)
        return Path(out)

    def close(self):
        import torch

        del self.model
        torch.cuda.empty_cache()


def make_tts(cfg: dict) -> TTS:
    backend = cfg.get("backend", "chatterbox")
    if backend == "dummy":
        return DummyTTS()
    if backend == "chatterbox":
        return ChatterboxTTS(cfg)
    raise ValueError(f"unknown tts backend {backend!r}")

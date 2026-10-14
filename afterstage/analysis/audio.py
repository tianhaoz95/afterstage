"""Audio analysis: vocal separation (Demucs) and speech recognition (Whisper via transformers).

Both run locally on the GPU; models are pulled once from the Hugging Face hub into the local
cache by ``scripts/setup_spark.sh`` and loaded offline afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .. import media
from ..log import log
from ..schema import TranscriptSegment


def separate_vocals(audio: Path, out_dir: Path, device: str) -> Path | None:
    """Split music from voice so face-matched voice samples are usable for cloning."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-d",
           "cuda" if device.startswith("cuda") else "cpu", "-o", str(out_dir), str(audio)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        log.warning(f"demucs failed, continuing without separation: {proc.stderr.strip()[-500:]}")
        return None
    produced = next(out_dir.rglob("vocals.wav"), None)
    if produced is None:
        return None
    dest = out_dir / "vocals.wav"
    shutil.move(str(produced), dest)
    return dest


def transcribe(audio: Path, model_id: str, device: str) -> list[TranscriptSegment]:
    try:
        import torch
        from transformers import pipeline
    except ImportError:
        log.warning("transformers/torch not installed; skipping ASR")
        return []
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    try:  # transformers >= 4.56 / 5.x
        asr = pipeline("automatic-speech-recognition", model=model_id, dtype=dtype, device=device, chunk_length_s=30)
    except TypeError:
        asr = pipeline("automatic-speech-recognition", model=model_id, torch_dtype=dtype, device=device,
                       chunk_length_s=30)
    wav16 = audio.with_name(audio.stem + "_16k.wav")
    media.run(["ffmpeg", "-i", str(audio), "-ac", "1", "-ar", "16000", str(wav16)])
    result = asr(str(wav16), return_timestamps=True, generate_kwargs={"task": "transcribe"})
    segs = []
    for ch in result.get("chunks", []):
        start, end = ch.get("timestamp") or (None, None)
        text = (ch.get("text") or "").strip()
        if start is None or not text:
            continue
        segs.append(TranscriptSegment(start=float(start), end=float(end if end is not None else start + 2.0), text=text))
    del asr
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    return segs


def pick_voice_sample(vocals: Path, segments: list[TranscriptSegment], out: Path, *, start: float | None = None,
                      end: float | None = None, min_len: float = 4.0, max_len: float = 12.0) -> tuple[Path, str] | None:
    """Cut a clean speech window for cloning. Explicit [start, end] wins; otherwise the longest
    transcribed spoken (non-lyric) stretch is chosen."""
    text = ""
    if start is None or end is None:
        best = None
        for seg in segments:
            dur = seg.end - seg.start
            if dur >= min_len and (best is None or dur > best.end - best.start):
                best = seg
        if best is None:
            return None
        start, end, text = best.start, min(best.end, best.start + max_len), best.text
    else:
        text = " ".join(s.text for s in segments if s.start >= start - 0.2 and s.end <= end + 0.2)
    media.cut_audio(vocals, out, start, end)
    return out, text

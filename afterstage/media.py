"""ffmpeg / ffprobe helpers. Everything media-related shells out to ffmpeg so it works the same
on the DGX Spark (Ubuntu ffmpeg) and on a dev laptop."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .log import log


class MediaError(RuntimeError):
    pass


def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(f"{name} not found on PATH (install ffmpeg)")
    return path


def run(args: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    cmd = [_bin(args[0]), *args[1:]]
    if args[0] == "ffmpeg":
        cmd[1:1] = ["-hide_banner", "-loglevel", "error", "-y"]
    log.debug(" ".join(map(str, cmd)))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"{args[0]} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}\ncmd: {' '.join(map(str, cmd))}")
    return proc


@dataclass
class ProbeInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: str | Path) -> ProbeInfo:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = any(s["codec_type"] == "audio" for s in data["streams"])
    duration = float(data["format"].get("duration") or (v or {}).get("duration") or 0.0)
    if v is None:
        return ProbeInfo(duration, 0, 0, 0.0, a)
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return ProbeInfo(duration, int(v["width"]), int(v["height"]), fps, a)


def audio_duration(path: str | Path) -> float:
    return probe(path).duration


def extract_audio(video: str | Path, out: str | Path, sr: int = 44100, mono: bool = False) -> Path:
    args = ["ffmpeg", "-i", str(video), "-vn", "-ar", str(sr)]
    if mono:
        args += ["-ac", "1"]
    run(args + [str(out)])
    return Path(out)


def cut_audio(src: str | Path, out: str | Path, start: float, end: float, sr: int = 24000) -> Path:
    run(["ffmpeg", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src), "-ac", "1", "-ar", str(sr), str(out)])
    return Path(out)


def frame_at(video: str | Path, t: float, out: str | Path) -> Path:
    run(["ffmpeg", "-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)])
    return Path(out)


def last_frame(video: str | Path, out: str | Path) -> Path:
    # -sseof seeks relative to the end; grab the final decodable frame.
    run(["ffmpeg", "-sseof", "-0.5", "-i", str(video), "-update", "1", "-q:v", "2", str(out)])
    return Path(out)


def frames_to_video(frames: list[Path], fps: float, out: str | Path, audio: str | Path | None = None,
                    crf: int = 16) -> Path:
    """Mux an ordered PNG sequence (+ optional audio) into an mp4."""
    if not frames:
        raise MediaError("no frames to encode")
    lst = Path(out).with_suffix(".frames.txt")
    lst.write_text("".join(f"file '{f.resolve()}'\nduration {1.0 / fps:.6f}\n" for f in frames))
    args = ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(lst)]
    if audio:
        args += ["-i", str(audio), "-c:a", "aac", "-b:a", "192k", "-shortest"]
    args += ["-r", f"{fps}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf), str(out)]
    run(args)
    lst.unlink(missing_ok=True)
    return Path(out)


def concat_audio(parts: list[Path], out: str | Path, sr: int = 24000) -> Path:
    lst = Path(out).with_suffix(".list.txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(lst), "-ar", str(sr), "-ac", "1", str(out)])
    lst.unlink(missing_ok=True)
    return Path(out)


def pad_audio(src: str | Path, out: str | Path, pad_seconds: float, sr: int = 24000) -> Path:
    run(["ffmpeg", "-i", str(src), "-af", f"apad=pad_dur={pad_seconds:.3f}", "-ar", str(sr), "-ac", "1", str(out)])
    return Path(out)


def has_filter(name: str) -> bool:
    try:
        out = run(["ffmpeg", "-hide_banner", "-filters"]).stdout
    except MediaError:
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if len(line.split()) > 1)

"""Render backends: turn prompts + reference images + audio into stills and video clips.

* ComfyRenderer – the real thing; drives a local ComfyUI with the graphs in ``comfy/workflows.py``.
* DummyRenderer – ffmpeg/PIL placeholders with correct durations, for dry runs and CI.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .. import media
from ..comfy import workflows as W
from ..comfy.client import ComfyClient
from ..config import Config, ensure_local
from ..log import log


class Renderer(Protocol):
    def keyframe(self, prompt: str, refs: list[Path], out: Path, seed: int) -> Path: ...
    def talk(self, image: Path, audio: Path, prompt: str, seconds: float, out: Path, seed: int) -> Path: ...
    def broll(self, start: Path, prompt: str, seconds: float, out: Path, seed: int, end: Path | None = None) -> Path: ...
    def music(self, tags: str, seconds: float, out: Path, seed: int) -> Path: ...
    def free(self) -> None: ...


# --------------------------------------------------------------------------- ComfyUI


class ComfyRenderer:
    def __init__(self, cfg: Config) -> None:
        url = cfg["comfy"]["url"]
        ensure_local(url, cfg, "ComfyUI")
        self.cfg = cfg
        self.p = cfg.profile
        self.models = cfg["models"]
        self.client = ComfyClient(url, timeout=cfg["comfy"]["timeout"])

    def _frames(self, graph, label: str) -> list[Path]:
        tmp = Path(tempfile.mkdtemp(prefix="afterstage_"))
        return self.client.run(graph, tmp, label)

    def keyframe(self, prompt, refs, out, seed):
        names = [self.client.upload(r) for r in refs[:3]]
        g = W.keyframe_graph(prompt, names, width=self.p["width"], height=self.p["height"], seed=seed,
                             steps=self.p["keyframe_steps"], cfg=self.p["keyframe_cfg"],
                             lightning=self.p["keyframe_lightning"], models=self.models)
        files = self._frames(g, f"keyframe {out.stem}")
        shutil.copy(files[0], out)
        shutil.rmtree(files[0].parent, ignore_errors=True)
        return out

    def talk(self, image, audio, prompt, seconds, out, seed):
        g = W.talk_graph(self.client.upload(image), self.client.upload(audio), prompt, seconds=seconds,
                         width=self.p["width"], height=self.p["height"], seed=seed, steps=self.p["talk_steps"],
                         models=self.models)
        frames = self._frames(g, f"talk {out.stem}")
        media.frames_to_video(frames, W.TALK_FPS, out, audio=audio)
        shutil.rmtree(frames[0].parent, ignore_errors=True)
        return out

    def broll(self, start, prompt, seconds, out, seed, end=None):
        g = W.broll_graph(self.client.upload(start), prompt, seconds=seconds, width=self.p["width"],
                          height=self.p["height"], seed=seed, steps=self.p["broll_steps"],
                          lightning=self.p["broll_lightning"], models=self.models,
                          end_image=self.client.upload(end) if end else None)
        frames = self._frames(g, f"broll {out.stem}")
        media.frames_to_video(frames, W.BROLL_FPS, out)
        shutil.rmtree(frames[0].parent, ignore_errors=True)
        return out

    def music(self, tags, seconds, out, seed):
        g = W.music_graph(tags, seconds=seconds, seed=seed, models=self.models)
        files = self._frames(g, "music")
        media.run(["ffmpeg", "-i", str(files[0]), "-ar", "48000", str(out)])
        shutil.rmtree(files[0].parent, ignore_errors=True)
        return out

    def free(self):
        if self.cfg["comfy"].get("free_after_stage", True):
            self.client.free()


# --------------------------------------------------------------------------- dummy


class DummyRenderer:
    """Placeholder media with the right shapes/durations: verifies orchestration and editing end-to-end."""

    def __init__(self, cfg: Config) -> None:
        self.w, self.h = cfg.profile["width"], cfg.profile["height"]

    def keyframe(self, prompt, refs, out, seed):
        base = Image.new("RGB", (self.w, self.h), (20 + seed % 60, 24, 40))
        if refs:
            plate = ImageOps.fit(Image.open(refs[0]).convert("RGB"), (self.w, self.h)).filter(ImageFilter.GaussianBlur(3))
            base = Image.blend(base, plate, 0.6)
        x = 20
        for r in refs[1:3]:
            thumb = ImageOps.fit(Image.open(r).convert("RGB"), (self.h // 2, self.h // 2))
            base.paste(thumb, (x, self.h // 4))
            x += self.h // 2 + 20
        d = ImageDraw.Draw(base)
        words, line, y = prompt.split(), "", 10
        for word in words:
            if len(line) + len(word) > 70:
                d.text((10, y), line, fill="white")
                y += 14
                line = ""
            line += word + " "
            if y > self.h // 4 - 14:
                break
        d.text((10, y), line, fill="white")
        base.save(out)
        return out

    def _animate(self, image: Path, seconds: float, out: Path, audio: Path | None, fps: int) -> Path:
        frames = max(2, int(seconds * fps))
        vf = (f"scale={self.w * 2}:{self.h * 2},zoompan=z='min(zoom+0.0008,1.12)':d={frames}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={self.w}x{self.h}:fps={fps}")
        args = ["ffmpeg", "-loop", "1", "-i", str(image)]
        if audio:
            args += ["-i", str(audio)]
        args += ["-vf", vf, "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac", "-shortest"]
        media.run(args + [str(out)])
        return out

    def talk(self, image, audio, prompt, seconds, out, seed):
        return self._animate(image, seconds, out, audio, W.TALK_FPS)

    def broll(self, start, prompt, seconds, out, seed, end=None):
        seconds = min(seconds, (W.BROLL_MAX_FRAMES - 1) / W.BROLL_FPS)
        if end is None:
            return self._animate(start, seconds, out, None, W.BROLL_FPS)
        a, b = out.with_suffix(".a.mp4"), out.with_suffix(".b.mp4")
        self._animate(start, seconds / 2 + 0.5, a, None, W.BROLL_FPS)
        self._animate(end, seconds / 2 + 0.5, b, None, W.BROLL_FPS)
        media.run(["ffmpeg", "-i", str(a), "-i", str(b), "-filter_complex",
                   f"[0:v][1:v]xfade=transition=fade:duration=1:offset={seconds / 2 - 0.5:.2f},format=yuv420p",
                   "-c:v", "libx264", str(out)])
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)
        return out

    def music(self, tags, seconds, out, seed):
        media.run(["ffmpeg", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds:.2f}", "-f", "lavfi", "-i",
                   f"sine=frequency=277:duration={seconds:.2f}", "-filter_complex",
                   "[0][1]amix=inputs=2,volume=0.3", "-ar", "48000", str(out)])
        return out

    def free(self):
        pass


def make_renderer(cfg: Config) -> Renderer:
    backend = cfg["render"]["backend"]
    if backend == "comfy":
        return ComfyRenderer(cfg)
    if backend == "dummy":
        log.info("render backend: dummy (placeholders, no GPU)")
        return DummyRenderer(cfg)
    raise ValueError(f"unknown render backend {backend!r}")

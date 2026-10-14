"""Stage 5 — animate every shot: lip-synced talk shots, b-roll, the bridge, and the music bed."""

from __future__ import annotations

from pathlib import Path

from .. import media
from ..comfy import workflows as W
from ..log import log
from ..project import Project
from ..render import Renderer, make_renderer
from ..schema import Screenplay
from .design import run_design
from .voice import run_voice

TALK_STYLE = {
    "excited": "talking excitedly with lively expressions and hand gestures",
    "happy": "talking happily, smiling",
    "warm": "talking warmly with a gentle smile",
    "emotional": "talking emotionally, eyes glistening",
    "playful": "talking playfully, laughing a little",
    "serious": "talking thoughtfully",
}


def run_render(project: Project, sp: Screenplay | None = None, renderer: Renderer | None = None,
               only: list[str] | None = None) -> dict[str, Path]:
    sp = sp or project.load_screenplay()
    cast = project.load_cast()
    renderer = renderer or make_renderer(project.cfg)
    voices = run_voice(project, sp)
    keyframes = run_design(project, sp, renderer)
    prof = project.cfg.profile
    common = {"profile": prof, "models": project.cfg["models"], "backend": project.cfg["render"]["backend"]}
    out_dir = project.dir("shots")
    clips: dict[str, Path] = {}

    # Render grouped by model family (InfiniteTalk, then Wan2.2) so 14B checkpoints aren't swapped per shot.
    order = sorted(enumerate(sp.shots), key=lambda it: (it[1].kind != "talk", it[0]))
    for i, shot in order:
        if only and shot.id not in only:
            existing = project.artifact(f"shot/{shot.id}")
            if existing:
                clips[shot.id] = existing
            continue
        out = out_dir / f"{shot.id}.mp4"
        kf = keyframes[shot.id]
        seed = 42 + i
        if shot.kind == "talk":
            audio = voices[shot.id]
            seconds = media.audio_duration(audio)
            name = cast.member(shot.speaker).name
            prompt = (f"{name} {TALK_STYLE.get(shot.emotion, 'talking naturally')} in a {shot.framing} shot. "
                      f"{shot.motion}").strip()
            inputs = {"kf": kf, "audio": audio, "prompt": prompt, **common}
            clips[shot.id] = project.cached(
                f"shot/{shot.id}", inputs,
                lambda kf=kf, audio=audio, prompt=prompt, seconds=seconds, out=out, seed=seed:
                    renderer.talk(kf, audio, prompt, seconds, out, seed))
        elif shot.kind == "bridge":
            if not cast.last_frame:
                raise RuntimeError("bridge shot needs cast.last_frame (re-run analyze)")
            start = project.abs(cast.last_frame)
            prompt = f"{shot.motion} {shot.visual}".strip()
            inputs = {"start": start, "end": kf, "prompt": prompt, "dur": shot.duration, **common}
            clips[shot.id] = project.cached(
                f"shot/{shot.id}", inputs,
                lambda start=start, kf=kf, prompt=prompt, d=shot.duration, out=out, seed=seed:
                    renderer.broll(start, prompt, d, out, seed, end=kf))
        else:
            prompt = f"{shot.visual}. {shot.motion}".strip()
            inputs = {"kf": kf, "prompt": prompt, "dur": shot.duration, **common}
            clips[shot.id] = project.cached(
                f"shot/{shot.id}", inputs,
                lambda kf=kf, prompt=prompt, d=shot.duration, out=out, seed=seed:
                    renderer.broll(kf, prompt, d, out, seed))
        log.info(f"  ✓ {shot.id} ({shot.kind}) → {project.rel(clips[shot.id])}")

    if sp.music and project.cfg["edit"]["music"]:
        total = sum(media.probe(p).duration for p in clips.values()) + 2.0
        total = min(max(total, 10.0), 600.0)
        out = project.dir("music") / "bed.wav"
        project.cached("music/bed", {"tags": sp.music, "seconds": round(total), **common},
                       lambda: renderer.music(sp.music, round(total), out, seed=3))
    renderer.free()
    return {s.id: clips[s.id] for s in sp.shots if s.id in clips}


def estimate(sp: Screenplay) -> dict:
    """Rough on-device cost model for planning (DGX Spark, draft profile)."""
    talk_s = sum(max(2.0, len(s.line or "") / 14.0) for s in sp.shots if s.kind == "talk")
    broll = [s for s in sp.shots if s.kind in ("broll", "bridge")]
    chunks = sum(W.talk_chunks(int(max(2.0, len(s.line or "") / 14.0) * W.TALK_FPS) + 1)
                 for s in sp.shots if s.kind == "talk")
    return {"talk_seconds": round(talk_s, 1), "talk_chunks": chunks, "broll_clips": len(broll),
            "keyframes": len(sp.shots) + len(sp.sets)}

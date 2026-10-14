"""Stage 3 — synthesise every spoken line with the speaker's cloned voice."""

from __future__ import annotations

from pathlib import Path

from .. import media
from ..log import log
from ..project import Project
from ..schema import CastBible, Screenplay
from ..tts import make_tts


def voice_inputs(project: Project, cast: CastBible, shot) -> dict:
    m = cast.member(shot.speaker)
    ref = project.abs(m.voice_ref) if m.voice_ref else None
    return {"line": shot.line, "language": shot.language, "emotion": shot.emotion, "voice_ref": ref,
            "tts": {k: v for k, v in project.cfg["tts"].items() if k != "device"}}


def run_voice(project: Project, sp: Screenplay | None = None) -> dict[str, Path]:
    sp = sp or project.load_screenplay()
    cast = project.load_cast()
    out_dir = project.dir("audio")
    pad = float(project.cfg["tts"].get("pad_seconds", 0.25))
    sr = int(project.cfg["tts"].get("sample_rate", 24000))
    tts = None
    results: dict[str, Path] = {}
    try:
        for shot in sp.shots:
            if shot.kind != "talk":
                continue
            inputs = voice_inputs(project, cast, shot)

            def produce(shot=shot, inputs=inputs) -> Path:
                nonlocal tts
                if tts is None:
                    tts = make_tts(project.cfg["tts"])
                raw = out_dir / f"{shot.id}_raw.wav"
                tts.synthesize(shot.line, raw, language=shot.language, voice_ref=inputs["voice_ref"],
                               emotion=shot.emotion)
                final = out_dir / f"{shot.id}.wav"
                media.pad_audio(raw, final, pad, sr=sr)
                raw.unlink(missing_ok=True)
                return final

            results[shot.id] = project.cached(f"voice/{shot.id}", inputs, produce)
    finally:
        if tts is not None:
            tts.close()
    total = sum(media.audio_duration(p) for p in results.values())
    log.info(f"voices ready: {len(results)} lines, {total:.1f}s of dialogue")
    return results


def set_voice(project: Project, member_id: str, *, file: str | None = None, start: float | None = None,
              end: float | None = None, text: str | None = None) -> Path:
    """Attach a cloning sample to a member: an external file, or a [start, end] window of the clip's vocals."""
    cast = project.load_cast()
    m = cast.member(member_id)
    dest = project.dir("analysis/voices") / f"{member_id}.wav"
    if file:
        media.run(["ffmpeg", "-i", str(file), "-ac", "1", "-ar", "24000", "-t", "20", str(dest)])
        m.voice_ref_text = text
    else:
        if not cast.vocals:
            raise RuntimeError("no vocal stem available; supply --file instead")
        from ..analysis.audio import pick_voice_sample

        picked = pick_voice_sample(project.abs(cast.vocals), cast.transcript, dest, start=start, end=end)
        if not picked:
            raise RuntimeError("no usable speech window found; pass --start/--end or --file")
        m.voice_ref_text = text or picked[1]
    m.voice_ref = project.rel(dest)
    project.save_cast(cast)  # the new sample changes this member's voice fingerprints -> lines re-voice
    return dest

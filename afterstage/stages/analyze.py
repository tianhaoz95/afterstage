"""Stage 1 — understand the source clip and write ``cast.json``."""

from __future__ import annotations

from ..analysis import audio as audio_an
from ..analysis import faces as faces_an
from ..analysis.scenes import detect_scenes
from ..llm import ChatClient, image_part
from ..log import log
from ..media import extract_audio, last_frame, probe
from ..project import Project
from ..schema import CastBible, Member

SCENE_PROMPT = (
    "Describe this video frame in one sentence for a film editor: setting, lighting, camera framing, "
    "what the people are doing. No speculation about identities."
)
MEMBER_PROMPT = (
    "Describe this person's visible appearance for a costume/continuity sheet in one sentence: hair colour and "
    "style, outfit, accessories, makeup. Do not guess their name or identity."
)
SUMMARY_PROMPT = (
    "These are frames from one video clip in order. Reply with JSON: "
    '{"summary": "<2 sentences: what happens, what kind of event>", '
    '"style": "<1 sentence: visual style, colour palette, lighting, camera language>"}'
)


def run_analyze(project: Project, force: bool = False) -> CastBible:
    if project.cast_path.exists() and not force:
        log.info("cast.json exists — skipping analysis (use --force to redo)")
        return project.load_cast()
    cfg = project.cfg
    a = cfg["analysis"]
    src = project.source
    info = probe(src)
    log.info(f"source: {info.duration:.1f}s {info.width}x{info.height} @ {info.fps:.2f}fps")
    adir = project.dir("analysis")

    scenes = detect_scenes(src, adir / "scenes", a["scene_threshold"], a["max_scenes"], info.duration)
    for sc in scenes:
        sc.keyframe = project.rel(sc.keyframe)
    lf = last_frame(src, adir / "last_frame.png")

    vocals = None
    transcript = []
    if info.has_audio:
        wav = extract_audio(src, adir / "audio.wav")
        if a["separation_backend"] == "demucs":
            vocals = audio_an.separate_vocals(wav, adir / "separated", a["device"])
        if a["asr_backend"] == "whisper":
            transcript = audio_an.transcribe(vocals or wav, a["asr_model"], a["device"])
        vocals = vocals or wav

    people = faces_an.find_people(
        src, adir / "faces", backend=a["face_backend"], device=a["device"], sample_fps=a["face_sample_fps"],
        min_size=a["face_min_size"], threshold=a["face_cluster_threshold"], max_members=a["max_members"],
        min_screen_time=a["min_member_screen_time"],
    )
    names = project.meta.cast_names
    members: list[Member] = []
    for i, p in enumerate(people):
        members.append(Member(
            id=f"m{i + 1}",
            name=names[i] if i < len(names) else f"Member {i + 1}",
            face_refs=[project.rel(x) for x in p["face_refs"]],
            body_refs=[project.rel(x) for x in p["body_refs"]],
            screen_time=p["screen_time"],
            embedding=project.rel(p["embedding"]),
            language=cfg["writer"]["language"],
        ))
    for j in range(len(members), len(names)):  # named people we couldn't see
        members.append(Member(id=f"m{j + 1}", name=names[j], language=cfg["writer"]["language"]))

    cast = CastBible(
        source_video=project.meta.source, duration=info.duration, width=info.width, height=info.height,
        fps=info.fps, last_frame=project.rel(lf), members=members, scenes=scenes, transcript=transcript,
        vocals=project.rel(vocals) if vocals else None,
    )

    if cfg["vlm"]["backend"] == "openai":
        _caption(project, cast)
    project.save_cast(cast)
    log.info(f"analysis done: {len(scenes)} scenes, {len(members)} members, {len(transcript)} transcript segments")
    log.info(f"review/edit names & voices in {project.cast_path}")
    return cast


def _caption(project: Project, cast: CastBible) -> None:
    try:
        vlm = ChatClient(project.cfg["vlm"], project.cfg, "VLM")
        for sc in cast.scenes:
            sc.caption = vlm.chat([{"role": "user", "content": [
                {"type": "text", "text": SCENE_PROMPT}, image_part(project.abs(sc.keyframe))]}],
                temperature=0.2, max_tokens=200).strip()
        for m in cast.members:
            ref = (m.body_refs or m.face_refs or [None])[0]
            if ref:
                m.appearance = vlm.chat([{"role": "user", "content": [
                    {"type": "text", "text": MEMBER_PROMPT}, image_part(project.abs(ref))]}],
                    temperature=0.2, max_tokens=150).strip()
        picks = cast.scenes[:: max(1, len(cast.scenes) // 6)][:6]
        from ..llm import extract_json

        reply = vlm.chat([{"role": "user", "content": [{"type": "text", "text": SUMMARY_PROMPT},
                                                       *[image_part(project.abs(s.keyframe), 512) for s in picks]]}],
                         temperature=0.2, max_tokens=400)
        data = extract_json(reply)
        cast.summary = str(data.get("summary", ""))
        cast.style = str(data.get("style", ""))
        if project.cfg["llm"].get("unload_after_use", True):
            vlm.unload()
    except Exception as exc:  # captions are nice-to-have
        log.warning(f"VLM captioning skipped: {exc}")

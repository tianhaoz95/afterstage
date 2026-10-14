"""Stage 2 — turn the user's request + cast bible into ``screenplay.json`` with a local LLM."""

from __future__ import annotations

import json

from ..llm import ChatClient
from ..log import log
from ..project import Project
from ..schema import CastBible, Member, Screenplay, SetDesign, Shot

SYSTEM = """You are the head writer and director of a small production team that EXTENDS existing video clips.
You receive analysis of a source clip (who appears, what happens, visual style) and a request from the user.
You write a shot-by-shot screenplay that a generative video pipeline will render. Output JSON only.

Pipeline capabilities you must design for:
- "talk" shot: ONE cast member speaks ONE line to camera or to the others, lip-synced. Framing closeup/medium works best.
  The line is spoken by a voice clone, so write natural spoken language (no stage directions inside `line`).
  Keep each line to 1-3 sentences (about 4-12 seconds when spoken). Alternate speakers like a real conversation.
- "broll" shot: a silent 3-5 second cutaway (establishing shot, reaction, hands, set details, group laughing).
- "bridge" shot: optional, ONLY as the very first shot. It starts on the last frame of the source clip and
  transitions into the first new scene (e.g. stage lights fade, camera pulls back, cut to backstage). Its `visual`
  describes the destination frame and its `motion` describes the transition.

Rules:
- Use ONLY the cast member ids given (e.g. "m1"). `characters` lists everyone visible; `speaker` must be in it.
- Put every location in `sets` once with a rich visual description (props, lighting, colours, lens feel)
  and reference it by `set_id`. 1-3 sets is typical.
- `visual` = what the first frame looks like (who, where, framing, wardrobe continuity with the source clip,
  expressions). `motion` = camera movement + body motion, in plain English, one sentence.
- Wardrobe and hair must stay consistent with the cast appearance notes unless the request says otherwise.
- Default spoken language is given per member; you may mix in short English phrases if natural.
- Respect the requested format and length. Make it entertaining, warm, specific to what happened in the clip.
- Never write sexual, hateful, or defamatory content about real people; keep it fan-friendly.
- If the format needs someone who is NOT in the clip (interviewer, MC, podcast host), declare them in `guests`
  with ids "g1", "g2", ... a name and a vivid `appearance`; they can then appear in shots and speak.
  Never invent extra members of the group.
- `music`: one line describing an instrumental background bed (genre, tempo, mood), or "" for none.
"""


def _cast_brief(cast: CastBible, transcript_lines: int = 40) -> str:
    members = [
        {"id": m.id, "name": m.name, "language": m.language, "appearance": m.appearance or "(unknown)",
         "has_face_reference": bool(m.face_refs), "screen_time_s": m.screen_time}
        for m in cast.members
    ]
    scenes = [f"[{s.start:.0f}-{s.end:.0f}s] {s.caption}" for s in cast.scenes if s.caption]
    lines = [f"[{t.start:.0f}s] {t.text}" for t in cast.transcript[:transcript_lines]]
    return json.dumps({
        "clip_summary": cast.summary, "visual_style": cast.style, "duration_s": round(cast.duration, 1),
        "cast": members, "scenes": scenes, "transcript_excerpt": lines,
    }, ensure_ascii=False, indent=1)


def run_write(project: Project, prompt: str | None = None, force: bool = False) -> Screenplay:
    if project.screenplay_path.exists() and not force:
        log.info("screenplay.json exists — skipping (use --force to rewrite)")
        return project.load_screenplay()
    if prompt:
        project.update_prompt(prompt)
    request = project.meta.prompt
    cast = project.load_cast()
    if not cast.members:
        raise RuntimeError("cast has no members. Pass --cast names to `afterstage new` or add members to cast.json")
    w = project.cfg["writer"]
    backend = project.cfg["llm"]["backend"]
    if backend == "template":
        sp = template_screenplay(cast, request, w)
    else:
        llm = ChatClient(project.cfg["llm"], project.cfg, "LLM")
        user = (
            f"SOURCE CLIP ANALYSIS:\n{_cast_brief(cast)}\n\n"
            f"USER REQUEST: {request}\n\n"
            f"Target runtime: about {w['target_seconds']} seconds, at most {w['max_shots']} shots. "
            f"{'Start with a bridge shot.' if w['include_bridge'] else 'Do not use a bridge shot.'}\n"
            "Return the screenplay JSON."
        )
        sp = llm.structured(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            Screenplay, attempts=w["repair_attempts"], extra_check=lambda s: s.validate_against(cast),
        )
        if project.cfg["llm"].get("unload_after_use", True):
            llm.unload()
    sp = normalise(sp, cast, w)
    add_guests(project, cast, sp)
    project.save_screenplay(sp)
    talk = sum(1 for s in sp.shots if s.kind == "talk")
    log.info(f"screenplay '{sp.title}': {len(sp.shots)} shots ({talk} talk). Edit {project.screenplay_path} freely.")
    return sp


def add_guests(project: Project, cast: CastBible, sp: Screenplay) -> None:
    """Guests become cast members without reference footage, so every later stage treats them uniformly."""
    known = {m.id for m in cast.members}
    added = False
    for g in sp.guests:
        if g.id not in known:
            cast.members.append(Member(id=g.id, name=g.name, appearance=g.appearance, language=g.language))
            added = True
    if added:
        project.save_cast(cast)


def normalise(sp: Screenplay, cast: CastBible, w: dict) -> Screenplay:
    """Deterministic clean-up the LLM shouldn't be trusted with."""
    shots = sp.shots[: w["max_shots"]]
    if not w["include_bridge"]:
        shots = [s for s in shots if s.kind != "bridge"]
    seen = set()
    for i, s in enumerate(shots):
        if not s.id or s.id in seen:
            s.id = f"s{i + 1:02d}"
        seen.add(s.id)
        if s.kind == "talk" and s.speaker and s.speaker not in s.characters:
            s.characters.insert(0, s.speaker)
        if s.kind != "talk":
            s.speaker, s.line = None, None
        if s.set_id is None and sp.sets:
            s.set_id = sp.sets[0].id
        if s.kind == "talk" and s.speaker:
            guest = next((g for g in sp.guests if g.id == s.speaker), None)
            if guest and not s.language:
                s.language = guest.language
    sp.shots = shots
    return sp


# --------------------------------------------------------------------------- offline writer


def template_screenplay(cast: CastBible, request: str, w: dict) -> Screenplay:
    """A deterministic screenplay used for tests and for dry runs without an LLM server."""
    ids = [m.id for m in cast.members]
    names = {m.id: m.name for m in cast.members}
    lang = w.get("language", "ko")
    studio = SetDesign(id="studio", description=(
        "Cozy podcast studio after a concert: warm tungsten practical lights, acoustic foam panels, two large "
        "condenser microphones on boom arms, a low wooden table with water bottles, neon sign on the back wall, "
        "shallow depth of field, 35mm cinematic look"))
    shots: list[Shot] = []
    if w.get("include_bridge", True):
        shots.append(Shot(id="s01", kind="bridge", set_id="studio", characters=ids[:2], framing="wide",
                          visual="Wide shot of the cast sitting together in the podcast studio, smiling, still in stage outfits",
                          motion="Stage lights fade to warm studio light as the camera pulls back", duration=5))
    shots.append(Shot(id=f"s{len(shots) + 1:02d}", kind="broll", set_id="studio", characters=ids[:3], framing="group",
                      visual="Establishing shot of the studio with the cast settling in, laughing",
                      motion="Slow dolly in", duration=4))
    lines_ko = [
        "안녕하세요 여러분! 방금 무대 끝나고 바로 왔어요. 아직도 심장이 너무 빨리 뛰어요.",
        "오늘 팬분들 함성 진짜 대박이었어요. 마지막 곡에서 다 같이 따라 불러줘서 감동받았어요.",
        "무대 중간에 이어모니터가 빠져서 조금 당황했는데, 아무도 몰랐죠?",
        "다음 투어에서는 새로운 유닛 무대도 준비하고 있으니까 기대해 주세요!",
    ]
    lines_en = [
        "Hi everyone! We came straight here after the show and my heart is still racing.",
        "The crowd tonight was unreal. When everyone sang the last song with us, I almost cried.",
        "My in-ear slipped out in the middle of the bridge. Nobody noticed, right?",
        "We're preparing a brand new unit stage for the next tour, so stay tuned!",
    ]
    lines = lines_ko if lang == "ko" else lines_en
    for i, line in enumerate(lines):
        spk = ids[i % len(ids)]
        shots.append(Shot(id=f"s{len(shots) + 1:02d}", kind="talk", set_id="studio", characters=[spk], speaker=spk,
                          line=line, language=lang, framing="medium", emotion="excited" if i == 0 else "warm",
                          visual=f"Medium close-up of {names[spk]} at the microphone, stage makeup, relaxed smile",
                          motion="Subtle handheld drift, natural head movement and gestures"))
    shots.append(Shot(id=f"s{len(shots) + 1:02d}", kind="broll", set_id="studio", characters=ids[:3], framing="wide",
                      visual="The cast waving goodbye to the camera together", motion="Slow pull back", duration=4))
    return Screenplay(title="After the Show", format="podcast", logline=request[:200],
                      music="warm lo-fi R&B instrumental, 90 BPM, soft keys, mellow", sets=[studio], shots=shots)

"""Stage 4 — production design: an empty set plate per location, then one keyframe per shot
that composes the set with the cast's reference crops (Qwen-Image-Edit, up to 3 reference images)."""

from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np

from ..log import log
from ..project import Project
from ..render import Renderer, make_renderer
from ..schema import CastBible, Screenplay, Shot

FRAMING = {
    "closeup": "close-up portrait framing, head and shoulders",
    "medium": "medium shot from the waist up",
    "wide": "wide shot showing the whole room",
    "two_shot": "two-shot with both people in frame",
    "group": "group shot with everyone in frame",
}


def _ref_for(project: Project, cast: CastBible, member_id: str) -> Path | None:
    m = cast.member(member_id)
    for rel in (m.body_refs[:1] or []) + (m.face_refs[:1] or []):
        p = project.abs(rel)
        if p.exists():
            return p
    return None


def keyframe_prompt(cast: CastBible, shot: Shot, ref_members: list[str], has_plate: bool) -> str:
    parts = []
    idx = 1
    if has_plate:
        parts.append("Image 1 is the location.")
        idx = 2
    for mid in ref_members:
        m = cast.member(mid)
        parts.append(f"Image {idx} shows {m.name}.")
        idx += 1
    others = [cast.member(c) for c in shot.characters if c not in ref_members]
    desc = [f"{m.name}: {m.appearance}" for m in [cast.member(c) for c in shot.characters] if m.appearance]
    parts.append(f"Create a new photograph: {shot.visual}")
    parts.append(FRAMING.get(shot.framing, "") + ".")
    if ref_members:
        parts.append("Keep every person's face, hairstyle, makeup and outfit exactly as in their reference image; "
                     "natural relaxed pose, they are not on stage anymore.")
    if others:
        parts.append("Also in frame: " + ", ".join(m.name for m in others) + ".")
    if desc:
        parts.append("Appearance notes: " + "; ".join(desc) + ".")
    if cast.style:
        parts.append(f"Visual style matching the source footage: {cast.style}")
    parts.append("Photorealistic cinematic film still, 16:9, sharp focus on faces, no text, no watermark.")
    return " ".join(p for p in parts if p.strip(". "))


def _identity_score(project: Project, cast: CastBible, image: Path, members: list[str]) -> float | None:
    """Min over members of best cosine similarity between their cluster centroid and faces in the image."""
    backend = project.cfg["analysis"]["face_backend"]
    if backend != "insightface":
        return None
    from ..analysis.faces import embed_image

    embs = embed_image(image, backend, project.cfg["analysis"]["device"])
    scores = []
    for mid in members:
        m = cast.member(mid)
        if not m.embedding or not project.abs(m.embedding).exists():
            continue
        ref = np.load(project.abs(m.embedding))
        scores.append(max((float(np.dot(ref, e)) for e in embs), default=0.0))
    return min(scores) if scores else None


def run_design(project: Project, sp: Screenplay | None = None, renderer: Renderer | None = None) -> dict[str, Path]:
    sp = sp or project.load_screenplay()
    cast = project.load_cast()
    renderer = renderer or make_renderer(project.cfg)
    prof = project.cfg.profile
    rcfg = project.cfg["render"]
    common = {"profile": prof, "models": project.cfg["models"], "backend": rcfg["backend"]}

    plates: dict[str, Path] = {}
    for s in sp.sets:
        prompt = (f"{s.description}. Empty location with no people, establishing shot. "
                  f"{cast.style} Photorealistic cinematic film still, 16:9, no text.")
        out = project.dir("sets") / f"{s.id}.png"
        plates[s.id] = project.cached(f"set/{s.id}", {"prompt": prompt, **common},
                                      lambda prompt=prompt, out=out: renderer.keyframe(prompt, [], out, seed=7))

    keyframes: dict[str, Path] = {}
    for shot in sp.shots:
        plate = plates.get(shot.set_id or "")
        ref_members = [c for c in shot.characters if _ref_for(project, cast, c)][:2 if plate else 3]
        if shot.speaker and shot.speaker in shot.characters and shot.speaker not in ref_members and _ref_for(project, cast, shot.speaker):
            ref_members = [shot.speaker] + ref_members[:-1] if ref_members else [shot.speaker]
        refs = ([plate] if plate else []) + [_ref_for(project, cast, c) for c in ref_members]
        prompt = keyframe_prompt(cast, shot, ref_members, plate is not None)
        out = project.dir("keyframes") / f"{shot.id}.png"
        inputs = {"prompt": prompt, "refs": refs, **common}

        def produce(shot=shot, prompt=prompt, refs=refs, out=out, ref_members=ref_members) -> Path:
            best_score, best_path = None, None
            tries = 1 + (rcfg["identity_retries"] if rcfg["identity_check"] and ref_members else 0)
            for attempt in range(tries):
                cand = out.with_name(f"{out.stem}_try{attempt}.png")
                renderer.keyframe(prompt, refs, cand, seed=1000 + attempt * 7919 + zlib.crc32(shot.id.encode()) % 1000)
                score = _identity_score(project, cast, cand, ref_members) if rcfg["identity_check"] else None
                if score is None:
                    best_path = cand
                    break
                log.info(f"  {shot.id} try {attempt}: identity {score:.2f}")
                if best_score is None or score > best_score:
                    best_score, best_path = score, cand
                if score >= rcfg["identity_threshold"]:
                    break
            best_path.replace(out)
            for f in out.parent.glob(f"{out.stem}_try*.png"):
                f.unlink()
            return out

        keyframes[shot.id] = project.cached(f"keyframe/{shot.id}", inputs, produce)
    renderer.free()
    log.info(f"design done: {len(plates)} set plates, {len(keyframes)} keyframes in {project.root / 'keyframes'}")
    return keyframes

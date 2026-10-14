"""Data model shared by every stage.

Two documents drive the whole pipeline and are stored as editable JSON in the project dir:

* ``cast.json``       – CastBible: what the analysis stage learned about the source clip.
* ``screenplay.json`` – Screenplay: what the writer stage decided to produce.

Everything downstream (voices, keyframes, video, edit) is derived from these two files, so a
human can edit either one and re-run; only the affected shots are regenerated (see ``project.py``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ShotKind = Literal["talk", "broll", "bridge"]
Framing = Literal["closeup", "medium", "wide", "two_shot", "group"]


# --------------------------------------------------------------------------- analysis


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    language: str | None = None
    speaker: str | None = None


class SceneInfo(BaseModel):
    index: int
    start: float
    end: float
    keyframe: str  # path relative to project dir
    caption: str = ""


class Member(BaseModel):
    """A person in the source clip (a face cluster), editable by the user."""

    id: str  # stable id, e.g. "m1"
    name: str  # display name the writer uses ("Ari"); user can rename
    face_refs: list[str] = Field(default_factory=list)  # tight face crops, best first
    body_refs: list[str] = Field(default_factory=list)  # upper-body crops (outfit reference)
    appearance: str = ""  # VLM description: hair, outfit, ...
    voice_ref: str | None = None  # clean speech sample used for voice cloning
    voice_ref_text: str | None = None
    language: str = "ko"
    screen_time: float = 0.0  # seconds the face was visible (used for ranking)
    embedding: str | None = None  # path to .npy mean face embedding


class CastBible(BaseModel):
    source_video: str
    duration: float
    width: int
    height: int
    fps: float
    summary: str = ""
    style: str = ""  # visual style summary used to keep generated shots on-look
    last_frame: str | None = None  # last frame of the clip, used for the bridge shot
    members: list[Member] = Field(default_factory=list)
    scenes: list[SceneInfo] = Field(default_factory=list)
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    vocals: str | None = None  # separated vocal stem of the source audio

    def member(self, member_id: str) -> Member:
        for m in self.members:
            if m.id == member_id:
                return m
        raise KeyError(f"unknown member id {member_id!r}; known: {[m.id for m in self.members]}")


# --------------------------------------------------------------------------- screenplay


class SetDesign(BaseModel):
    id: str
    description: str  # what the location looks like, used to render a set plate


class Guest(BaseModel):
    """Someone the format needs who is not in the source clip (interviewer, MC, podcast host)."""

    id: str  # must start with "g": g1, g2, ...
    name: str
    appearance: str = ""  # used to render them, since there is no reference footage
    language: str = "ko"


class Shot(BaseModel):
    id: str
    kind: ShotKind
    set_id: str | None = None
    characters: list[str] = Field(default_factory=list)  # member ids visible in frame
    speaker: str | None = None  # member id (talk shots)
    line: str | None = None  # spoken text (talk shots)
    language: str = "ko"
    emotion: str = "neutral"
    framing: Framing = "medium"
    visual: str = ""  # keyframe description (composition, lighting, wardrobe)
    motion: str = ""  # camera / action description for the video model
    duration: float = 4.0  # seconds; for talk shots the audio length wins

    @field_validator("duration")
    @classmethod
    def _clamp_duration(cls, v: float) -> float:
        return float(min(max(v, 1.0), 20.0))


class Screenplay(BaseModel):
    title: str
    format: str = "podcast"  # free text: podcast / interview / behind-the-scenes / continuation
    logline: str = ""
    music: str = ""  # prompt for the background music bed; empty = no music
    sets: list[SetDesign] = Field(default_factory=list)
    guests: list[Guest] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)

    def set(self, set_id: str | None) -> SetDesign | None:
        for s in self.sets:
            if s.id == set_id:
                return s
        return None

    def validate_against(self, cast: CastBible) -> list[str]:
        """Return human-readable problems (empty list = ok). Used by the writer's repair loop."""
        problems: list[str] = []
        member_ids = {m.id for m in cast.members}
        ids = member_ids | {g.id for g in self.guests}
        for g in self.guests:
            if not g.id.startswith("g") or (g.id in member_ids and cast.member(g.id).name != g.name):
                problems.append(f"guest id {g.id!r} must start with 'g' and not clash with cast ids")
        set_ids = {s.id for s in self.sets}
        seen: set[str] = set()
        if not self.shots:
            problems.append("screenplay has no shots")
        for sh in self.shots:
            if sh.id in seen:
                problems.append(f"duplicate shot id {sh.id}")
            seen.add(sh.id)
            for c in sh.characters:
                if c not in ids:
                    problems.append(f"shot {sh.id}: unknown character id {c!r} (use one of {sorted(ids)})")
            if sh.set_id and sh.set_id not in set_ids:
                problems.append(f"shot {sh.id}: unknown set_id {sh.set_id!r} (define it in sets)")
            if sh.kind == "talk":
                if not sh.speaker or sh.speaker not in ids:
                    problems.append(f"shot {sh.id}: talk shot needs speaker from {sorted(ids)}")
                if not sh.line or not sh.line.strip():
                    problems.append(f"shot {sh.id}: talk shot needs a non-empty line")
                elif len(sh.line) > 400:
                    problems.append(f"shot {sh.id}: line too long ({len(sh.line)} chars, max 400); split it")
        bridges = [s for s in self.shots if s.kind == "bridge"]
        if len(bridges) > 1:
            problems.append("at most one bridge shot is allowed")
        if bridges and self.shots[0].kind != "bridge":
            problems.append("the bridge shot must be the first shot")
        return problems

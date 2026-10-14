"""End-to-end orchestration. Each stage is idempotent and cached, so `run` can be re-invoked after
any manual edit (cast names, voice samples, screenplay lines, config) and only redoes what changed."""

from __future__ import annotations

from pathlib import Path

from .editor import run_edit
from .log import log
from .project import Project
from .stages.analyze import run_analyze
from .stages.render import estimate, run_render
from .stages.write import run_write

STAGES = ["analyze", "write", "render", "edit"]


def run_all(project: Project, *, until: str = "edit", pause_after_write: bool = False) -> Path | None:
    if until not in STAGES:
        raise ValueError(f"until must be one of {STAGES}")
    run_analyze(project)
    if until == "analyze":
        return None
    sp = run_write(project)
    est = estimate(sp)
    log.info(f"plan: {est['keyframes']} keyframes, {est['talk_chunks']} talk windows "
             f"(~{est['talk_seconds']}s dialogue), {est['broll_clips']} b-roll clips")
    if until == "write" or pause_after_write:
        log.info(f"review {project.screenplay_path}, then run `afterstage run {project.root}` again")
        return None
    clips = run_render(project, sp)
    if until == "render":
        return None
    return run_edit(project, sp, clips)

"""afterstage command line.

    afterstage new  shows/after-party  --clip concert.mp4 --prompt "..." --cast "Ari,Bora,Chae,Dami"
    afterstage run  shows/after-party            # analyze → write → voices → keyframes → video → edit
    afterstage cast shows/after-party            # inspect who was found; rename / attach voice samples
    afterstage render shows/after-party --shot s04   # re-render one shot after editing screenplay.json
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config, parse_overrides
from .project import Project

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Extend a video clip with on-device generative AI.")
models_app = typer.Typer(no_args_is_help=True, help="Manage ComfyUI model weights.")
app.add_typer(models_app, name="models")
console = Console()

_state: dict = {"config": None, "set": []}


@app.callback()
def main(
    config: Path | None = typer.Option(None, "--config", "-c", help="YAML config overriding defaults."),
    set_: list[str] = typer.Option([], "--set", "-s", help="Override a config key, e.g. -s render.profile=final"),
    profile: str | None = typer.Option(None, "--profile", help="Render profile (draft|final)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="No GPU/servers: template writer, dummy TTS and renderer."),
) -> None:
    sets = list(set_)
    if profile:
        sets.append(f"render.profile={profile}")
    if dry_run:
        sets += ["llm.backend=template", "vlm.backend=none", "tts.backend=dummy", "render.backend=dummy",
                 "analysis.asr_backend=none", "analysis.separation_backend=none", "analysis.face_backend=opencv",
                 "analysis.device=cpu", "render.identity_check=false"]
    _state["config"] = config
    _state["set"] = sets


def _project(path: Path) -> Project:
    return Project(path, user_config=_state["config"], overrides=parse_overrides(_state["set"]))


# --------------------------------------------------------------------------- lifecycle


@app.command()
def new(
    path: Path = typer.Argument(..., help="Project directory to create."),
    clip: Path = typer.Option(..., "--clip", help="Source video clip."),
    prompt: str = typer.Option(..., "--prompt", "-p", help="What to generate, e.g. 'a podcast where they ...'"),
    cast: str = typer.Option("", "--cast", help="Comma-separated names, in order of screen time (optional)."),
) -> None:
    """Create a project from a clip and a request."""
    names = [n.strip() for n in cast.split(",") if n.strip()]
    p = Project.create(path, clip, prompt, names, user_config=_state["config"],
                       overrides=parse_overrides(_state["set"]))
    console.print(f"[green]created[/] {p.root}")


@app.command()
def make(
    clip: Path = typer.Argument(..., help="Source video clip."),
    prompt: str = typer.Option(..., "--prompt", "-p"),
    out: Path = typer.Option(Path("projects/run"), "--out", "-o", help="Project directory."),
    cast: str = typer.Option("", "--cast"),
    review: bool = typer.Option(False, "--review", help="Stop after writing the screenplay for human review."),
) -> None:
    """One shot: create a project and run every stage."""
    from .pipeline import run_all

    names = [n.strip() for n in cast.split(",") if n.strip()]
    if not (out / "project.json").exists():
        Project.create(out, clip, prompt, names, user_config=_state["config"], overrides=parse_overrides(_state["set"]))
    result = run_all(_project(out), pause_after_write=review)
    if result:
        console.print(f"[bold green]done:[/] {result}")


@app.command()
def run(
    path: Path,
    until: str = typer.Option("edit", help="Stop after stage: analyze|write|render|edit"),
    review: bool = typer.Option(False, "--review", help="Stop after writing the screenplay."),
) -> None:
    """Run (or resume) all stages; cached work is reused."""
    from .pipeline import run_all

    result = run_all(_project(path), until=until, pause_after_write=review)
    if result:
        console.print(f"[bold green]done:[/] {result}")


@app.command()
def analyze(path: Path, force: bool = typer.Option(False, "--force")) -> None:
    """Scenes, faces → cast members, vocals, transcript, captions → cast.json."""
    from .stages.analyze import run_analyze

    run_analyze(_project(path), force=force)
    cast(path)


@app.command()
def write(
    path: Path,
    prompt: str | None = typer.Option(None, "--prompt", "-p", help="Replace the request before writing."),
    force: bool = typer.Option(False, "--force", help="Rewrite even if screenplay.json exists."),
) -> None:
    """Write screenplay.json with the local LLM."""
    from .stages.write import run_write

    sp = run_write(_project(path), prompt=prompt, force=force or bool(prompt))
    _print_screenplay(sp)


@app.command()
def voice(path: Path) -> None:
    """Synthesize all dialogue lines."""
    from .stages.voice import run_voice

    run_voice(_project(path))


@app.command()
def design(path: Path, shot: list[str] = typer.Option([], "--shot", help="Force re-roll of these shots.")) -> None:
    """Render set plates and per-shot keyframes (review them in <project>/keyframes)."""
    from .stages.design import run_design

    p = _project(path)
    p.force |= {f"keyframe/{s}" for s in shot}
    run_design(p)


@app.command()
def render(
    path: Path,
    shot: list[str] = typer.Option([], "--shot", help="Only (re)render these shot ids."),
    force: bool = typer.Option(False, "--force", help="Ignore cache for the selected shots."),
) -> None:
    """Voices + keyframes + video for every shot (cached)."""
    from .stages.render import run_render

    p = _project(path)
    if force:
        targets = shot or [s.id for s in p.load_screenplay().shots]
        p.force |= {f"shot/{s}" for s in targets}
    run_render(p, only=shot or None)


@app.command()
def edit(path: Path) -> None:
    """Assemble the final cut from rendered shots."""
    from .editor import run_edit

    console.print(f"[bold green]final:[/] {run_edit(_project(path))}")


# --------------------------------------------------------------------------- cast management


@app.command()
def cast(path: Path) -> None:
    """Show detected cast members."""
    p = _project(path)
    c = p.load_cast()
    t = Table(title=f"cast — {p.root.name}")
    for col in ("id", "name", "screen s", "faces", "voice", "appearance"):
        t.add_column(col)
    for m in c.members:
        t.add_row(m.id, m.name, f"{m.screen_time:.1f}", str(len(m.face_refs)), m.voice_ref or "—", (m.appearance or "")[:60])
    console.print(t)
    console.print(f"face crops: {p.root / 'analysis/faces'}")


@app.command()
def rename(path: Path, member: str, name: str) -> None:
    """Give a detected member their real name (used by the writer)."""
    p = _project(path)
    c = p.load_cast()
    c.member(member).name = name
    p.save_cast(c)
    console.print(f"{member} → {name}")


@app.command("set-voice")
def set_voice_cmd(
    path: Path,
    member: str,
    file: Path | None = typer.Option(None, "--file", help="Clean speech sample (5-20 s)."),
    start: float | None = typer.Option(None, "--start", help="Or: window start (s) in the source clip."),
    end: float | None = typer.Option(None, "--end"),
    text: str | None = typer.Option(None, "--text", help="Transcript of the sample (optional)."),
) -> None:
    """Attach a voice-cloning sample to a member."""
    from .stages.voice import set_voice

    dest = set_voice(_project(path), member, file=str(file) if file else None, start=start, end=end, text=text)
    console.print(f"voice sample for {member}: {dest}")


@app.command()
def status(path: Path) -> None:
    """Show which artifacts exist for each shot."""
    p = _project(path)
    sp = p.load_screenplay()
    t = Table(title=sp.title)
    for col in ("shot", "kind", "speaker", "voice", "keyframe", "video", "line / visual"):
        t.add_column(col)
    ok = lambda k: "✓" if p.artifact(k) else "·"
    for s in sp.shots:
        t.add_row(s.id, s.kind, s.speaker or "", ok(f"voice/{s.id}") if s.kind == "talk" else "", ok(f"keyframe/{s.id}"),
                  ok(f"shot/{s.id}"), (s.line or s.visual)[:70])
    console.print(t)


# --------------------------------------------------------------------------- infrastructure


@app.command()
def doctor(comfy_dir: Path | None = typer.Option(None, "--comfy-dir", envvar="COMFYUI_DIR")) -> None:
    """Check ffmpeg, the local LLM/VLM, ComfyUI, model files and workflow compatibility."""
    import httpx

    from .comfy import workflows as W
    from .comfy.client import ComfyClient
    from .comfy.graph import validate_prompt
    from .config import ensure_local

    cfg = load_config(_state["config"], overrides=parse_overrides(_state["set"]))
    good = True

    def check(name: str, fn) -> bool:
        nonlocal good
        try:
            msg = fn()
            console.print(f"[green]✓[/] {name}" + (f" — {msg}" if msg else ""))
            return True
        except Exception as exc:  # noqa: BLE001
            good = False
            console.print(f"[red]✗[/] {name} — {exc}")
            return False

    def ffmpeg_check():
        if not shutil.which("ffmpeg"):
            raise RuntimeError("not on PATH")
        return shutil.which("ffmpeg")

    check("ffmpeg", ffmpeg_check)

    def llm_check(section: str):
        s = cfg[section]
        if s["backend"] != "openai":
            return f"backend={s['backend']}"
        ensure_local(s["base_url"], cfg, section)
        r = httpx.get(s["base_url"].rstrip("/") + "/models", timeout=10)
        r.raise_for_status()
        ids = [m.get("id") for m in (r.json().get("data") or [])]
        if s["model"] not in ids:
            raise RuntimeError(f"model {s['model']!r} not served (have {ids[:8]}); e.g. `ollama pull {s['model']}`")
        return s["model"]

    check("LLM", lambda: llm_check("llm"))
    check("VLM", lambda: llm_check("vlm"))

    def tts_check():
        if cfg["tts"]["backend"] == "chatterbox":
            import chatterbox  # noqa: F401
        return cfg["tts"]["backend"]

    check("TTS", tts_check)

    if cfg["render"]["backend"] == "comfy":
        client = ComfyClient(cfg["comfy"]["url"])

        def comfy_check():
            ensure_local(cfg["comfy"]["url"], cfg, "ComfyUI")
            stats = client.ping()
            dev = stats.get("devices", [{}])[0]
            return f"{dev.get('name', '?')} vram={dev.get('vram_total', 0) / 2**30:.0f}GB"

        comfy_up = check("ComfyUI", comfy_check)
        m = cfg["models"]
        p = cfg.profile
        graphs = {
            "keyframe": W.keyframe_graph("x", ["a.png"], width=p["width"], height=p["height"], seed=1,
                                         steps=p["keyframe_steps"], cfg=p["keyframe_cfg"],
                                         lightning=p["keyframe_lightning"], models=m),
            "talk": W.talk_graph("a.png", "a.wav", "x", seconds=6, width=p["width"], height=p["height"], seed=1,
                                 steps=p["talk_steps"], models=m),
            "broll": W.broll_graph("a.png", "x", seconds=4, width=p["width"], height=p["height"], seed=1,
                                   steps=p["broll_steps"], lightning=p["broll_lightning"], models=m, end_image="b.png"),
            "music": W.music_graph("x", seconds=30, seed=1, models=m),
        }
        for name, g in graphs.items() if comfy_up else []:
            def wf_check(g=g):
                errs = [e for e in validate_prompt(g.to_prompt(), client.object_info(), check_files=True)
                        if "a.png" not in e and "b.png" not in e and "a.wav" not in e]
                if errs:
                    raise RuntimeError("; ".join(errs[:4]))
            check(f"workflow {name}", wf_check)

    if comfy_dir:
        from .models import status as mstatus

        missing = [k for k, _, ok in mstatus(cfg, comfy_dir) if not ok]

        def files_check():
            if missing:
                raise RuntimeError(f"{len(missing)} missing: {missing} → `afterstage models download`")
            return "all present"

        check("model files", files_check)
    raise typer.Exit(0 if good else 1)


@models_app.command("list")
def models_list(comfy_dir: Path = typer.Option(..., "--comfy-dir", envvar="COMFYUI_DIR")) -> None:
    from .models import status as mstatus

    cfg = load_config(_state["config"], overrides=parse_overrides(_state["set"]))
    for key, path, ok in mstatus(cfg, comfy_dir):
        console.print(f"{'[green]✓' if ok else '[red]✗'}[/] {key:20s} {path}")


@models_app.command("download")
def models_download(
    comfy_dir: Path = typer.Option(..., "--comfy-dir", envvar="COMFYUI_DIR"),
    groups: str = typer.Option("", help="Comma list of keyframe,talk,broll,music (default all)."),
) -> None:
    from .models import download

    cfg = load_config(_state["config"], overrides=parse_overrides(_state["set"]))
    download(cfg, comfy_dir, {g.strip() for g in groups.split(",") if g.strip()} or None)


@app.command()
def ui(
    root: Path = typer.Option(Path("projects"), help="Directory holding projects."),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(7860),
) -> None:
    """Launch the local web UI (Gradio)."""
    from .ui.app import launch

    launch(root, host=host, port=port, config=_state["config"], sets=_state["set"])


def _print_screenplay(sp) -> None:
    t = Table(title=f"{sp.title} ({sp.format})")
    for col in ("id", "kind", "speaker", "line / visual"):
        t.add_column(col)
    for s in sp.shots:
        t.add_row(s.id, s.kind, s.speaker or "", s.line or s.visual)
    console.print(t)


if __name__ == "__main__":
    app()

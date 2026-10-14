"""Local web UI (Gradio) over the same project/stage API as the CLI.

Flow: New project → Analyze → fix Cast (names, voice samples) → Write / edit Script → Keyframes
(review, re-roll) → Render → Final. Every step is cached, so jumping back and forth is cheap.
Binds to 127.0.0.1 by default; reach it from a laptop with `ssh -L 7860:localhost:7860 spark`.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import gradio as gr

from ..config import parse_overrides
from ..project import Project
from ..schema import Screenplay


def launch(root: Path, *, host: str = "127.0.0.1", port: int = 7860, config: Path | None = None,
           sets: list[str] | None = None) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    overrides = parse_overrides(sets or [])

    def projects() -> list[str]:
        return sorted(p.parent.name for p in root.glob("*/project.json"))

    def open_project(name: str, profile: str | None = None) -> Project:
        if not name:
            raise gr.Error("pick or create a project first")
        ov = dict(overrides)
        if profile:
            ov = {**ov, "render": {**ov.get("render", {}), "profile": profile}}
        return Project(root / name, user_config=config, overrides=ov)

    def guard(fn):
        def wrapped(*args):
            try:
                return fn(*args)
            except gr.Error:
                raise
            except Exception as exc:  # surface stage failures in the UI instead of a blank error
                traceback.print_exc()
                raise gr.Error(f"{type(exc).__name__}: {exc}") from exc
        return wrapped

    # ------------------------------------------------------------------ handlers

    @guard
    def create(name, clip, prompt, cast_names):
        if not name or not clip or not prompt:
            raise gr.Error("name, clip and prompt are required")
        names = [n.strip() for n in (cast_names or "").split(",") if n.strip()]
        Project.create(root / name, clip, prompt, names, user_config=config, overrides=overrides)
        return gr.update(choices=projects(), value=name), f"created {name}"

    @guard
    def analyze(name, force):
        from ..stages.analyze import run_analyze

        run_analyze(open_project(name), force=force)
        return (*load_cast(name), "analysis done")

    def load_cast(name):
        p = open_project(name)
        if not p.cast_path.exists():
            return [], "[]"
        c = p.load_cast()
        gallery = [(str(p.abs(m.face_refs[0])), f"{m.id}: {m.name}") for m in c.members if m.face_refs]
        rows = [{"id": m.id, "name": m.name, "screen_time": m.screen_time, "voice_ref": m.voice_ref,
                 "appearance": m.appearance} for m in c.members]
        return gallery, json.dumps(rows, ensure_ascii=False, indent=1)

    @guard
    def rename(name, member, new_name):
        p = open_project(name)
        c = p.load_cast()
        c.member(member.strip()).name = new_name.strip()
        p.save_cast(c)
        return (*load_cast(name), f"{member} → {new_name}")

    @guard
    def voice(name, member, file, start, end):
        from ..stages.voice import set_voice

        dest = set_voice(open_project(name), member.strip(), file=file, start=start if file is None else None,
                         end=end if file is None else None)
        return (*load_cast(name), str(dest), f"voice sample set for {member}")

    @guard
    def write(name, prompt, force):
        from ..stages.write import run_write

        p = open_project(name)
        sp = run_write(p, prompt=prompt or None, force=force or bool(prompt))
        return sp.model_dump_json(indent=2), "screenplay written"

    def load_script(name):
        p = open_project(name)
        return p.screenplay_path.read_text() if p.screenplay_path.exists() else ""

    @guard
    def save_script(name, text):
        p = open_project(name)
        sp = Screenplay.model_validate_json(text)
        problems = sp.validate_against(p.load_cast())
        if problems:
            raise gr.Error("; ".join(problems[:6]))
        p.save_screenplay(sp)
        return "saved — changed shots will re-render"

    @guard
    def keyframes(name, profile, reroll):
        from ..stages.design import run_design

        p = open_project(name, profile)
        p.force |= {f"keyframe/{s.strip()}" for s in (reroll or "").split(",") if s.strip()}
        kfs = run_design(p)
        return [(str(v), k) for k, v in kfs.items()], "keyframes ready"

    @guard
    def render(name, profile, only):
        from ..editor import run_edit
        from ..stages.render import run_render

        p = open_project(name, profile)
        shots = [s.strip() for s in (only or "").split(",") if s.strip()]
        if shots:
            p.force |= {f"shot/{s}" for s in shots}
        clips = run_render(p, only=shots or None)
        final = run_edit(p, clips=clips)
        return str(final), f"final cut: {final}"

    # ------------------------------------------------------------------ layout

    with gr.Blocks(title="afterstage") as demo:
        gr.Markdown("## afterstage — extend a clip with on-device AI\nEverything runs on this machine. "
                    "Output is watermarked as AI-generated.")
        status = gr.Textbox(label="status", interactive=False)
        with gr.Row():
            project = gr.Dropdown(choices=projects(), label="project", allow_custom_value=False)
            profile = gr.Radio(["draft", "final"], value="draft", label="render profile")

        with gr.Tab("1 · New"):
            n_name = gr.Textbox(label="project name", placeholder="after-party")
            n_clip = gr.File(label="source clip", file_types=["video"], type="filepath")
            n_prompt = gr.Textbox(label="what should happen next?", lines=3,
                                  placeholder="A cosy podcast right after the concert where the members talk about "
                                              "their favourite moments and a mistake nobody noticed")
            n_cast = gr.Textbox(label="cast names (optional, comma-separated, by screen time)")
            gr.Button("create", variant="primary").click(create, [n_name, n_clip, n_prompt, n_cast], [project, status])

        with gr.Tab("2 · Cast"):
            force_an = gr.Checkbox(label="re-analyze from scratch")
            an_btn = gr.Button("analyze clip", variant="primary")
            faces = gr.Gallery(label="detected members", columns=6, height=260)
            cast_json = gr.Code(label="cast", language="json", interactive=False)
            with gr.Row():
                r_id = gr.Textbox(label="member id", placeholder="m1")
                r_name = gr.Textbox(label="real name")
                r_btn = gr.Button("rename")
            with gr.Row():
                v_file = gr.Audio(label="clean speech sample (5–20 s)", type="filepath")
                v_start = gr.Number(label="…or window start (s) in clip")
                v_end = gr.Number(label="window end (s)")
                v_btn = gr.Button("set voice")
            v_out = gr.Textbox(label="voice file", interactive=False)
            an_btn.click(analyze, [project, force_an], [faces, cast_json, status])
            r_btn.click(rename, [project, r_id, r_name], [faces, cast_json, status])
            v_btn.click(voice, [project, r_id, v_file, v_start, v_end], [faces, cast_json, v_out, status])

        with gr.Tab("3 · Script"):
            w_prompt = gr.Textbox(label="new request (optional; rewrites the script)", lines=2)
            w_force = gr.Checkbox(label="rewrite even if a script exists")
            w_btn = gr.Button("write with local LLM", variant="primary")
            script = gr.Code(label="screenplay.json (editable)", language="json", lines=30)
            s_btn = gr.Button("validate & save")
            w_btn.click(write, [project, w_prompt, w_force], [script, status])
            s_btn.click(save_script, [project, script], [status])

        with gr.Tab("4 · Keyframes"):
            k_reroll = gr.Textbox(label="re-roll shot ids (comma-separated, optional)")
            k_btn = gr.Button("render keyframes", variant="primary")
            k_gallery = gr.Gallery(label="keyframes", columns=4, height=480)
            k_btn.click(keyframes, [project, profile, k_reroll], [k_gallery, status])

        with gr.Tab("5 · Render"):
            x_only = gr.Textbox(label="only (re)render these shot ids (optional)")
            x_btn = gr.Button("render video + edit", variant="primary")
            final = gr.Video(label="final cut")
            x_btn.click(render, [project, profile, x_only], [final, status])

        project.change(lambda n: (*load_cast(n), load_script(n)) if n else ([], "[]", ""), [project],
                       [faces, cast_json, script])

    demo.queue(default_concurrency_limit=1).launch(server_name=host, server_port=port, allowed_paths=[str(root)])

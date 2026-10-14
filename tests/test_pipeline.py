"""End-to-end run with the offline backends (template writer, dummy TTS, dummy renderer)."""

import json
import shutil
from pathlib import Path

import httpx
import pytest

from afterstage import media
from afterstage.comfy import workflows as W
from afterstage.comfy.client import ComfyClient
from afterstage.config import load_config, parse_overrides
from afterstage.pipeline import run_all
from afterstage.project import Project

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

DRY = ["llm.backend=template", "vlm.backend=none", "tts.backend=dummy", "render.backend=dummy",
       "analysis.asr_backend=none", "analysis.separation_backend=none", "analysis.face_backend=none",
       "analysis.device=cpu", "render.identity_check=false", "edit.width=320", "edit.height=180",
       "render.profiles.draft.width=320", "render.profiles.draft.height=180", "edit.burn_subtitles=true"]


@pytest.fixture()
def clip(tmp_path):
    out = tmp_path / "clip.mp4"
    media.run(["ffmpeg", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=3", "-f", "lavfi", "-i", "sine=f=330:d=3",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)])
    return out


def test_dry_run_end_to_end_and_incremental(tmp_path, clip):
    overrides = parse_overrides(DRY)
    Project.create(tmp_path / "proj", clip, "a podcast about the show", ["A", "B"], overrides=overrides)
    project = Project(tmp_path / "proj", overrides=overrides)
    final = run_all(project)
    assert final and final.exists()
    info = media.probe(final)
    assert info.width == 320 and info.has_audio
    sp = project.load_screenplay()
    talk = [s for s in sp.shots if s.kind == "talk"]
    assert len(talk) >= 2
    assert final.with_suffix(".srt").read_text().count("-->") == len(talk)
    assert info.duration > 3.0  # source clip + generated segment

    # second run regenerates nothing
    state_before = json.loads((project.root / "state.json").read_text())
    run_all(Project(tmp_path / "proj", overrides=overrides))
    state_after = json.loads((project.root / "state.json").read_text())
    assert {k: v["fp"] for k, v in state_before.items()} == {k: v["fp"] for k, v in state_after.items()}
    assert all(state_before[k]["at"] == state_after[k]["at"] for k in state_before)

    # editing one line re-voices and re-renders only that shot
    target = talk[1]
    target.line = "완전히 새로운 대사입니다."
    project.save_screenplay(sp)
    run_all(Project(tmp_path / "proj", overrides=overrides))
    state_new = json.loads((project.root / "state.json").read_text())
    changed = {k for k in state_new if state_new[k]["at"] != state_after.get(k, {}).get("at")}
    must = {f"voice/{target.id}", f"shot/{target.id}", f"conform/{target.id}"}
    assert must <= changed <= must | {"music/bed"}  # keyframes and other shots are untouched


def test_comfy_client_roundtrip(tmp_path):
    """ComfyClient against a fake ComfyUI: validate → upload → queue → poll → download."""
    object_info = json.loads((Path(__file__).parent / "fixtures" / "object_info.json").read_text())
    models = load_config()["models"]
    # make the fake server's model lists contain our files so check_files=True passes
    for spec in object_info.values():
        for name, s in {**spec["input"].get("required", {}), **spec["input"].get("optional", {})}.items():
            if name in ("unet_name", "lora_name", "clip_name", "vae_name", "name", "audio_encoder_name",
                        "clip_name1", "clip_name2", "image", "audio"):
                opts = list(models.values()) + ["up.png", "up.wav"]
                if isinstance(s[0], list):
                    s[0] = opts
                else:
                    s[1]["options"] = opts
    calls = {"history": 0}

    def handler(req: httpx.Request):
        path = req.url.path
        if path == "/object_info":
            return httpx.Response(200, json=object_info)
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "up.png" if b".png" in req.content else "up.wav"})
        if path == "/prompt":
            prompt = json.loads(req.content)["prompt"]
            assert any(n["class_type"] == "WanInfiniteTalkToVideo" for n in prompt.values())
            calls["out"] = next(k for k, n in prompt.items() if n.get("_meta", {}).get("title") == W.OUTPUT)
            return httpx.Response(200, json={"prompt_id": "p1"})
        if path == "/history/p1":
            calls["history"] += 1
            if calls["history"] < 2:
                return httpx.Response(200, json={})
            out_id = calls["out"]
            return httpx.Response(200, json={"p1": {"status": {"status_str": "success", "completed": True},
                                                     "outputs": {out_id: {"images": [
                                                         {"filename": "talk_00001_.png", "subfolder": "", "type": "output"},
                                                         {"filename": "talk_00002_.png", "subfolder": "", "type": "output"}]}}}})
        if path == "/view":
            return httpx.Response(200, content=b"png-bytes")
        return httpx.Response(404)

    c = ComfyClient("http://127.0.0.1:8188")
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    import afterstage.comfy.client as client_mod

    client_mod.time.sleep = lambda s: None
    img, wav = tmp_path / "a.png", tmp_path / "a.wav"
    img.write_bytes(b"x")
    wav.write_bytes(b"y")
    g = W.talk_graph(c.upload(img), c.upload(wav), "x", seconds=2, width=320, height=192, seed=1, steps=4, models=models)
    files = c.run(g, tmp_path / "out", "t")
    assert [f.name for f in files] == ["talk_00001_.png", "talk_00002_.png"]

import json
from pathlib import Path

import pytest

from afterstage.comfy import workflows as W
from afterstage.comfy.graph import validate_prompt
from afterstage.config import load_config

OBJECT_INFO = json.loads((Path(__file__).parent / "fixtures" / "object_info.json").read_text())
MODELS = load_config()["models"]


def _graphs():
    return {
        "keyframe_refs": W.keyframe_graph("p", ["a.png", "b.png", "c.png"], width=832, height=480, seed=1, steps=4,
                                          cfg=1.0, lightning=True, models=MODELS),
        "keyframe_t2i": W.keyframe_graph("p", [], width=1280, height=720, seed=1, steps=40, cfg=4.0,
                                         lightning=False, models=MODELS),
        "talk_short": W.talk_graph("a.png", "a.wav", "x", seconds=2.0, width=832, height=480, seed=1, steps=6,
                                   models=MODELS),
        "talk_long": W.talk_graph("a.png", "a.wav", "x", seconds=14.0, width=832, height=480, seed=1, steps=6,
                                  models=MODELS),
        "broll": W.broll_graph("a.png", "x", seconds=5, width=832, height=480, seed=1, steps=4, lightning=True,
                               models=MODELS),
        "bridge": W.broll_graph("a.png", "x", seconds=5, width=1280, height=720, seed=1, steps=20, lightning=False,
                                models=MODELS, end_image="b.png"),
        "music": W.music_graph("lofi", seconds=90, seed=1, models=MODELS),
    }


@pytest.mark.parametrize("name", list(_graphs()))
def test_graph_matches_comfy_schema(name):
    prompt = _graphs()[name].to_prompt()
    assert validate_prompt(prompt, OBJECT_INFO) == []
    outs = [n for n in prompt.values() if n.get("_meta", {}).get("title") == W.OUTPUT]
    assert len(outs) == 1


def test_validator_catches_mistakes():
    g = W.broll_graph("a.png", "x", seconds=5, width=832, height=480, seed=1, steps=4, lightning=True, models=MODELS)
    p = g.to_prompt()
    first = next(k for k, v in p.items() if v["class_type"] == "KSamplerAdvanced")
    p[first]["inputs"]["sampler_name"] = "not_a_sampler"
    del p[first]["inputs"]["cfg"]
    p[first]["inputs"]["bogus"] = 1
    errs = validate_prompt(p, OBJECT_INFO)
    assert any("not_a_sampler" in e for e in errs)
    assert any("missing required input 'cfg'" in e for e in errs)
    assert any("unknown input 'bogus'" in e for e in errs)


def test_talk_chunking_covers_audio():
    assert W.talk_chunks(81) == 1
    assert W.talk_chunks(82) == 2
    for seconds in (1, 3.2, 3.3, 7.9, 12.0, 30.0):
        total = int(seconds * W.TALK_FPS) + 1
        n = W.talk_chunks(total)
        produced = W.TALK_CHUNK + (n - 1) * (W.TALK_CHUNK - W.TALK_MOTION)
        assert produced >= total
        assert produced - total < W.TALK_CHUNK  # never a wasted extra window


def test_talk_chain_wiring():
    p = W.talk_graph("a.png", "a.wav", "x", seconds=8.0, width=832, height=480, seed=5, steps=6,
                     models=MODELS).to_prompt()
    talks = [k for k, v in p.items() if v["class_type"] == "WanInfiniteTalkToVideo"]
    assert len(talks) == W.talk_chunks(int(8.0 * W.TALK_FPS) + 1) == 3
    assert "previous_frames" not in p[talks[0]]["inputs"]
    for t in talks[1:]:
        assert "previous_frames" in p[t]["inputs"]
    # every continuation trims its motion frames using the node's trim_image output (slot 4)
    trims = [v for v in p.values() if v["class_type"] == "ImageFromBatch"]
    assert len(trims) == len(talks) - 1
    assert all(v["inputs"]["batch_index"][1] == 4 for v in trims)
    seeds = {v["inputs"]["noise_seed"] for k, v in p.items() if v["class_type"] == "RandomNoise"}
    assert len(seeds) == len(talks)


def test_wan_frame_counts_are_4n_plus_1():
    for s in (0.5, 1, 2.3, 5, 9):
        n = W._wan_frames(s, W.BROLL_FPS, W.BROLL_MAX_FRAMES)
        assert (n - 1) % 4 == 0 and 5 <= n <= W.BROLL_MAX_FRAMES


def test_lightning_toggles_lora():
    on = W.broll_graph("a.png", "x", seconds=5, width=832, height=480, seed=1, steps=4, lightning=True, models=MODELS)
    off = W.broll_graph("a.png", "x", seconds=5, width=832, height=480, seed=1, steps=20, lightning=False, models=MODELS)
    count = lambda g: sum(1 for v in g.to_prompt().values() if v["class_type"] == "LoraLoaderModelOnly")
    assert count(on) == 2 and count(off) == 0


def test_uploaded_inputs_pass_file_check():
    p = W.broll_graph("afterstage_new_upload.png", "x", seconds=2, width=320, height=192, seed=1, steps=4,
                      lightning=False, models=MODELS).to_prompt()
    errs = validate_prompt(p, OBJECT_INFO, check_files=True)
    assert not any("afterstage_new_upload.png" in e for e in errs)
    assert any("wan2.2_i2v_high_noise" in e for e in errs)  # model files still checked

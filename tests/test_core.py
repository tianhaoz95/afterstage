import json

import httpx
import pytest

from afterstage.config import Config, ConfigError, ensure_local, load_config, parse_overrides
from afterstage.llm import ChatClient, extract_json
from afterstage.schema import CastBible, Member, Screenplay, SetDesign, Shot
from afterstage.stages.write import normalise, template_screenplay


def _cast(n=2):
    return CastBible(source_video="s.mp4", duration=10, width=1280, height=720, fps=30,
                     members=[Member(id=f"m{i}", name=f"N{i}") for i in range(1, n + 1)])


# --------------------------------------------------------------------------- config


def test_overrides_and_profiles():
    cfg = load_config(overrides=parse_overrides(["render.profile=final", "llm.model=qwen3:30b", "edit.fps=30"]))
    assert cfg.profile["width"] == 1280
    assert cfg["llm"]["model"] == "qwen3:30b"
    assert cfg.get_path("edit.fps") == 30


def test_ensure_local_blocks_public_endpoints():
    cfg = Config(load_config())
    ensure_local("http://127.0.0.1:11434/v1", cfg, "LLM")
    ensure_local("http://192.168.1.20:8188", cfg, "ComfyUI")
    with pytest.raises(ConfigError):
        ensure_local("https://8.8.8.8/v1", cfg, "LLM")
    cfg["privacy"]["allow_remote"] = True
    ensure_local("https://8.8.8.8/v1", cfg, "LLM")


# --------------------------------------------------------------------------- schema


def test_screenplay_validation_reports_problems():
    sp = Screenplay(title="t", sets=[SetDesign(id="studio", description="d")], shots=[
        Shot(id="a", kind="talk", speaker="m9", line="hi", characters=["m9"], set_id="studio"),
        Shot(id="a", kind="broll", set_id="nowhere"),
        Shot(id="c", kind="bridge"),
    ])
    problems = sp.validate_against(_cast())
    joined = "\n".join(problems)
    assert "duplicate shot id a" in joined
    assert "unknown character id 'm9'" in joined
    assert "unknown set_id 'nowhere'" in joined
    assert "bridge shot must be the first" in joined


def test_template_screenplay_is_valid_and_normalised():
    cast = _cast(3)
    w = load_config()["writer"]
    sp = normalise(template_screenplay(cast, "podcast please", w), cast, w)
    assert sp.validate_against(cast) == []
    assert sp.shots[0].kind == "bridge"
    assert all(s.speaker in s.characters for s in sp.shots if s.kind == "talk")


# --------------------------------------------------------------------------- llm


def test_extract_json_variants():
    assert extract_json('<think>hmm {"no": 1}</think>\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}


def test_structured_repair_loop():
    cast = _cast(2)
    bad = {"title": "x", "shots": [{"id": "s1", "kind": "talk", "speaker": "m7", "line": "hi", "characters": ["m7"]}]}
    good = {"title": "x", "shots": [{"id": "s1", "kind": "talk", "speaker": "m1", "line": "hi", "characters": ["m1"]}]}
    replies = iter([json.dumps(bad), json.dumps(good)])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    cfg = load_config()
    client = ChatClient(cfg["llm"], cfg)
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    sp = client.structured([{"role": "user", "content": "go"}], Screenplay, extra_check=lambda s: s.validate_against(cast))
    assert sp.shots[0].speaker == "m1"
    assert len(seen) == 2
    assert "unknown character id 'm7'" in seen[1]["messages"][-1]["content"]
    assert seen[0]["response_format"]["type"] == "json_schema"


def test_guests_are_valid_speakers():
    from afterstage.schema import Guest

    cast = _cast(1)
    sp = Screenplay(title="t", guests=[Guest(id="g1", name="MC Kim", appearance="grey suit")], shots=[
        Shot(id="s1", kind="talk", speaker="g1", characters=["g1", "m1"], line="Welcome!"),
        Shot(id="s2", kind="talk", speaker="m1", characters=["m1"], line="Thanks!"),
    ])
    assert sp.validate_against(cast) == []
    sp.guests[0].id = "x1"
    assert any("must start with 'g'" in p for p in sp.validate_against(cast))

"""Regenerate the walkthrough site's data from the real code, so the diagrams never drift from it.

    .venv/bin/python reports/project-walkthrough/script/build_data.py

Writes site/data/project.json with:
  * the actual ComfyUI API graphs built by afterstage.comfy.workflows (rendered by the graph viewer)
  * the model manifest (sizes feed the memory-budget chart)
  * the default config, CLI commands, collected test names and per-module line counts
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "reports" / "project-walkthrough" / "site" / "data" / "project.json"
sys.path.insert(0, str(ROOT))

from afterstage.comfy import workflows as W  # noqa: E402
from afterstage.config import load_config  # noqa: E402
from afterstage.models import manifest  # noqa: E402


def graphs(cfg) -> dict:
    m = cfg["models"]
    d = cfg["render"]["profiles"]["draft"]
    built = {
        "keyframe": ("Keyframe — Qwen-Image-Edit-2511, set plate + 2 cast references, Lightning 4-step",
                     W.keyframe_graph("<shot prompt>", ["set_plate.png", "member_a.png", "member_b.png"],
                                      width=d["width"], height=d["height"], seed=1000, steps=d["keyframe_steps"],
                                      cfg=d["keyframe_cfg"], lightning=True, models=m)),
        "talk": ("Talk — InfiniteTalk, 8 s of dialogue → 3 chained 81-frame windows",
                 W.talk_graph("keyframe.png", "line.wav", "<speaker> talking warmly", seconds=8.0, width=d["width"],
                              height=d["height"], seed=42, steps=d["talk_steps"], models=m)),
        "broll": ("B-roll — Wan2.2 I2V A14B, high-noise → low-noise experts, lightx2v 4-step",
                  W.broll_graph("keyframe.png", "<visual. motion>", seconds=5, width=d["width"], height=d["height"],
                                seed=43, steps=d["broll_steps"], lightning=True, models=m)),
        "bridge": ("Bridge — Wan2.2 first→last frame: source clip's last frame → first new keyframe",
                   W.broll_graph("source_last_frame.png", "<transition>", seconds=5, width=d["width"],
                                 height=d["height"], seed=41, steps=d["broll_steps"], lightning=True, models=m,
                                 end_image="keyframe.png")),
        "music": ("Music — ACE-Step 1.5 XL turbo instrumental bed",
                  W.music_graph("warm lo-fi R&B instrumental, 90 BPM", seconds=95, seed=3, models=m)),
    }
    return {k: {"title": t, "prompt": g.to_prompt()} for k, (t, g) in built.items()}


def tests() -> list[str]:
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT, capture_output=True,
                             text=True, timeout=120).stdout
        return [line.strip() for line in out.splitlines() if "::" in line]
    except Exception:  # noqa: BLE001
        return []


def loc() -> list[dict]:
    rows = []
    for p in sorted((ROOT / "afterstage").rglob("*.py")):
        n = sum(1 for _ in p.open())
        if n > 5:
            rows.append({"file": str(p.relative_to(ROOT)), "lines": n})
    return rows


def main() -> None:
    cfg = load_config()
    data = {
        "constants": {"talk_fps": W.TALK_FPS, "talk_chunk": W.TALK_CHUNK, "talk_motion": W.TALK_MOTION,
                      "broll_fps": W.BROLL_FPS, "broll_max_frames": W.BROLL_MAX_FRAMES},
        "graphs": graphs(cfg),
        "models": manifest(),
        "config": dict(cfg),
        "tests": tests(),
        "loc": loc(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB, {len(data['tests'])} tests)")


if __name__ == "__main__":
    main()

"""A tiny builder for ComfyUI API-format prompts.

Workflows are written as Python instead of exported JSON so they can be parameterised
(number of InfiniteTalk chunks, number of reference images, LoRA on/off) and unit-tested
against a recorded ``/object_info`` schema.

    g = Graph()
    unet = g.add("UNETLoader", unet_name="x.safetensors", weight_dtype="default")
    g.add("KSampler", model=unet.out(0), ...)
    g.to_prompt()  # -> {"1": {"class_type": "UNETLoader", "inputs": {...}}, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Ref:
    node_id: str
    slot: int

    def to_json(self) -> list:
        return [self.node_id, self.slot]


@dataclass
class Node:
    id: str
    class_type: str
    inputs: dict[str, Any] = field(default_factory=dict)
    title: str | None = None

    def out(self, slot: int = 0) -> Ref:
        return Ref(self.id, slot)

    # Most nodes have a single output; let a Node stand in for its first output.
    def to_json(self) -> list:
        return [self.id, 0]


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self._next = 1

    def add(self, class_type: str, _title: str | None = None, **inputs: Any) -> Node:
        node = Node(str(self._next), class_type, {k: v for k, v in inputs.items() if v is not None}, _title)
        self._next += 1
        self.nodes[node.id] = node
        return node

    def by_title(self, title: str) -> Node:
        for n in self.nodes.values():
            if n.title == title:
                return n
        raise KeyError(title)

    def to_prompt(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for node in self.nodes.values():
            inputs = {}
            for k, v in node.inputs.items():
                inputs[k] = v.to_json() if isinstance(v, (Ref, Node)) else v
            entry: dict[str, Any] = {"class_type": node.class_type, "inputs": inputs}
            if node.title:
                entry["_meta"] = {"title": node.title}
            out[node.id] = entry
        return out


# --------------------------------------------------------------------------- validation


def validate_prompt(prompt: dict[str, dict], object_info: dict[str, Any], check_files: bool = False) -> list[str]:
    """Static check of an API prompt against a ComfyUI ``/object_info`` dump.

    Catches the mistakes that matter when hand-writing graphs: unknown node classes, missing
    required inputs, unknown input names, links to missing nodes / out-of-range output slots,
    link type mismatches, and enum values outside the allowed list. Model-file enums are only
    checked when ``check_files`` is true (on the target machine, where the files exist).
    """
    errors: list[str] = []
    for nid, node in prompt.items():
        ct = node["class_type"]
        info = object_info.get(ct)
        if info is None:
            errors.append(f"node {nid}: unknown class_type {ct}")
            continue
        req = dict(info["input"].get("required", {}))
        opt = dict(info["input"].get("optional", {}))
        # flatten dynamic-combo sub-inputs ("mode.mask_1") for the chosen option
        for name, spec in list(req.items()) + list(opt.items()):
            if spec[0] == "COMFY_DYNAMICCOMBO_V3":
                chosen = node["inputs"].get(name)
                for option in spec[1].get("options", []):
                    if option["key"] == chosen:
                        for sub_kind in ("required", "optional"):
                            for sub, sub_spec in option.get("inputs", {}).get(sub_kind, {}).items():
                                (req if sub_kind == "required" else opt)[f"{name}.{sub}"] = sub_spec
        known = set(req) | set(opt)
        for name in req:
            if name not in node["inputs"]:
                errors.append(f"node {nid} ({ct}): missing required input {name!r}")
        for name, value in node["inputs"].items():
            if name not in known:
                errors.append(f"node {nid} ({ct}): unknown input {name!r}")
                continue
            spec = req.get(name) or opt.get(name)
            kind = spec[0]
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                src = prompt.get(value[0])
                if src is None:
                    errors.append(f"node {nid} ({ct}).{name}: link to missing node {value[0]}")
                    continue
                src_info = object_info.get(src["class_type"])
                if src_info is None:
                    continue
                outs = src_info["output"]
                if value[1] >= len(outs):
                    errors.append(f"node {nid} ({ct}).{name}: {src['class_type']} has no output slot {value[1]}")
                    continue
                out_type = outs[value[1]]
                expect = kind if isinstance(kind, str) else "COMBO"
                if expect not in ("*", "COMBO", "COMFY_DYNAMICCOMBO_V3") and out_type != "*" and out_type != expect:
                    errors.append(
                        f"node {nid} ({ct}).{name}: expects {expect}, got {out_type} from {src['class_type']}"
                    )
                continue
            options = None
            if isinstance(kind, list):
                options = kind
            elif kind == "COMBO":
                options = spec[1].get("options") if len(spec) > 1 else None
            elif kind == "COMFY_DYNAMICCOMBO_V3":
                options = [o["key"] for o in spec[1].get("options", [])]
            extra = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if extra.get("image_upload") or extra.get("audio_upload") or extra.get("video_upload"):
                options = None  # uploaded inputs aren't in a cached /object_info listing
            if options is not None and value not in options:
                is_file = isinstance(value, str) and value.rsplit(".", 1)[-1] in {
                    "safetensors", "pt", "pth", "ckpt", "gguf", "bin", "png", "jpg", "jpeg", "wav", "mp3", "flac", "webp"
                }
                if is_file and not check_files:
                    continue
                errors.append(f"node {nid} ({ct}).{name}: {value!r} not in {options[:12]}")
            if kind in ("INT", "FLOAT") and len(spec) > 1 and isinstance(value, (int, float)):
                lo, hi = spec[1].get("min"), spec[1].get("max")
                if lo is not None and value < lo or hi is not None and value > hi:
                    errors.append(f"node {nid} ({ct}).{name}: {value} outside [{lo}, {hi}]")
    return errors

"""Client for a *local* OpenAI-compatible server (Ollama, vLLM, llama.cpp, SGLang, TRT-LLM).

No cloud SDKs: plain HTTP to an endpoint that ``config.ensure_local`` has verified is on this
machine or LAN.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from .config import Config, ensure_local
from .log import log

T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates <think> blocks and ``` fences)."""
    text = _THINK.sub("", text).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise LLMError(f"no JSON object in reply: {text[:300]!r}")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise LLMError("unterminated JSON object in reply")


def image_part(path: str | Path, max_side: int = 768) -> dict:
    """Encode an image as an OpenAI ``image_url`` content part (downscaled to keep prompts small)."""
    from io import BytesIO

    from PIL import Image

    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


class ChatClient:
    def __init__(self, section: dict, cfg: Config, what: str = "LLM") -> None:
        self.base_url = section["base_url"].rstrip("/")
        ensure_local(self.base_url, cfg, what)
        self.model = section["model"]
        self.section = section
        self.http = httpx.Client(
            timeout=httpx.Timeout(30.0, read=float(section.get("timeout", 600))),
            headers={"Authorization": f"Bearer {section.get('api_key', 'local')}"},
        )

    def chat(self, messages: list[dict], *, json_schema: dict | None = None, temperature: float | None = None,
             max_tokens: int | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.section.get("temperature", 0.7) if temperature is None else temperature,
            "max_tokens": max_tokens or self.section.get("max_tokens", 4096),
            "stream": False,
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": False},
            }
        r = self.http.post(f"{self.base_url}/chat/completions", json=body)
        if r.status_code == 400 and json_schema is not None:
            # some servers only support {"type": "json_object"}
            body["response_format"] = {"type": "json_object"}
            r = self.http.post(f"{self.base_url}/chat/completions", json=body)
        if r.status_code != 200:
            raise LLMError(f"LLM server error {r.status_code}: {r.text[:1000]}")
        msg = r.json()["choices"][0]["message"]
        return msg.get("content") or ""

    def structured(self, messages: list[dict], model: type[T], *, attempts: int = 3,
                   extra_check=None) -> T:
        """Ask for JSON matching ``model``; feed validation errors back until it complies."""
        schema = model.model_json_schema()
        convo = list(messages)
        last_err = ""
        for attempt in range(1, attempts + 1):
            reply = self.chat(convo, json_schema=schema)
            try:
                obj = model.model_validate(extract_json(reply))
                problems = extra_check(obj) if extra_check else []
                if not problems:
                    return obj
                last_err = "\n".join(f"- {p}" for p in problems)
            except (LLMError, ValidationError, json.JSONDecodeError) as exc:
                last_err = str(exc)[:3000]
            log.warning(f"LLM output rejected (attempt {attempt}/{attempts}):\n{last_err}")
            convo = convo + [
                {"role": "assistant", "content": reply[:12000]},
                {"role": "user", "content": "Your JSON has problems:\n" + last_err +
                 "\nReturn the complete corrected JSON object only."},
            ]
        raise LLMError(f"model never produced valid output: {last_err}")

    def unload(self) -> None:
        """Free GPU memory held by the model (Ollama-specific; harmless elsewhere)."""
        parsed = urlparse(self.base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        try:
            self.http.post(f"{root}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=30)
        except httpx.HTTPError:
            pass

"""Minimal ComfyUI HTTP client: upload inputs, queue a prompt, wait, download outputs."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx

from ..log import log
from .graph import Graph, validate_prompt
from .workflows import OUTPUT


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, url: str, timeout: float = 7200.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex
        self.http = httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))
        self._object_info: dict | None = None

    # ------------------------------------------------------------------ basics

    def ping(self) -> dict:
        r = self.http.get(f"{self.url}/system_stats")
        r.raise_for_status()
        return r.json()

    def object_info(self) -> dict:
        if self._object_info is None:
            r = self.http.get(f"{self.url}/object_info")
            r.raise_for_status()
            self._object_info = r.json()
        return self._object_info

    def upload(self, path: str | Path, kind: str = "image") -> str:
        """Upload a file to ComfyUI's input dir; returns the name to reference in LoadImage/LoadAudio."""
        path = Path(path)
        name = f"afterstage_{uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve()) + str(path.stat().st_mtime)).hex[:12]}{path.suffix}"
        with path.open("rb") as fh:
            # /upload/image accepts any file type and stores it in input/
            r = self.http.post(
                f"{self.url}/upload/image",
                files={"image": (name, fh, "application/octet-stream")},
                data={"overwrite": "true", "type": "input"},
            )
        r.raise_for_status()
        return r.json()["name"]

    def free(self) -> None:
        try:
            self.http.post(f"{self.url}/free", json={"unload_models": True, "free_memory": True})
        except httpx.HTTPError as exc:  # non-fatal
            log.warning(f"ComfyUI /free failed: {exc}")

    # ------------------------------------------------------------------ run

    def run(self, graph: Graph, out_dir: str | Path, label: str = "job") -> list[Path]:
        prompt = graph.to_prompt()
        problems = validate_prompt(prompt, self.object_info(), check_files=True)
        if problems:
            raise ComfyError(f"{label}: workflow does not match this ComfyUI install:\n  " + "\n  ".join(problems))
        r = self.http.post(f"{self.url}/prompt", json={"prompt": prompt, "client_id": self.client_id})
        if r.status_code != 200:
            raise ComfyError(f"{label}: ComfyUI rejected prompt: {r.text[:2000]}")
        prompt_id = r.json()["prompt_id"]
        log.info(f"[comfy] {label}: queued {prompt_id}")
        entry = self._wait(prompt_id, label)
        return self._download(entry, prompt, Path(out_dir))

    def _wait(self, prompt_id: str, label: str) -> dict:
        start = time.time()
        last_note = 0.0
        while True:
            r = self.http.get(f"{self.url}/history/{prompt_id}")
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                entry = data[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m[1] for m in status.get("messages", []) if m[0] == "execution_error"]
                    detail = msgs[0] if msgs else status
                    raise ComfyError(f"{label}: execution failed: {json.dumps(detail)[:2000]}")
                if status.get("completed", True):
                    return entry
            elapsed = time.time() - start
            if elapsed > self.timeout:
                raise ComfyError(f"{label}: timed out after {elapsed:.0f}s")
            if elapsed - last_note > 60:
                log.info(f"[comfy] {label}: running… {elapsed / 60:.1f} min")
                last_note = elapsed
            time.sleep(2.0)

    def _download(self, entry: dict, prompt: dict, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = [nid for nid, n in prompt.items() if n.get("_meta", {}).get("title") == OUTPUT]
        outputs = entry.get("outputs", {})
        files: list[Path] = []
        for nid in target or list(outputs):
            node_out = outputs.get(nid, {})
            for key in ("images", "audio", "video", "gifs"):
                for item in node_out.get(key, []):
                    params = {"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                              "type": item.get("type", "output")}
                    r = self.http.get(f"{self.url}/view", params=params)
                    r.raise_for_status()
                    dest = out_dir / item["filename"]
                    dest.write_bytes(r.content)
                    files.append(dest)
        if not files:
            raise ComfyError("job finished but produced no output files")
        return sorted(files)

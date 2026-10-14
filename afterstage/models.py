"""Download the ComfyUI model weights listed in ``models.yaml`` (resumable, idempotent)."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import httpx
import yaml
from rich.progress import BarColumn, DownloadColumn, Progress, TransferSpeedColumn

from .config import Config
from .log import log


def manifest() -> dict:
    return yaml.safe_load(resources.files("afterstage").joinpath("models.yaml").read_text())["models"]


def _groups(entry: dict) -> set[str]:
    g = entry.get("group", [])
    return {g} if isinstance(g, str) else set(g)


def plan(cfg: Config, comfy_dir: Path, groups: set[str] | None = None) -> list[tuple[str, Path, dict]]:
    items = []
    for key, entry in manifest().items():
        if groups and not (_groups(entry) & groups):
            continue
        filename = cfg["models"].get(key) or entry["url"].rsplit("/", 1)[-1]
        items.append((key, comfy_dir / "models" / entry["folder"] / filename, entry))
    return items


def status(cfg: Config, comfy_dir: Path) -> list[tuple[str, Path, bool]]:
    return [(k, p, p.exists() and p.stat().st_size > 1024) for k, p, _ in plan(cfg, comfy_dir)]


def download(cfg: Config, comfy_dir: Path, groups: set[str] | None = None) -> None:
    token = os.environ.get("HF_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    todo = [(k, p, e) for k, p, e in plan(cfg, comfy_dir, groups) if not (p.exists() and p.stat().st_size > 1024)]
    if not todo:
        log.info("all models present")
        return
    log.info(f"downloading {len(todo)} files (~{sum(e.get('gb', 0) for _, _, e in todo):.0f} GB)")
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, read=600.0), headers=headers) as http, \
            Progress("[progress.description]{task.description}", BarColumn(), DownloadColumn(),
                     TransferSpeedColumn()) as prog:
        for key, path, entry in todo:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(path.suffix + ".part")
            have = part.stat().st_size if part.exists() else 0
            hdrs = {"Range": f"bytes={have}-"} if have else {}
            with http.stream("GET", entry["url"], headers=hdrs) as r:
                if r.status_code == 416:  # already complete
                    part.rename(path)
                    continue
                r.raise_for_status()
                if have and r.status_code != 206:
                    have = 0  # server ignored Range
                total = int(r.headers.get("content-length", 0)) + have
                task = prog.add_task(path.name[:48], total=total or None, completed=have)
                with part.open("ab" if have else "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        prog.advance(task, len(chunk))
            part.rename(path)
            log.info(f"✓ {key}: {path}")

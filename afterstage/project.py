"""Project workspace: one directory per source clip + prompt.

    my_project/
      project.json      source + user prompt
      config.yaml       optional per-project overrides
      source.<ext>      copy of the input clip
      cast.json         CastBible (editable)
      screenplay.json   Screenplay (editable)
      state.json        content-addressed cache of every generated artifact
      analysis/ sets/ keyframes/ audio/ shots/ music/ final/

Every generated artifact goes through ``Project.cached``: its inputs are fingerprinted, so
editing one line of the screenplay only re-renders that shot's voice and video.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import Config, load_config
from .log import log
from .schema import CastBible, Screenplay


class ProjectError(RuntimeError):
    pass


class ProjectMeta(BaseModel):
    source: str
    prompt: str
    created: str
    cast_names: list[str] = []


def _file_sig(path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(st.st_mtime_ns)}"


class Project:
    def __init__(self, root: str | Path, cfg: Config | None = None, user_config: str | Path | None = None,
                 overrides: dict | None = None) -> None:
        self.root = Path(root).resolve()
        if not (self.root / "project.json").exists():
            raise ProjectError(f"{self.root} is not a afterstage project (run `afterstage new` first)")
        self.cfg = cfg or load_config(user_config, self.root / "config.yaml", overrides=overrides)
        self._state_path = self.root / "state.json"
        self._state: dict[str, Any] = json.loads(self._state_path.read_text()) if self._state_path.exists() else {}
        self.force: set[str] = set()  # cache keys (or prefixes) to regenerate regardless of fingerprint

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(cls, root: str | Path, source: str | Path, prompt: str, cast_names: list[str] | None = None,
               **kw: Any) -> Project:
        root = Path(root).resolve()
        source = Path(source).resolve()
        if not source.exists():
            raise ProjectError(f"source clip not found: {source}")
        root.mkdir(parents=True, exist_ok=True)
        if (root / "project.json").exists():
            raise ProjectError(f"{root} already contains a project")
        dest = root / f"source{source.suffix.lower()}"
        shutil.copy2(source, dest)
        meta = ProjectMeta(source=dest.name, prompt=prompt, created=datetime.now(UTC).isoformat(),
                           cast_names=cast_names or [])
        (root / "project.json").write_text(meta.model_dump_json(indent=2))
        return cls(root, **kw)

    # ------------------------------------------------------------------ paths & docs

    @property
    def meta(self) -> ProjectMeta:
        return ProjectMeta.model_validate_json((self.root / "project.json").read_text())

    def update_prompt(self, prompt: str) -> None:
        meta = self.meta
        meta.prompt = prompt
        (self.root / "project.json").write_text(meta.model_dump_json(indent=2))

    @property
    def source(self) -> Path:
        return self.root / self.meta.source

    def dir(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def rel(self, path: str | Path) -> str:
        return str(Path(path).resolve().relative_to(self.root))

    def abs(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def cast_path(self) -> Path:
        return self.root / "cast.json"

    @property
    def screenplay_path(self) -> Path:
        return self.root / "screenplay.json"

    def load_cast(self) -> CastBible:
        if not self.cast_path.exists():
            raise ProjectError("no cast.json yet — run `afterstage analyze` first")
        return CastBible.model_validate_json(self.cast_path.read_text())

    def save_cast(self, cast: CastBible) -> None:
        self.cast_path.write_text(cast.model_dump_json(indent=2))

    def load_screenplay(self) -> Screenplay:
        if not self.screenplay_path.exists():
            raise ProjectError("no screenplay.json yet — run `afterstage write` first")
        return Screenplay.model_validate_json(self.screenplay_path.read_text())

    def save_screenplay(self, sp: Screenplay) -> None:
        self.screenplay_path.write_text(sp.model_dump_json(indent=2))

    # ------------------------------------------------------------------ cache

    def fingerprint(self, inputs: dict[str, Any]) -> str:
        def norm(v: Any) -> Any:
            if isinstance(v, Path):
                return _file_sig(v) if v.exists() else str(v)
            if isinstance(v, BaseModel):
                return v.model_dump()
            if isinstance(v, dict):
                return {k: norm(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [norm(x) for x in v]
            return v

        blob = json.dumps(norm(inputs), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    def _forced(self, key: str) -> bool:
        return any(key == f or key.startswith(f + "/") or key.endswith("/" + f) for f in self.force)

    def cached(self, key: str, inputs: dict[str, Any], produce: Callable[[], Path]) -> Path:
        """Return the artifact for ``key``, regenerating it only when its inputs changed."""
        fp = self.fingerprint(inputs)
        hit = self._state.get(key)
        if hit and hit.get("fp") == fp and self.abs(hit["path"]).exists() and not self._forced(key):
            return self.abs(hit["path"])
        log.info(f"→ generating {key}")
        path = Path(produce())
        self._state[key] = {"fp": fp, "path": self.rel(path), "at": datetime.now(UTC).isoformat()}
        self._state_path.write_text(json.dumps(self._state, indent=2))
        return path

    def artifact(self, key: str) -> Path | None:
        hit = self._state.get(key)
        if hit and self.abs(hit["path"]).exists():
            return self.abs(hit["path"])
        return None

    def invalidate(self, prefix: str) -> int:
        keys = [k for k in self._state if k == prefix or k.startswith(prefix + "/")]
        for k in keys:
            del self._state[k]
        self._state_path.write_text(json.dumps(self._state, indent=2))
        return len(keys)

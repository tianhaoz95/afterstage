"""Configuration: packaged defaults, deep-merged with an optional user YAML and project YAML."""

from __future__ import annotations

import copy
import ipaddress
import socket
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    """A nested dict with dotted access: ``cfg.get_path("render.profile")``."""

    def get_path(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set_path(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    @property
    def profile(self) -> dict:
        name = self.get_path("render.profile", "draft")
        profiles = self.get_path("render.profiles", {})
        if name not in profiles:
            raise ConfigError(f"render profile {name!r} not defined; have {list(profiles)}")
        return profiles[name]


def default_config_dict() -> dict:
    text = resources.files("afterstage").joinpath("default_config.yaml").read_text()
    return yaml.safe_load(text)


def load_config(*paths: str | Path | None, overrides: dict | None = None) -> Config:
    data = default_config_dict()
    for p in paths:
        if p and Path(p).exists():
            data = _deep_merge(data, yaml.safe_load(Path(p).read_text()) or {})
    if overrides:
        data = _deep_merge(data, overrides)
    return Config(data)


def parse_overrides(pairs: list[str]) -> dict:
    """Turn ``["render.profile=final", "llm.model=qwen3:30b"]`` into a nested dict."""
    cfg = Config()
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"override must look like key.path=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        cfg.set_path(key.strip(), yaml.safe_load(raw))
    return dict(cfg)


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "host.docker.internal"} or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return bool(infos)


def ensure_local(url: str, cfg: Config, what: str) -> None:
    """Enforce the on-device promise: refuse endpoints that resolve to public addresses."""
    if cfg.get_path("privacy.allow_remote", False):
        return
    host = urlparse(url).hostname or ""
    if not _is_private_host(host):
        raise ConfigError(
            f"{what} endpoint {url!r} is not on this machine or LAN. afterstage runs fully on-device; "
            "point it at a local server or set privacy.allow_remote: true if you really mean it."
        )

"""Shot boundary detection + one representative keyframe per scene."""

from __future__ import annotations

from pathlib import Path

from .. import media
from ..log import log
from ..schema import SceneInfo


def detect_scenes(video: Path, out_dir: Path, threshold: float, max_scenes: int, duration: float) -> list[SceneInfo]:
    spans: list[tuple[float, float]] = []
    try:
        from scenedetect import ContentDetector, detect

        for start, end in detect(str(video), ContentDetector(threshold=threshold)):
            spans.append((start.get_seconds(), end.get_seconds()))
    except Exception as exc:  # pragma: no cover - depends on codecs
        log.warning(f"scene detection failed ({exc}); using fixed windows")
    if not spans:
        step = max(duration / max(1, min(max_scenes, int(duration // 4) or 1)), 1.0)
        t = 0.0
        while t < duration - 0.1:
            spans.append((t, min(duration, t + step)))
            t += step
    # merge down to max_scenes by joining the shortest neighbours
    while len(spans) > max_scenes:
        i = min(range(len(spans) - 1), key=lambda k: (spans[k][1] - spans[k][0]) + (spans[k + 1][1] - spans[k + 1][0]))
        spans[i : i + 2] = [(spans[i][0], spans[i + 1][1])]

    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = []
    for i, (s, e) in enumerate(spans):
        kf = out_dir / f"scene_{i:03d}.jpg"
        media.frame_at(video, s + (e - s) / 2, kf)
        scenes.append(SceneInfo(index=i, start=round(s, 3), end=round(e, 3), keyframe=str(kf)))
    return scenes

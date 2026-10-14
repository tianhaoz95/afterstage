"""Find the people in the clip: detect faces on sampled frames, embed, cluster into members.

Backends
* insightface (default on the Spark): RetinaFace detector + ArcFace embeddings (buffalo_l).
* opencv: YuNet detector (OpenCV zoo, 230 KB) + a colour/texture descriptor. Weak, but needs no GPU
          stack (dev laptops / dry runs).
* none: skip; members come from ``--cast`` names only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..log import log


@dataclass
class FaceHit:
    t: float
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    score: float
    emb: np.ndarray
    frame_idx: int
    frontal: float = 1.0


@dataclass
class Cluster:
    hits: list[FaceHit] = field(default_factory=list)

    @property
    def centroid(self) -> np.ndarray:
        c = np.mean([h.emb for h in self.hits], axis=0)
        return c / (np.linalg.norm(c) + 1e-8)


# --------------------------------------------------------------------------- backends


@lru_cache(maxsize=1)
def _insightface(device: str):
    from insightface.app import FaceAnalysis

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if device.startswith("cuda") else ["CPUExecutionProvider"]
    app = FaceAnalysis(name="buffalo_l", providers=providers, allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0 if device.startswith("cuda") else -1, det_size=(640, 640))
    return app


YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"


@lru_cache(maxsize=1)
def _yunet_path() -> str | None:
    path = Path.home() / ".cache" / "afterstage" / "face_detection_yunet_2023mar.onnx"
    if not path.exists():
        try:
            import httpx

            path.parent.mkdir(parents=True, exist_ok=True)
            r = httpx.get(YUNET_URL, follow_redirects=True, timeout=60)
            r.raise_for_status()
            path.write_bytes(r.content)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"could not fetch YuNet face detector ({exc}); opencv face backend disabled")
            return None
    return str(path)


def _yunet_detect(frame: np.ndarray, min_size: int) -> list[tuple[int, int, int, int, float]]:
    model = _yunet_path()
    if model is None:
        return []
    h, w = frame.shape[:2]
    det = cv2.FaceDetectorYN.create(model, "", (w, h), 0.8)
    _, faces = det.detect(frame)
    out = []
    for f in faces if faces is not None else []:
        x, y, fw, fh = (int(v) for v in f[:4])
        if min(fw, fh) >= min_size:
            out.append((max(0, x), max(0, y), x + fw, y + fh, float(f[-1])))
    return out


def _opencv_embed(crop: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(cv2.resize(crop, (64, 64)), cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).flatten()
    gray = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (16, 16)).flatten().astype(np.float32)
    v = np.concatenate([hist / (hist.sum() + 1e-6), (gray - gray.mean()) / (gray.std() + 1e-6) * 0.05])
    return v / (np.linalg.norm(v) + 1e-8)


def detect(frame: np.ndarray, backend: str, device: str, min_size: int) -> list[tuple[tuple, float, np.ndarray, float]]:
    """Return [(bbox, score, embedding, frontalness)] for one BGR frame."""
    out = []
    if backend == "insightface":
        for f in _insightface(device).get(frame):
            x1, y1, x2, y2 = (int(v) for v in f.bbox)
            if min(x2 - x1, y2 - y1) < min_size:
                continue
            frontal = 1.0
            if getattr(f, "kps", None) is not None:
                k = f.kps  # eyes, nose, mouth corners: nose centred between eyes => frontal
                eye_mid = (k[0][0] + k[1][0]) / 2
                span = abs(k[1][0] - k[0][0]) + 1e-6
                frontal = float(max(0.0, 1.0 - abs(k[2][0] - eye_mid) / span))
            out.append(((x1, y1, x2, y2), float(f.det_score), f.normed_embedding.astype(np.float32), frontal))
    elif backend == "opencv":
        for x1, y1, x2, y2, score in _yunet_detect(frame, min_size):
            crop = frame[y1:y2, x1:x2]
            if crop.size:
                out.append(((x1, y1, x2, y2), score, _opencv_embed(crop), 1.0))
    return out


def embed_image(path: str | Path, backend: str, device: str) -> list[np.ndarray]:
    img = cv2.imread(str(path))
    if img is None:
        return []
    return [e for _, _, e, _ in detect(img, backend, device, min_size=32)]


# --------------------------------------------------------------------------- clustering


def cluster_faces(hits: list[FaceHit], threshold: float) -> list[Cluster]:
    clusters: list[Cluster] = []
    for h in sorted(hits, key=lambda x: -x.score):
        best, best_sim = None, threshold
        for c in clusters:
            sim = float(np.dot(c.centroid, h.emb))
            if sim > best_sim:
                best, best_sim = c, sim
        if best is None:
            clusters.append(Cluster([h]))
        else:
            best.hits.append(h)
    # merge clusters whose centroids ended up close
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if float(np.dot(clusters[i].centroid, clusters[j].centroid)) > threshold + 0.1:
                    clusters[i].hits += clusters[j].hits
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(clusters, key=lambda c: -len(c.hits))


def _square_crop(frame: np.ndarray, bbox, scale: float, down_bias: float = 0.0) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2 + (y2 - y1) * down_bias
    side = max(x2 - x1, y2 - y1) * scale
    a, b = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    c, d = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    return frame[b:d, a:c]


def find_people(video: Path, out_dir: Path, *, backend: str, device: str, sample_fps: float, min_size: int,
                threshold: float, max_members: int, min_screen_time: float) -> list[dict]:
    """Returns member dicts: {face_refs, body_refs, screen_time, embedding(np)} sorted by screen time."""
    if backend == "none":
        return []
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(round(fps / sample_fps)))
    hits: list[FaceHit] = []
    frames: dict[int, np.ndarray] = {}  # JPEG-encoded to bound memory on long clips
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                found = detect(frame, backend, device, min_size)
                if found:
                    frames[idx] = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])[1]
                for bbox, score, emb, frontal in found:
                    hits.append(FaceHit(idx / fps, bbox, score, emb, idx, frontal))
        idx += 1
    cap.release()
    log.info(f"faces: {len(hits)} detections on {len(frames)} frames")
    if not hits:
        return []

    clusters = cluster_faces(hits, threshold)
    out_dir.mkdir(parents=True, exist_ok=True)
    people = []
    for c in clusters:
        screen_time = len(c.hits) / sample_fps
        if screen_time < min_screen_time:
            continue
        n = len(people) + 1
        ranked = sorted(c.hits, key=lambda h: -(h.score * h.frontal * np.sqrt((h.bbox[2] - h.bbox[0]) * (h.bbox[3] - h.bbox[1]))))
        face_refs, body_refs = [], []
        used_frames: set[int] = set()
        for h in ranked:
            if h.frame_idx in used_frames:
                continue
            used_frames.add(h.frame_idx)
            frame = cv2.imdecode(frames[h.frame_idx], cv2.IMREAD_COLOR)
            k = len(face_refs)
            fp = out_dir / f"m{n}_face_{k}.jpg"
            bp = out_dir / f"m{n}_body_{k}.jpg"
            cv2.imwrite(str(fp), cv2.resize(_square_crop(frame, h.bbox, 1.8), (512, 512)))
            cv2.imwrite(str(bp), _square_crop(frame, h.bbox, 4.5, down_bias=1.2))
            face_refs.append(fp)
            body_refs.append(bp)
            if len(face_refs) >= 4:
                break
        emb_path = out_dir / f"m{n}_embedding.npy"
        np.save(emb_path, c.centroid)
        people.append({"face_refs": face_refs, "body_refs": body_refs, "screen_time": round(screen_time, 2),
                       "embedding": emb_path})
        if len(people) >= max_members:
            break
    return people

"""Stage 6 — the edit: conform every clip, concatenate (source clip → generated segment), mix the
music bed under the dialogue with side-chain ducking, burn subtitles + the AI-generated badge, and
write provenance metadata. Pure ffmpeg + Pillow, so it needs no GPU."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import media
from ..log import log
from ..project import Project
from ..schema import Screenplay

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


@dataclass
class Cue:
    start: float
    end: float
    text: str
    speaker: str


def _font(size: int, path: str | None = None) -> ImageFont.ImageFont:
    for p in ([path] if path else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    log.warning("no CJK font found (install fonts-noto-cjk); subtitles may not render Hangul")
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    tokens = text.split(" ") if " " in text else list(text)
    joiner = " " if " " in text else ""
    for tok in tokens:
        trial = (cur + joiner + tok) if cur else tok
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    return lines[:3]


def render_text_png(text: str, out: Path, width: int, height: int, font_size: int, font_path: str | None = None,
                    position: str = "bottom") -> Path:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(font_size, font_path)
    if position == "badge":
        pad = 8
        tw = d.textlength(text, font=font)
        x, y = width - tw - 3 * pad, 2 * pad
        d.rounded_rectangle([x - pad, y - pad // 2, x + tw + pad, y + font_size + pad], radius=8, fill=(0, 0, 0, 140))
        d.text((x, y), text, font=font, fill=(255, 255, 255, 230))
    else:
        lines = _wrap(d, text, font, int(width * 0.86))
        lh = int(font_size * 1.3)
        y = height - lh * len(lines) - int(height * 0.06)
        for line in lines:
            tw = d.textlength(line, font=font)
            x = (width - tw) / 2
            d.text((x, y), line, font=font, fill="white", stroke_width=max(2, font_size // 12), stroke_fill="black")
            y += lh
    img.save(out)
    return out


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], out: Path) -> Path:
    out.write_text("\n".join(f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n"
                             for i, c in enumerate(cues, 1)), encoding="utf-8")
    return out


def conform(src: Path, out: Path, w: int, h: int, fps: int, crf: int) -> Path:
    info = media.probe(src)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"setsar=1,fps={fps},format=yuv420p")
    args = ["ffmpeg", "-i", str(src)]
    if not info.has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    args += ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if info.has_audio else "1:a:0",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-c:a", "aac", "-b:a", "192k",
             "-ar", "48000", "-ac", "2", "-t", f"{info.duration:.3f}", "-movflags", "+faststart", str(out)]
    media.run(args)
    return out


def run_edit(project: Project, sp: Screenplay | None = None, clips: dict[str, Path] | None = None) -> Path:
    sp = sp or project.load_screenplay()
    e = project.cfg["edit"]
    w, h, fps, crf = e["width"], e["height"], e["fps"], e["crf"]
    work = project.dir("final/work")
    clips = clips or {s.id: project.artifact(f"shot/{s.id}") for s in sp.shots}
    missing = [k for k, v in clips.items() if v is None]
    if missing:
        raise RuntimeError(f"shots not rendered yet: {missing} (run `afterstage render`)")

    segments: list[Path] = []
    t = 0.0
    gen_start = 0.0
    if e["include_source"]:
        segments.append(project.cached("conform/source", {"src": project.source, "fmt": [w, h, fps, crf]},
                                       lambda: conform(project.source, work / "000_source.mp4", w, h, fps, crf)))
        t = gen_start = media.probe(segments[-1]).duration
    cast = project.load_cast()
    cues: list[Cue] = []
    for shot in sp.shots:
        seg = project.cached(f"conform/{shot.id}", {"src": clips[shot.id], "fmt": [w, h, fps, crf]},
                             lambda shot=shot: conform(clips[shot.id], work / f"{shot.id}.mp4", w, h, fps, crf))
        dur = media.probe(seg).duration
        if shot.kind == "talk" and shot.line:
            cues.append(Cue(t + 0.05, t + dur - 0.1, shot.line, cast.member(shot.speaker).name))
        segments.append(seg)
        t += dur
    total = t

    concat_list = work / "concat.txt"
    concat_list.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))
    joined = work / "joined.mp4"
    media.run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(joined)])

    # ---- audio: duck music under dialogue, only during the generated part
    inputs = ["-i", str(joined)]
    filters = []
    audio_label = "[0:a]"
    music = project.artifact("music/bed")
    if music and e["music"]:
        inputs += ["-i", str(music)]
        gen_len = total - gen_start
        delay_ms = int(gen_start * 1000)
        filters.append(
            f"[1:a]atrim=0:{gen_len:.3f},afade=t=in:d=1.5,afade=t=out:st={max(0.0, gen_len - 2.5):.3f}:d=2.5,"
            f"volume={e['music_volume']},adelay={delay_ms}|{delay_ms},apad,aformat=channel_layouts=stereo[mus]")
        filters.append("[0:a]asplit=2[dlg][key]")
        filters.append("[mus][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[ducked]")
        filters.append("[dlg][ducked]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_label = "[aout]"

    # ---- video overlays: AI badge + subtitles (PNG overlays work without libass/freetype)
    ov_inputs: list[str] = []
    vchain = "[0:v]"
    n_in = len(inputs) // 2
    overlay_steps = []
    if project.cfg["privacy"]["watermark"]:
        badge = render_text_png(project.cfg["privacy"]["watermark_text"], work / "badge.png", w, h,
                                max(18, h // 32), e.get("font"), position="badge")
        ov_inputs += ["-i", str(badge)]
        overlay_steps.append((n_in, gen_start, total))
        n_in += 1
    if e["burn_subtitles"]:
        for k, cue in enumerate(cues):
            png = render_text_png(cue.text, work / f"sub_{k:03d}.png", w, h, e["subtitle_font_size"], e.get("font"))
            ov_inputs += ["-i", str(png)]
            overlay_steps.append((n_in, cue.start, cue.end))
            n_in += 1
    for j, (idx, a, b) in enumerate(overlay_steps):
        nxt = f"[v{j}]"
        filters.append(f"{vchain}[{idx}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'{nxt}")
        vchain = nxt

    final_dir = project.dir("final")
    out = final_dir / f"{_slug(sp.title)}.mp4"
    srt = write_srt(cues, final_dir / f"{_slug(sp.title)}.srt")
    args = ["ffmpeg", *inputs, *ov_inputs]
    if filters:
        args += ["-filter_complex", ";".join(filters)]
    args += ["-map", vchain if overlay_steps else "0:v", "-map", audio_label]
    if overlay_steps:
        args += ["-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    if project.cfg["privacy"]["metadata_tag"]:
        meta = {"generator": "afterstage", "ai_generated": True, "generated_from_s": round(gen_start, 2),
                "prompt": project.meta.prompt[:300]}
        args += ["-metadata", f"comment={json.dumps(meta, ensure_ascii=False)}",
                 "-metadata", "description=Contains AI-generated video and synthetic voices."]
    media.run(args + [str(out)])
    log.info(f"final cut: {out} ({total:.1f}s, generated part starts at {gen_start:.1f}s); subtitles: {srt}")
    return out


def _slug(s: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in s.strip().lower())
    return "_".join(filter(None, keep.split("_")))[:60] or "output"

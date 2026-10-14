"""ComfyUI workflows used by the renderer, built with native (core) ComfyUI nodes only.

* keyframe  – Qwen-Image-Edit-2511: composes cast reference crops + a set plate into a still.
* talk      – InfiniteTalk (Wan2.1 I2V 14B + InfiniteTalk patch): lip-synced talking video of
              arbitrary length, rendered as chained 81-frame chunks with 9 motion frames of overlap.
* broll     – Wan2.2 I2V 14B (high/low-noise experts), or first→last-frame mode for the bridge.
* music     – ACE-Step 1.5 instrumental bed.

Every graph ends in a node titled ``OUTPUT`` (SaveImage / SaveAudio); the client collects
that node's files. Frames are saved as PNG sequences and muxed by our own ffmpeg editor, which
keeps the ComfyUI side free of container/codec settings.
"""

from __future__ import annotations

import math

from .graph import Graph, Node

OUTPUT = "OUTPUT"

TALK_FPS = 25
TALK_CHUNK = 81  # frames per InfiniteTalk window
TALK_MOTION = 9  # overlapping context frames carried into the next window
BROLL_FPS = 16
BROLL_MAX_FRAMES = 81  # Wan2.2 sweet spot (~5 s at 16 fps)

WAN_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，"
    "丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
    "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走, watermark, text, logo"
)


def _seed(seed: int) -> int:
    return int(seed) % (2**50)


def _wan_frames(seconds: float, fps: int, cap: int) -> int:
    """Wan latents are 4n+1 frames long."""
    n = max(5, min(cap, int(round(seconds * fps)) + 1))
    return ((n - 1) // 4) * 4 + 1


def talk_chunks(total_frames: int) -> int:
    if total_frames <= TALK_CHUNK:
        return 1
    return 1 + math.ceil((total_frames - TALK_CHUNK) / (TALK_CHUNK - TALK_MOTION))


# --------------------------------------------------------------------------- keyframe


def keyframe_graph(
    prompt: str,
    images: list[str],
    *,
    width: int,
    height: int,
    seed: int,
    steps: int,
    cfg: float,
    lightning: bool,
    models: dict,
) -> Graph:
    g = Graph()
    model: Node = g.add("UNETLoader", unet_name=models["keyframe_unet"], weight_dtype="default")
    if lightning:
        model = g.add("LoraLoaderModelOnly", model=model, lora_name=models["keyframe_lora"], strength_model=1.0)
    model = g.add("ModelSamplingAuraFlow", model=model, shift=3.1)
    model = g.add("CFGNorm", model=model, strength=1.0)
    clip = g.add("CLIPLoader", clip_name=models["keyframe_clip"], type="qwen_image", device="default")
    vae = g.add("VAELoader", vae_name=models["keyframe_vae"])

    refs: dict[str, Node] = {}
    for i, name in enumerate(images[:3], start=1):
        img = g.add("LoadImage", image=name)
        refs[f"image{i}"] = g.add("FluxKontextImageScale", image=img.out(0))

    def encode(text: str) -> Node:
        cond = g.add("TextEncodeQwenImageEditPlus", clip=clip, prompt=text, vae=vae, **refs)
        return g.add("FluxKontextMultiReferenceLatentMethod", conditioning=cond,
                     reference_latents_method="index_timestep_zero")

    pos, neg = encode(prompt), encode("")
    latent = g.add("EmptySD3LatentImage", width=width, height=height, batch_size=1)
    samples = g.add("KSampler", model=model, seed=_seed(seed), steps=steps, cfg=cfg, sampler_name="euler",
                    scheduler="simple", positive=pos, negative=neg, latent_image=latent, denoise=1.0)
    image = g.add("VAEDecode", samples=samples, vae=vae)
    g.add("SaveImage", OUTPUT, images=image, filename_prefix="afterstage/keyframe")
    return g


# --------------------------------------------------------------------------- talk (InfiniteTalk)


def talk_graph(
    image: str,
    audio: str,
    prompt: str,
    *,
    seconds: float,
    width: int,
    height: int,
    seed: int,
    steps: int,
    models: dict,
) -> Graph:
    total = int(math.ceil(seconds * TALK_FPS)) + 1
    n_chunks = talk_chunks(total)

    g = Graph()
    unet = g.add("UNETLoader", unet_name=models["talk_unet"], weight_dtype="default")
    model = g.add("LoraLoaderModelOnly", model=unet, lora_name=models["talk_lora"], strength_model=1.0)
    patch = g.add("ModelPatchLoader", name=models["talk_patch"])
    clip = g.add("CLIPLoader", clip_name=models["wan_clip"], type="wan", device="default")
    pos = g.add("CLIPTextEncode", clip=clip, text=prompt)
    neg = g.add("ConditioningZeroOut", conditioning=pos)
    vae = g.add("VAELoader", vae_name=models["talk_vae"])
    enc = g.add("AudioEncoderLoader", audio_encoder_name=models["talk_audio_encoder"])
    wav = g.add("LoadAudio", audio=audio)
    audio_emb = g.add("AudioEncoderEncode", audio_encoder=enc, audio=wav)
    start = g.add("LoadImage", image=image)
    sampler = g.add("KSamplerSelect", sampler_name="euler")

    frames: Node | None = None
    for k in range(n_chunks):
        talk = g.add(
            "WanInfiniteTalkToVideo",
            mode="single_speaker",
            model=model,
            model_patch=patch,
            positive=pos,
            negative=neg,
            vae=vae,
            width=width,
            height=height,
            length=TALK_CHUNK,
            audio_encoder_output_1=audio_emb,
            motion_frame_count=TALK_MOTION,
            audio_scale=1.0,
            start_image=start.out(0),
            previous_frames=frames,
        )
        noise = g.add("RandomNoise", noise_seed=_seed(seed + k))
        guider = g.add("CFGGuider", model=talk.out(0), positive=talk.out(1), negative=talk.out(2), cfg=1.0)
        sigmas = g.add("BasicScheduler", model=talk.out(0), scheduler="normal", steps=steps, denoise=1.0)
        latent = g.add("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler, sigmas=sigmas,
                       latent_image=talk.out(3))
        decoded = g.add("VAEDecode", samples=latent.out(0), vae=vae)
        if frames is None:
            frames = decoded
        else:
            # drop the motion-context frames the window re-rendered, then append
            fresh = g.add("ImageFromBatch", image=decoded, batch_index=talk.out(4), length=4096)
            frames = g.add("ImageBatch", image1=frames, image2=fresh)
    g.add("SaveImage", OUTPUT, images=frames, filename_prefix="afterstage/talk")
    return g


# --------------------------------------------------------------------------- b-roll / bridge (Wan2.2 I2V)


def broll_graph(
    start_image: str,
    prompt: str,
    *,
    seconds: float,
    width: int,
    height: int,
    seed: int,
    steps: int,
    lightning: bool,
    models: dict,
    end_image: str | None = None,
    negative: str = WAN_NEGATIVE,
) -> Graph:
    length = _wan_frames(seconds, BROLL_FPS, BROLL_MAX_FRAMES)
    g = Graph()
    experts = []
    for which in ("high", "low"):
        m: Node = g.add("UNETLoader", unet_name=models[f"broll_unet_{which}"], weight_dtype="default")
        if lightning:
            m = g.add("LoraLoaderModelOnly", model=m, lora_name=models[f"broll_lora_{which}"], strength_model=1.0)
        experts.append(g.add("ModelSamplingSD3", model=m, shift=5.0))
    high, low = experts
    clip = g.add("CLIPLoader", clip_name=models["wan_clip"], type="wan", device="default")
    pos = g.add("CLIPTextEncode", clip=clip, text=prompt)
    neg = g.add("CLIPTextEncode", clip=clip, text=negative)
    vae = g.add("VAELoader", vae_name=models["wan_vae"])
    start = g.add("LoadImage", image=start_image)
    if end_image:
        end = g.add("LoadImage", image=end_image)
        cond = g.add("WanFirstLastFrameToVideo", positive=pos, negative=neg, vae=vae, width=width, height=height,
                     length=length, batch_size=1, start_image=start.out(0), end_image=end.out(0))
    else:
        cond = g.add("WanImageToVideo", positive=pos, negative=neg, vae=vae, width=width, height=height,
                     length=length, batch_size=1, start_image=start.out(0))
    if lightning:
        cfg, split = 1.0, steps // 2
    else:
        cfg, split = 3.5, steps // 2
    first = g.add("KSamplerAdvanced", model=high, add_noise="enable", noise_seed=_seed(seed), steps=steps, cfg=cfg,
                  sampler_name="euler", scheduler="simple", positive=cond.out(0), negative=cond.out(1),
                  latent_image=cond.out(2), start_at_step=0, end_at_step=split, return_with_leftover_noise="enable")
    second = g.add("KSamplerAdvanced", model=low, add_noise="disable", noise_seed=0, steps=steps, cfg=cfg,
                   sampler_name="euler", scheduler="simple", positive=cond.out(0), negative=cond.out(1),
                   latent_image=first, start_at_step=split, end_at_step=10000, return_with_leftover_noise="disable")
    images = g.add("VAEDecode", samples=second, vae=vae)
    g.add("SaveImage", OUTPUT, images=images, filename_prefix="afterstage/broll")
    return g


# --------------------------------------------------------------------------- music (ACE-Step 1.5)


def music_graph(tags: str, *, seconds: float, seed: int, models: dict, bpm: int = 96) -> Graph:
    g = Graph()
    unet = g.add("UNETLoader", unet_name=models["music_unet"], weight_dtype="default")
    model = g.add("ModelSamplingAuraFlow", model=unet, shift=3.0)
    clip = g.add("DualCLIPLoader", clip_name1=models["music_clip1"], clip_name2=models["music_clip2"], type="ace",
                 device="default")
    pos = g.add("TextEncodeAceStepAudio1.5", clip=clip, tags=tags, lyrics="[Instrumental]", seed=_seed(seed), bpm=bpm,
                duration=float(seconds), timesignature="4", language="en", keyscale="C major",
                generate_audio_codes=True, cfg_scale=2.0, temperature=0.85, top_p=0.9, top_k=0, min_p=0.0)
    neg = g.add("ConditioningZeroOut", conditioning=pos)
    latent = g.add("EmptyAceStep1.5LatentAudio", seconds=float(seconds), batch_size=1)
    samples = g.add("KSampler", model=model, seed=_seed(seed), steps=8, cfg=1.0, sampler_name="euler",
                    scheduler="simple", positive=pos, negative=neg, latent_image=latent, denoise=1.0)
    vae = g.add("VAELoader", vae_name=models["music_vae"])
    audio = g.add("VAEDecodeAudio", samples=samples, vae=vae)
    g.add("SaveAudio", OUTPUT, audio=audio, filename_prefix="afterstage/music")
    return g

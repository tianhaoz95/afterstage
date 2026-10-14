# afterstage

Give it a movie clip and a sentence; it extends the clip with new, AI-generated footage. For example, a K-pop concert clip plus *"a cosy podcast right after the show where the members talk about their favourite moments"* produces the original clip, then a seamless transition into a podcast studio, then lip-synced members talking in their own cloned voices, with subtitles and a music bed.

**Everything runs on-device on an NVIDIA DGX Spark.** There are no cloud APIs:
- The LLM, VLM, TTS, image and video models all run locally.
- afterstage refuses non-local endpoints unless you opt in explicitly.
- Library telemetry is disabled.
- After setup, Hugging Face is put in offline mode.

```
source clip ─► 1 ANALYZE ─► 2 WRITE ─► 3 VOICE ─► 4 DESIGN ─► 5 RENDER ─► 6 EDIT ─► final.mp4
               scenes        local LLM   Chatterbox   Qwen-Image-  InfiniteTalk   ffmpeg: conform,
               faces→cast    → screen-   multilingual Edit-2511    (lip-sync),    concat, duck music,
               vocals, ASR   play.json   voice clone  keyframes    Wan2.2 I2V /   subtitles, AI badge,
               VLM captions  (editable)  per line     per shot     FLF bridge,    provenance metadata
               → cast.json                                        ACE-Step music
```

**Walkthrough:** an interactive guide to the design, implementation and testing, with animated diagrams, the real ComfyUI graphs and a step-by-step test plan:

```bash
reports/project-walkthrough/script/serve.sh --open     # http://127.0.0.1:8642
```

## Models (all local, all open weights)

| Job | Model | Runs in |
|---|---|---|
| Screenplay | Qwen3-32B (any OpenAI-compatible local server; `gpt-oss:120b` also works) | Ollama |
| Scene and appearance captions | Qwen3-VL-8B | Ollama |
| Speech recognition | Whisper large-v3-turbo | afterstage (transformers) |
| Vocal / music separation | Demucs htdemucs | afterstage |
| Face detection and identity | InsightFace buffalo_l (RetinaFace + ArcFace) | afterstage (CPU, Grace) |
| Voice cloning TTS (23 languages incl. Korean) | Chatterbox Multilingual | afterstage |
| Set plates and shot keyframes, from cast reference crops | Qwen-Image-Edit-2511 (fp8) + Lightning LoRA | ComfyUI |
| Lip-synced talking video, any length | InfiniteTalk on Wan2.1-I2V-14B + lightx2v LoRA | ComfyUI |
| B-roll, and the bridge out of the source clip (first→last frame) | Wan2.2-I2V-A14B (fp8, high/low-noise experts) | ComfyUI |
| Music bed | ACE-Step 1.5 XL turbo | ComfyUI |

ComfyUI graphs use **core ComfyUI nodes only** (no custom nodes). They are built in Python (`afterstage/comfy/workflows.py`) and validated against ComfyUI's real node schemas. `tests/fixtures/object_info.json` is a snapshot of those schemas, and the workflows also passed a live ComfyUI server's own validator.

## Setup on the DGX Spark

```bash
git clone https://github.com/tianhaoz95/afterstage ~/afterstage && cd ~/afterstage
bash scripts/setup_spark.sh        # apt deps, uv, ComfyUI (torch cu130), afterstage venv, Ollama,
                                   # ~110 GB ComfyUI weights + LLM/VLM pulls, warms all caches
source scripts/afterstage.env
bash scripts/start_services.sh     # Ollama (systemd) + ComfyUI on 127.0.0.1:8188
.venv/bin/afterstage doctor --comfy-dir $COMFYUI_DIR
```

`doctor` checks the following:
- ffmpeg is installed.
- The LLM and VLM are being served.
- Chatterbox imports.
- ComfyUI is reachable.
- Every model file is present.
- Each of the four workflows validates against **your** ComfyUI install's live node schemas.

Use `SKIP_MODELS=1` to install software only, and `afterstage models download --groups talk,broll` to fetch weights selectively.

## Usage

```bash
# one shot
afterstage make concert.mp4 -o projects/podcast \
  --cast "Ari,Bora,Chae,Dami" \
  -p "A cosy podcast right after the concert. They laugh about the in-ear mishap and thank the fans."

# or step by step, with review points
afterstage new projects/podcast --clip concert.mp4 -p "..." --cast "Ari,Bora,Chae,Dami"
afterstage analyze projects/podcast          # → cast table; face crops in analysis/faces/
afterstage rename projects/podcast m3 "Chae" # fix names if the order was off
afterstage set-voice projects/podcast m1 --file ari_speech.wav   # best quality: clean speech
afterstage set-voice projects/podcast m2 --start 61.5 --end 70    # or a window of the clip's vocal stem
afterstage write projects/podcast            # → screenplay.json (edit it freely)
afterstage design projects/podcast           # → keyframes/ (re-roll: --shot s04)
afterstage render projects/podcast           # voices + video, cached per shot
afterstage edit projects/podcast             # → final/<title>.mp4 + .srt
afterstage --profile final run projects/podcast   # same project, 720p / full-step quality

afterstage ui --root projects                # web UI on 127.0.0.1:7860 (ssh -L 7860:localhost:7860 spark)
```

The prompt is open-ended. Other example requests:
- *an interview with an MC* (the writer adds non-clip people as `guests`)
- *backstage after the encore*
- *a continuation of the clip where the members walk off stage and greet fans*

### Edit-and-re-run is the main loop

Every artifact is content-addressed in `state.json`. If you edit one line in `screenplay.json`, the next `run` re-voices and re-renders **only that shot**, then re-cuts the edit. Changing a member's voice sample re-voices only their lines. Changing the profile re-renders everything at the new quality.

Useful overrides: `-s key=value`, for example `-s llm.model=gpt-oss:120b`, `-s writer.target_seconds=180`, `-s edit.include_source=false`, `-s render.profiles.final.width=1280`.

### Dry run (any machine, no GPU)

```bash
afterstage --dry-run make some.mp4 -p "a podcast" --cast "A,B" -o projects/dry
```

This uses a template writer, placeholder TTS tones and ffmpeg placeholder video. Every stage, the cache and the editor run for real, so you can check the plumbing anywhere. It's also what the test suite runs.

## Performance expectations (DGX Spark, rough estimates — measure on your unit)

GB10 has about 273 GB/s memory bandwidth and Blackwell FP8/FP4 compute; 128 GB of unified memory holds every model resident at once.

| Step | Draft (832×480, distilled LoRAs) | Final (1280×720, full steps) |
|---|---|---|
| Keyframe (Qwen-Image-Edit) | ~20–40 s | ~3–5 min |
| Talk, per 3.2 s window (InfiniteTalk 14B, 6 steps) | ~1.5–3 min | ~4–8 min |
| B-roll 5 s (Wan2.2 A14B) | ~2–4 min | ~15–25 min |
| Screenplay (Qwen3-32B) | ~1–2 min | — |

A 90-second podcast in draft mode takes a few hours, and final mode takes overnight. Iterate in draft, then render final once. Shots are rendered grouped by model so the 14B checkpoints aren't swapped back and forth.

## Configuration

`afterstage/default_config.yaml` is fully commented. Put overrides in a YAML passed with `--config`, or in `<project>/config.yaml`. The model filenames map to `afterstage/models.yaml`, which lists the download URLs. To swap a model (for example, a newer Wan or LTX release), update both files and the graph builder in `comfy/workflows.py`. `afterstage doctor` will then tell you whether the new graph matches your ComfyUI.

## Repository map

```
afterstage/
  cli.py            typer CLI           pipeline.py   stage orchestration
  config.py         config + locality guard            project.py    workspace + content-addressed cache
  schema.py         CastBible / Screenplay (pydantic)  llm.py        local OpenAI-compatible client + JSON repair loop
  analysis/         scenes, faces (cluster→members), audio (Demucs, Whisper)
  stages/           analyze, write, voice, design (keyframes + identity check), render
  tts/              Chatterbox Multilingual, dummy
  comfy/            graph builder + static validator, workflows, HTTP client
  render/           ComfyRenderer, DummyRenderer
  editor/           conform, concat, ducking, subtitles (Pillow-rendered, CJK fonts), badge, metadata
  ui/app.py         Gradio UI
scripts/            setup_spark.sh, start_services.sh
tests/              workflows vs. ComfyUI schema, core logic, end-to-end dry run, fake-ComfyUI client test
```

## Responsible use

This tool makes realistic video and cloned voices of real people.
- By default the output carries a visible **AI-generated** badge and an embedded provenance comment. Keep both.
- Use it for personal, fan or creative work where it's clear the result is synthetic.
- Don't publish content that could be mistaken for real statements by real artists. Don't use it to put words in someone's mouth that could harm them.
- Voice cloning a real person, and redistributing source footage, can carry legal obligations where you live.

## Status and known limitations

- **Not yet run on a real DGX Spark.** This repo was built and tested on a Mac without the GPU stack. What is verified:
  - The orchestration, cache, editor and UI run end to end with placeholder backends (`pytest`: 22 tests).
  - The ComfyUI graphs pass ComfyUI's validator.
  - The model URLs resolve.

  The GPU path still needs a first run on hardware. Most likely to need tweaks:
  - the Chatterbox dependency pins on aarch64
  - InfiniteTalk/Wan step counts for quality
  - memory headroom with `--highvram`
- Talk shots are single-speaker close/medium shots (InfiniteTalk `single_speaker`). Two-person lip-sync in one frame (`two_speakers` with masks) is supported by the node but not wired in yet.
- The wav2vec2 audio encoder InfiniteTalk uses was trained on Chinese speech. Korean and English lip-sync works but isn't perfect. LatentSync could be added as a post-pass.
- Concert audio is mostly singing, so supply clean speech samples via `set-voice --file` for convincing voices.
- Identity drift: keyframes are re-rolled when ArcFace similarity to the source face is below threshold. For long projects, a per-member LoRA trained on the face crops would be the next step.

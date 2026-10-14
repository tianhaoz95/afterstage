#!/usr/bin/env bash
# One-time setup of afterstage on an NVIDIA DGX Spark (GB10, DGX OS / Ubuntu 24.04, aarch64, CUDA 13).
#
#   bash scripts/setup_spark.sh            # everything (~140 GB of downloads: weights + LLMs)
#   SKIP_MODELS=1 bash scripts/setup_spark.sh   # software only
#
# Layout (override with env vars):
#   $COMFYUI_DIR   ~/ComfyUI        ComfyUI + its own venv (torch cu130)
#   $REPO/.venv                      afterstage venv (torch cu130, Whisper, Demucs, InsightFace, Chatterbox)
#   Ollama (system service)          local LLM + VLM server on 127.0.0.1:11434
#
# After setup nothing talks to the internet: weights are cached locally and HF_HUB_OFFLINE=1 is set
# in scripts/afterstage.env.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMFYUI_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"   # Blackwell (sm_121) needs CUDA 13 wheels
PY="${PY:-3.12}"
LLM_MODEL="${LLM_MODEL:-qwen3:32b}"
VLM_MODEL="${VLM_MODEL:-qwen3-vl:8b}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "warning: this script targets the DGX Spark (aarch64); continuing anyway" >&2
fi

log "system packages"
sudo apt-get update -y
sudo apt-get install -y ffmpeg git git-lfs build-essential python3-dev cmake pkg-config \
  libgl1 libglib2.0-0 fonts-noto-cjk curl

if ! command -v uv >/dev/null; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# --------------------------------------------------------------------------- ComfyUI
log "ComfyUI → $COMFYUI_DIR"
if [[ ! -d "$COMFYUI_DIR/.git" ]]; then
  git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFYUI_DIR"
else
  git -C "$COMFYUI_DIR" pull --ff-only || true
fi
uv venv -p "$PY" "$COMFYUI_DIR/.venv" --allow-existing
uv pip install -p "$COMFYUI_DIR/.venv/bin/python" torch torchvision torchaudio --index-url "$TORCH_INDEX"
uv pip install -p "$COMFYUI_DIR/.venv/bin/python" -r "$COMFYUI_DIR/requirements.txt"
"$COMFYUI_DIR/.venv/bin/python" - <<'EOF'
import torch
assert torch.cuda.is_available(), "CUDA not visible to PyTorch in the ComfyUI venv"
print("ComfyUI torch", torch.__version__, torch.cuda.get_device_name(0), "sm", torch.cuda.get_device_capability(0))
EOF

# --------------------------------------------------------------------------- afterstage venv
log "afterstage venv → $REPO/.venv"
uv venv -p "$PY" "$REPO/.venv" --allow-existing
CF="$REPO/.venv/bin/python"
uv pip install -p "$CF" torch torchvision torchaudio --index-url "$TORCH_INDEX"
uv pip install -p "$CF" -e "$REPO[gpu,ui,dev]"
# onnxruntime-gpu has no aarch64 wheel on PyPI; InsightFace runs fine on the 20-core Grace CPU.
# Chatterbox pins an old torch (x86-only CUDA wheels), so install it without deps and add its
# runtime requirements (as pinned by chatterbox-tts 0.1.7) around our cu130 torch.
uv pip install -p "$CF" --no-deps "chatterbox-tts==0.1.7"
uv pip install -p "$CF" "transformers==5.2.0" "diffusers==0.29.0" "resemble-perth>=1.0.0" "conformer==0.3.2" \
  "s3tokenizer" "safetensors==0.5.3" "librosa==0.11.0" "numpy<2" "pyloudnorm" "omegaconf" "pykakasi==2.3.0" \
  "spacy-pkuseg"
"$CF" - <<'EOF'
import torch, torchaudio
assert torch.cuda.is_available(), "CUDA not visible in the afterstage venv"
from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: F401
print("afterstage torch", torch.__version__, "ok")
EOF

# --------------------------------------------------------------------------- Ollama (LLM + VLM)
if ! command -v ollama >/dev/null; then
  log "installing Ollama"
  curl -fsSL https://ollama.com/install.sh | sh
fi
# keep the server on loopback only
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:11434"\nEnvironment="OLLAMA_KEEP_ALIVE=10m"\n' |
  sudo tee /etc/systemd/system/ollama.service.d/afterstage.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now ollama

if [[ "${SKIP_MODELS:-0}" != "1" ]]; then
  log "pulling LLM ($LLM_MODEL) and VLM ($VLM_MODEL)"
  ollama pull "$LLM_MODEL"
  ollama pull "$VLM_MODEL"

  log "ComfyUI weights (~110 GB, resumable)"
  COMFYUI_DIR="$COMFYUI_DIR" "$REPO/.venv/bin/afterstage" models download --comfy-dir "$COMFYUI_DIR"

  log "warming analysis + TTS caches (Whisper, Demucs, InsightFace, Chatterbox)"
  "$CF" - <<'EOF'
import subprocess, sys, tempfile, os
from huggingface_hub import snapshot_download
snapshot_download("openai/whisper-large-v3-turbo")
snapshot_download("ResembleAI/chatterbox")
from insightface.app import FaceAnalysis
FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"]).prepare(ctx_id=-1)
# demucs downloads htdemucs on first use; run it on a second of silence
d = tempfile.mkdtemp()
wav = os.path.join(d, "s.wav")
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1", wav], check=True)
subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", d, wav], check=True)
print("caches warm")
EOF
fi

cat > "$REPO/scripts/afterstage.env" <<EOF
# sourced by scripts/start_services.sh
export COMFYUI_DIR="$COMFYUI_DIR"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export GRADIO_ANALYTICS_ENABLED=False
export DO_NOT_TRACK=1
EOF

log "done. Next:"
echo "  source scripts/afterstage.env && bash scripts/start_services.sh"
echo "  .venv/bin/afterstage doctor --comfy-dir \$COMFYUI_DIR"
echo "  .venv/bin/afterstage make concert.mp4 -p 'a podcast after the show' --cast 'A,B,C,D' -o projects/podcast"

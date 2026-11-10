#!/usr/bin/env bash
# One-line installer + runner for the distributed Rotor-Cuda client (Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USER/Rotor-Cuda-Distributed/main/client/install.sh | bash -s -- --server https://YOUR-APP.up.railway.app --token CLIENT_TOKEN
#
# All arguments after `--` are passed to client/rotor_client.py (see --help there).
# Env: REPO_URL, INSTALL_DIR, CUDA_HOME, CCAP (override GPU compute capability, e.g. 8.6)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/YOUR_GITHUB_USER/Rotor-Cuda-Distributed.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Rotor-Cuda-Distributed}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null && SUDO="sudo"

log() { echo -e "\033[1;32m[install]\033[0m $*"; }

# ---- 1. system packages ---------------------------------------------------
need=()
for c in git g++ make python3; do command -v "$c" >/dev/null || need+=("$c"); done
if [ ! -f /usr/include/gmp.h ] && [ ! -f /usr/include/x86_64-linux-gnu/gmp.h ]; then need+=("libgmp-dev"); fi
if [ ${#need[@]} -gt 0 ]; then
  log "installing missing packages: ${need[*]}"
  if command -v apt-get >/dev/null; then
    $SUDO apt-get update -y && $SUDO apt-get install -y git build-essential libgmp-dev python3
  elif command -v dnf >/dev/null; then
    $SUDO dnf install -y git gcc-c++ make gmp-devel python3
  else
    echo "Install git, g++, make, libgmp-dev, python3 manually." >&2; exit 1
  fi
fi

# ---- 2. get / update the source ------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  log "updating $INSTALL_DIR"; git -C "$INSTALL_DIR" pull --ff-only || true
else
  log "cloning $REPO_URL"; git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# ---- 3. build Rotor from SOURCE (never uses prebuilt binaries) -------------
cd "$INSTALL_DIR/Rotor-Cuda"
MODE_ARGS=()
if [ -x "$CUDA_HOME/bin/nvcc" ] && command -v nvidia-smi >/dev/null; then
  CC="${CCAP:-$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n1 | tr -d ' ')}"
  log "GPU build, compute capability $CC"
  if [ ! -x Rotor ] || [ -n "${REBUILD:-}" ]; then
    make clean >/dev/null 2>&1 || true
    make -j"$(nproc)" gpu=1 CCAP="$CC" CUDA="$CUDA_HOME"
  fi
else
  log "CUDA toolkit or nvidia-smi not found -> CPU-only build"
  MODE_ARGS=(--cpu)
  if [ ! -x Rotor ] || [ -n "${REBUILD:-}" ]; then
    make clean >/dev/null 2>&1 || true
    make -j"$(nproc)"
  fi
fi

# ---- 4. run ---------------------------------------------------------------
log "starting client (Ctrl+C to stop; use tmux/screen to keep it running)"
exec python3 "$INSTALL_DIR/client/rotor_client.py" "${MODE_ARGS[@]}" "$@"

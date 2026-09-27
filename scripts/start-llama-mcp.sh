#!/usr/bin/env bash
set -euo pipefail

export LD_LIBRARY_PATH="$HOME/local/rocm-gfx906/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GGML_CUDA_DISABLE_GRAPHS=1
export HSA_OVERRIDE_GFX_VERSION=9.0.6

exec /home/otherdrums/llama.cpp/build/bin/llama-server \
  -m /home/otherdrums/models/Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf \
  --port 8081 \
  --host 0.0.0.0 \
  --ctx-size 262144 \
  -np 1 \
  -fa on \
  --jinja \
  -ngl 99 \
  -ncmoe 22 \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  --spec-draft-p-min 0.6 \
  --no-mmap \
  -kvu \
  --reasoning-preserve \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --temp 0.6 \
  --top-p 0.95 \
  --top-k 20 \
  -n -1 \
  --cors-origins http://192.168.10.222:8081

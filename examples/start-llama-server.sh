# One tuned llama-server configuration, kept as an example rather than as a script.
#
# This is the configuration the evaluation numbers in docs/findings.md were measured under: a 35B
# MoE coder model on a single AMD card, with multi-token prediction drafting and quantised KV cache.
# None of that is required by the MCP server, which only speaks HTTP to whatever is on the port, and
# none of it is a sensible default for anyone else. The hardware-specific lines are grouped at the top
# so they are obvious rather than buried among the sampling parameters.
#
# Copy this, delete what does not apply, and set the two required values:
#
#   LLAMA_SERVER_BIN   path to llama-server
#   LLAMA_MODEL        path to a .gguf
#
# On the reasoning mode: this server's own measurements were taken with thinking disabled, which is
# worth about two cases in thirty-three. See docs/findings.md.
set -euo pipefail

: "${LLAMA_SERVER_BIN:?set LLAMA_SERVER_BIN to your llama-server}"
: "${LLAMA_MODEL:?set LLAMA_MODEL to a .gguf}"

# --- hardware-specific ----------------------------------------------------------------------
# The three lines below are for an AMD gfx906 card. On other hardware, delete them.
export LD_LIBRARY_PATH="$HOME/local/rocm-gfx906/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GGML_CUDA_DISABLE_GRAPHS=1
export HSA_OVERRIDE_GFX_VERSION=9.0.6

# Multi-token prediction drafting. Only valid for a model built for it; delete for a plain model.
DRAFT_ARGS=(--spec-type draft-mtp --spec-draft-n-max 2 --spec-draft-p-min 0.6)

# Sampling defaults chosen to match what the evaluation harness sends, so a manual session and a
# measured one behave the same way.
exec "$LLAMA_SERVER_BIN" \
  -m "$LLAMA_MODEL" \
  --port 8081 \
  --host 0.0.0.0 \
  --ctx-size 262144 \
  -np 1 \
  -fa on \
  --jinja \
  -ngl 99 \
  "${DRAFT_ARGS[@]}" \
  --no-mmap \
  -kvu \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --temp 0.6 \
  --top-p 0.95 \
  --top-k 20 \
  -n -1 \
  --cors-origins "http://${BENTHIC_MCP_HOST:-localhost}:8081"

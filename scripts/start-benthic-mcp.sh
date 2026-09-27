#!/usr/bin/env bash
set -euo pipefail

export BENTHIC_MCP_ENV_FILE="${BENTHIC_MCP_ENV_FILE:-$HOME/.config/benthic-mcp/env}"
set -a
source "$BENTHIC_MCP_ENV_FILE"
set +a

exec /home/otherdrums/mcp-tools/.venv/bin/benthic-mcp

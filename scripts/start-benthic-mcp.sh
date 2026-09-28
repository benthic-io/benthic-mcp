#!/usr/bin/env bash
# Start the Benthic MCP server with the settings from the env file.
#
# The checkout location is derived from this script's own path, so it works wherever the repository
# lives. Set BENTHIC_MCP_ROOT to override it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="${BENTHIC_MCP_ROOT:-$(dirname "$here")}"

export BENTHIC_MCP_ENV_FILE="${BENTHIC_MCP_ENV_FILE:-$HOME/.config/benthic-mcp/env}"
set -a
source "$BENTHIC_MCP_ENV_FILE"
set +a

exec "$root/.venv/bin/benthic-mcp"

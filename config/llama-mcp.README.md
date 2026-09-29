# Two MCP configs, two different formats
#
# llama.cpp has two independent MCP clients and they do not share a config format. Passing one to
# the other fails silently, which is the trap this file exists to prevent.
#
# | client | configured by | format | transports |
# | --- | --- | --- | --- |
# | llama-server itself | `--mcp-servers-config` | object with `mcpServers` | stdio only |
# | llama.cpp Web UI | its own MCP settings dialog | array of server objects | HTTP and SSE |
#
# `config/llama-mcp-servers.example.json` is for `--mcp-servers-config`. That client spawns
# `command` as a child process and speaks NDJSON over its stdin and stdout; it has no HTTP
# transport, so `url` and `headers` in that file would be ignored and the entry dropped for having no
# `command`. The server therefore has to serve MCP over stdio, which it does with
# `BENTHIC_MCP_TRANSPORT=stdio`.
#
# `config/llama-mcp.example.json` is for the Web UI, which runs the browser MCP SDK and can reach an
# HTTP server, so it needs the URL and the bearer token. That file is a top-level array on purpose.
#
# ## The two ways to fail silently
#
# Both of these log a warning and expose no tools, rather than raising:
#
#   - A top-level array where an object with `mcpServers` is expected. The parser returns an empty
#     list on the first check and never inspects anything else, so the log reads
#     "MCP config: no servers found in JSON" and nothing more.
#   - An entry with no `command`, which is skipped with a warning naming the server.
#
# Malformed JSON that does throw aborts startup, so a partial fix is worse than no config at all.
#
# ## Accepted schema for `--mcp-servers-config`
#
#   {
#     "mcpServers": {
#       "<name>": {
#         "command": "/path/to/executable",     required; the entry is skipped without it
#         "args": ["--flag"],                   argv, appended after the command
#         "env": {"KEY": "VALUE"},              merged over the parent environment
#         "cwd": "/optional/working/dir",
#         "timeout_ms": 30000                   per tool call; the default is 30000
#       }
#     }
#   }
#
# The server name is the object key, not a field. Unknown keys are dropped without complaint.
#
# ## What to expect once it connects
#
#   - Wire names are prefixed with the server name, so the tools are `benthic_benthic_discover`,
#     `benthic_benthic_query`, and so on. A name that collides with a builtin tool is skipped with a
#     warning.
#   - `GET /tools` lists them with `"type": "mcp"`.
#   - Startup warmup spawns each server once to list tools, capped at 10 seconds per server
#     (`MCP_WARMUP_TIMEOUT_SECONDS`). A cold stdio spawn here is about two seconds.
#   - A transport that fails to spawn is retried lazily after a cooldown (`MCP_COOLDOWN_SECONDS`).
#   - MCP is disabled by default and has to be enabled with `--mcp-servers-config`.
#
# Registering the tools is necessary but not sufficient. `/tools` is a Web-UI-internal registry, and
# a chat completion only sees the tools its request carries, so a client driving the OpenAI-compatible
# endpoint has to fetch the definitions and pass them in `tools`. The Web UI does this itself; a
# script has to do it explicitly.
#
# `--ui-mcp-proxy` is unrelated to any of this. It is the Web UI's CORS proxy for reaching an MCP
# server from the browser, not a transport for llama-server, and enabling it will not help a
# stdio-only client connect.

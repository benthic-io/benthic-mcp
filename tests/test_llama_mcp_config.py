"""llama.cpp has two MCP clients with two incompatible config formats.

The bug these exist to prevent was passing the Web UI's config to `--mcp-servers-config`. Both files
parse as valid JSON and both fail silently, so nothing but a test against each parser's own gate
catches it: the server-side parser returns an empty list on the first shape check, and the server-side
entry is then skipped for having no `command`. The only symptom is a warning line and no tools.
"""

import json
from pathlib import Path

CONFIG = Path(__file__).parents[1] / "config"


def parse_servers_config(document: object) -> list[dict]:
    """Replicates server_mcp_server_config::parse_cursor_format, the gate at server-mcp.cpp:142."""
    if not isinstance(document, dict):
        return []
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    parsed = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or not cfg.get("command"):
            continue  # logged as "has no command, skipping"
        parsed.append({"name": name, **cfg})
    return parsed


def test_the_servers_config_example_yields_a_server() -> None:
    document = json.loads((CONFIG / "llama-mcp-servers.example.json").read_text(encoding="utf-8"))

    servers = parse_servers_config(document)

    assert len(servers) == 1
    assert servers[0]["name"] == "benthic"
    assert servers[0]["env"]["BENTHIC_MCP_TRANSPORT"] == "stdio"


def test_the_web_ui_config_is_an_array_with_a_url() -> None:
    document = json.loads((CONFIG / "llama-mcp.example.json").read_text(encoding="utf-8"))

    assert isinstance(document, list)
    server = document[0]
    assert server["id"] == "benthic"
    assert server["url"].startswith("http://")
    assert json.loads(server["headers"])["Authorization"].startswith("Bearer ")


def test_the_two_formats_are_not_interchangeable() -> None:
    """The actual bug: each file is rejected by the other client's parser."""
    web_ui = json.loads((CONFIG / "llama-mcp.example.json").read_text(encoding="utf-8"))
    servers_config = json.loads((CONFIG / "llama-mcp-servers.example.json").read_text(encoding="utf-8"))

    assert parse_servers_config(web_ui) == []
    assert not isinstance(servers_config, list)


def test_the_servers_config_asks_for_stdio_and_not_a_url() -> None:
    """A remote transport is not merely unsupported here, it is silently ignored.

    The parser reads command, args, env, cwd and timeout_ms. A url or headers key would be dropped
    and the entry would then be skipped for having no command, which is why the server speaks stdio.
    """
    document = json.loads((CONFIG / "llama-mcp-servers.example.json").read_text(encoding="utf-8"))
    entry = document["mcpServers"]["benthic"]

    assert "url" not in entry
    assert "headers" not in entry
    assert entry["command"]


def test_the_servers_config_needs_no_bearer_token() -> None:
    """stdio has no HTTP layer, so demanding a token would block the only working transport."""
    document = json.loads((CONFIG / "llama-mcp-servers.example.json").read_text(encoding="utf-8"))

    assert "BENTHIC_MCP_BEARER_TOKEN" not in document["mcpServers"]["benthic"].get("env", {})

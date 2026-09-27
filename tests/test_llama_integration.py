import json
from pathlib import Path


def test_example_web_ui_configuration_is_valid() -> None:
    config = Path(__file__).parents[1] / "config" / "llama-mcp.example.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
    server = data[0]
    assert server["id"] == "benthic"
    assert server["enabled"] is True
    assert server["url"] == "http://192.168.10.222:8082/mcp"
    assert server["useProxy"] is False
    headers = json.loads(server["headers"])
    assert headers["Authorization"] == "Bearer REPLACE_WITH_BENTHIC_MCP_TOKEN"

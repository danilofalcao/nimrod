import json

from nimrod.config import Config
from nimrod.install import install_pi_extension, install_pi_mcp


def _config(tmp_path):
    return Config(home=tmp_path / "home", pi_root=tmp_path / "pi")


def test_install_pi_mcp_merges_and_is_idempotent(tmp_path):
    config = _config(tmp_path)
    config.pi_root.mkdir(parents=True)
    mcp_path = config.pi_root / "mcp.json"
    mcp_path.write_text(json.dumps({
        "mcpServers": {"other": {"command": "other", "args": []}},
    }), encoding="utf-8")

    install_pi_mcp(config, "/opt/nimrod", dry=False)

    data = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["nimrod"] == {"command": "/opt/nimrod", "args": ["serve"]}
    assert data["mcpServers"]["other"]["command"] == "other"

    settings = json.loads((config.pi_root / "settings.json").read_text(encoding="utf-8"))
    assert "npm:pi-mcp-adapter" in settings["packages"]

    # Second run must not duplicate anything.
    install_pi_mcp(config, "/opt/nimrod", dry=False)
    data2 = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert list(data2["mcpServers"]).count("nimrod") == 1
    settings2 = json.loads((config.pi_root / "settings.json").read_text(encoding="utf-8"))
    assert settings2["packages"].count("npm:pi-mcp-adapter") == 1


def test_install_pi_extension_renders_binary(tmp_path):
    config = _config(tmp_path)
    target = install_pi_extension(config, "/opt/nimrod", dry=False)
    assert target == config.pi_root / "extensions" / "nimrod.ts"
    text = target.read_text(encoding="utf-8")
    assert "__NIMROD_BIN__" not in text
    assert "/opt/nimrod" in text
    assert "session_start" in text and "before_agent_start" in text

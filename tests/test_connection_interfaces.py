"""Shared configuration, CLI, MCP and HTTP compatibility without hardware."""
import asyncio
import importlib
import json
import sys

import pytest

from fake_phone import build
from test_transport import Backend


@pytest.fixture
def configured(tmp_path, monkeypatch):
    from homescreen import config
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "LAST_DEVICE", tmp_path / "data/last-device.txt")
    monkeypatch.setattr(config, "_session", None)
    monkeypatch.setattr(config, "_sessions", {})
    monkeypatch.setattr(config, "_current", None)
    monkeypatch.setattr(config, "_switching", False)
    for key in ("HOMESCREEN_FIXTURE", "HOMESCREEN_SIM", "HOMESCREEN_UDID", "HOMESCREEN_TRANSPORT"):
        monkeypatch.delenv(key, raising=False)
    return config


def test_transport_configuration_preserves_device_and_draft(configured):
    config = configured
    s = config.session()
    s.source.udid = "A"
    s.device_id = "A"
    assert config.configure_connection("wifi") is s
    assert s.source.udid == "A" and s.source.connection.mode == "wifi"
    assert config.configure_connection("auto", "B").source.udid == "B"
    with pytest.raises(ValueError):
        config.configure_connection("unsafe")


def test_environment_keeps_usb_default_and_fixture_precedence(configured, monkeypatch, tmp_path):
    config = configured
    assert config.make_source().connection.mode == "usb"
    monkeypatch.setenv("HOMESCREEN_TRANSPORT", "wifi")
    monkeypatch.setenv("HOMESCREEN_UDID", "A")
    assert config.make_source().connection.mode == "wifi"
    path = tmp_path / "fixture.plist"
    build(path)
    monkeypatch.setenv("HOMESCREEN_FIXTURE", str(path))
    assert config.make_source().path == path


def test_reconnect_keeps_draft_and_recovery_guard(configured):
    config = configured
    s = config.configure_connection("wifi", "A")
    s.source.connection.backend = Backend(wifi=["A"])
    s._pending_path.write_text('{}')
    result = asyncio.run(config.reconnect())
    assert result["udid"] == "A" and result["needs_read"]
    assert s.layout is None  # connect/reconnect never reads/replaces/applies a draft


def test_cli_flags_devices_and_legacy_fixture_read(configured, monkeypatch, capsys, tmp_path):
    from homescreen import cli, transport
    async def devices():
        return [{"udid": "A", "methods": ["wifi"]}]
    monkeypatch.setattr(transport, "discover_devices", devices)
    monkeypatch.setattr(sys, "argv", ["homescreen", "--transport", "wifi", "--udid", "A", "devices"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)[0]["udid"] == "A"
    path = tmp_path / "fixture.plist"
    build(path)
    monkeypatch.setenv("HOMESCREEN_FIXTURE", str(path))
    monkeypatch.setattr(configured, "_session", None)
    monkeypatch.setattr(sys, "argv", ["homescreen", "read"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert "device" in json.loads(capsys.readouterr().out)


def test_mcp_connection_tools_keep_write_gate(configured, monkeypatch):
    from homescreen import server
    monkeypatch.setattr(server, "ALLOW_APPLY", False)
    config = configured
    s = config.configure_connection("auto", "A")
    s.source.connection.backend = Backend(usb=["A"], wifi=["A"])
    result = asyncio.run(server.connect_device("wifi", "A"))
    assert result["transport"] == "wifi"
    assert asyncio.run(server.reconnect_device())["udid"] == "A"
    assert asyncio.run(server.apply("anything"))["ok"] is False
    assert asyncio.run(server.restore("anything"))["ok"] is False
    assert s.layout is None


def test_http_connection_controls_are_guarded_and_do_not_write(configured, monkeypatch):
    from starlette.testclient import TestClient
    from homescreen import ui
    config = configured
    s = config.configure_connection("usb", "A")
    s.source.connection.backend = Backend(usb=["A"], wifi=["A"])
    with TestClient(ui.app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/connection", json={"mode": "wifi"}).status_code == 403
        headers = {"X-Token": ui.TOKEN}
        result = client.post("/api/connection", json={"mode": "wifi"}, headers=headers)
        assert result.status_code == 200 and result.json()["connection"]["mode"] == "wifi"
        result = client.post("/api/reconnect", json={}, headers=headers)
        assert result.json()["connection"]["transport"] == "wifi"
        assert client.post("/api/connection", json={"mode": "unsafe"}, headers=headers).status_code == 400
        assert s.layout is None


def test_stdio_mcp_registers_wireless_tools_and_preserves_fixture_workflow(tmp_path):
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    path = tmp_path / "fixture.plist"
    build(path, as_dict=True)
    async def check():
        params = StdioServerParameters(command=sys.executable, args=["-m", "homescreen.server"],
            env={**os.environ, "HOMESCREEN_FIXTURE": str(path), "HOMESCREEN_DATA": str(tmp_path / "data"),
                 "HOMESCREEN_ALLOW_APPLY": "0"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                names = {t.name for t in (await client.list_tools()).tools}
                assert {"list_devices", "connect_device", "reconnect_device", "pair_wifi", "read_phone", "apply", "restore"} <= names
                result = await client.call_tool("read_phone", {})
                assert not result.is_error
                result = await client.call_tool("show_layout", {})
                assert not result.is_error and "library" in str(result)
                result = await client.call_tool("apply", {"apply_token": "invalid"})
                assert "off" in str(result)
    asyncio.run(check())


def test_cli_ui_honors_transport_flag_after_config_import(configured, monkeypatch):
    from homescreen import cli, ui
    observed = []
    def main(*args):
        configured.enable_device_switching()
        observed.append(configured.session().source.connection.mode)
    monkeypatch.setattr(ui, "main", main)
    monkeypatch.setattr(sys, "argv", ["homescreen", "--transport", "wifi", "ui", "--no-browser"])
    cli.main()
    assert observed == ["wifi"]

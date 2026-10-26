"""Integration system: MCP over stdio, HTTP actions and plugins, end to end."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app import config, integrations
from app.tools import REGISTRY


@pytest.fixture
def setup_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIRM_LEVEL", "normal")
    monkeypatch.setattr(integrations, "INTEGRATIONS_FILE", tmp_path / "integrations.json")
    monkeypatch.setattr(integrations, "PLUGINS_DIR", tmp_path / "plugins")
    (tmp_path / "plugins").mkdir()
    yield tmp_path
    integrations.shutdown()
    for key in [k for k, t in REGISTRY.items() if t.source != "builtin"]:
        REGISTRY.pop(key)


def _serve(handler_body):
    received = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received["path"] = self.path
            received["auth"] = self.headers.get("Authorization")
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(handler_body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, received


def test_mcp_http_and_plugin_load(setup_files, monkeypatch):
    srv, received = _serve(b'{"ok": true}')
    monkeypatch.setenv("TEST_TOKEN", "s3cret")
    fake = Path(__file__).parent / "fake_mcp_server.py"
    (setup_files / "integrations.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "demo": {"command": sys.executable, "args": [str(fake)]},
                    "off": {"enabled": False, "command": "nope"},
                    "broken": {"command": "definitely-not-a-real-binary-xyz"},
                },
                "http_actions": [
                    {
                        "name": "lights",
                        "description": "lights",
                        "method": "POST",
                        "url": f"http://127.0.0.1:{srv.server_port}/api/{{action}}",
                        "headers": {"Authorization": "Bearer ${TEST_TOKEN}"},
                        "body": {"entity_id": "{entity}", "brightness": "{level}"},
                        "params": {
                            "action": {"type": "string"},
                            "entity": {"type": "string"},
                            "level": {"type": "integer", "optional": True},
                        },
                    }
                ],
            }
        )
    )
    (setup_files / "plugins" / "hello.py").write_text(
        "from app.tools import tool\n"
        "@tool('say_hello', 'greets', {'name': {'type': 'string'}}, ['name'])\n"
        "def say_hello(name):\n    return 'hi ' + name\n"
    )
    (setup_files / "plugins" / "bad.py").write_text("raise RuntimeError('boom')\n")

    status = {s["name"]: s for s in integrations.load()}
    assert status["demo"]["status"] == "connected" and status["demo"]["tools"] == 2
    assert status["off"]["status"] == "disabled"
    assert status["broken"]["status"] == "error"
    assert status["hello"]["status"] == "connected"
    assert status["bad"]["status"] == "error" and "boom" in status["bad"]["error"]

    echo = REGISTRY["demo_echo"]
    assert echo.source == "mcp:demo"
    assert echo.confirmation_reason({"text": "x"}) is None  # read-only
    assert REGISTRY["demo_delete_item"].confirmation_reason({"id": 1})  # looks destructive
    assert echo.run({"text": "ping"}).text == "echo: ping"

    res = REGISTRY["lights"].run({"action": "turn_on", "entity": "light.desk", "level": 80})
    assert res.ok
    assert received["path"] == "/api/turn_on"
    assert received["auth"] == "Bearer s3cret"
    assert received["body"] == {"entity_id": "light.desk", "brightness": 80}

    assert REGISTRY["say_hello"].run({"name": "Muneeb"}).text == "hi Muneeb"

    # Reload with an empty config removes everything that isn't built in.
    (setup_files / "integrations.json").write_text("{}")
    (setup_files / "plugins" / "hello.py").unlink()
    (setup_files / "plugins" / "bad.py").unlink()
    integrations.load()
    assert "demo_echo" not in REGISTRY and "lights" not in REGISTRY and "say_hello" not in REGISTRY
    assert "open_app" in REGISTRY
    srv.shutdown()


def test_bad_json_is_reported(setup_files):
    (setup_files / "integrations.json").write_text("{not json")
    status = integrations.load()
    assert status[-1]["status"] == "error" and "not valid JSON" in status[-1]["error"]

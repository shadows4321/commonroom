import dataclasses

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from commonroom_protocol import API_PREFIX, PROTOCOL_VERSION
from commonroom_server.app import DEFAULT_VIEWER_DIR, create_app
from commonroom_server.config import ConfigError, load_agents

ADMIN = f"{API_PREFIX}/admin"
AUTH = {"Authorization": "Bearer host-secret"}


@pytest.fixture
def client(config, agents, tmp_path):
    agents = dataclasses.replace(agents, admin_password="host-secret")
    app = create_app(config, agents, viewer_dir=DEFAULT_VIEWER_DIR, clock_interval=0, avatars_dir=tmp_path)
    with TestClient(app) as c:
        yield c


def register(ws, token):
    ws.send_json({"type": "register", "id": "r", "protocol": PROTOCOL_VERSION, "token": token})
    return ws.receive_json()


def test_admin_is_off_without_a_password(config, agents, tmp_path):
    app = create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path)
    with TestClient(app) as c:
        assert c.get(ADMIN, headers=AUTH).status_code == 404


def test_admin_needs_the_password(client):
    assert client.get(ADMIN).status_code == 401
    assert client.get(ADMIN, headers={"Authorization": "Bearer tok-mia"}).status_code == 401
    assert client.post(f"{ADMIN}/agents/mia/suspend").status_code == 401


def test_admin_page_is_served(client):
    page = client.get("/admin")
    assert page.status_code == 200 and "Host sign-in" in page.text
    assert client.get("/viewer/admin.js").status_code == 200


def test_status_lists_agents(client):
    with client.websocket_connect(f"{API_PREFIX}/ws") as ws:
        assert register(ws, "tok-mia")["type"] == "welcome"
        status = client.get(ADMIN, headers=AUTH).json()
    assert status["counts"]["rooms"] == 3 and {r["id"] for r in status["rooms"]} == {"hall", "kitchen", "study"}
    agents = {a["id"]: a for a in status["agents"]}
    assert agents["mia"]["connected"] and agents["mia"]["room"] == "hall"
    assert not agents["otto"]["connected"] and agents["otto"]["room"] is None  # never joined
    assert agents["kai"]["home"] == "study"


def test_move_an_agent(client):
    with client.websocket_connect(f"{API_PREFIX}/ws") as ws:
        register(ws, "tok-mia")
        res = client.post(f"{ADMIN}/agents/mia/move", json={"room": "kitchen"}, headers=AUTH)
        assert res.status_code == 200
        assert next(a for a in res.json()["agents"] if a["id"] == "mia")["room"] == "kitchen"
        while (msg := ws.receive_json())["type"] != "left_room":
            pass
        assert msg["to"] == "kitchen" and msg["via"] is None
    res = client.post(f"{ADMIN}/agents/mia/move", json={"room": None}, headers=AUTH)  # home
    assert next(a for a in res.json()["agents"] if a["id"] == "mia")["room"] == "hall"
    assert client.post(f"{ADMIN}/agents/mia/move", json={"room": "attic"}, headers=AUTH).status_code == 404
    assert client.post(f"{ADMIN}/agents/nobody/move", json={"room": "hall"}, headers=AUTH).status_code == 404
    assert client.post(f"{ADMIN}/agents/mia/move", json=["hall"], headers=AUTH).status_code == 400


def test_suspend_and_resume(client):
    with client.websocket_connect(f"{API_PREFIX}/ws") as ws:
        register(ws, "tok-mia")
        res = client.post(f"{ADMIN}/agents/mia/suspend", headers=AUTH)
        mia = next(a for a in res.json()["agents"] if a["id"] == "mia")
        assert mia["suspended"]
        while (msg := ws.receive_json())["type"] != "error":
            pass
        assert msg["reason"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with client.websocket_connect(f"{API_PREFIX}/ws") as ws:
        reply = register(ws, "tok-mia")
        assert reply["type"] == "error" and reply["reason"] == "unauthorized"
    mia = next(a for a in client.get(ADMIN, headers=AUTH).json()["agents"] if a["id"] == "mia")
    assert not mia["connected"]
    client.post(f"{ADMIN}/agents/mia/resume", headers=AUTH)
    with client.websocket_connect(f"{API_PREFIX}/ws") as ws:
        assert register(ws, "tok-mia")["type"] == "welcome"


def test_remove_avatar(client):
    from avatar_files import png
    client.put(f"{API_PREFIX}/avatar", content=png(), headers={"Authorization": "Bearer tok-otto"})
    res = client.delete(f"{ADMIN}/agents/otto/avatar", headers=AUTH)
    assert next(a for a in res.json()["agents"] if a["id"] == "otto")["avatar"] is None


def test_admin_password_config(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("viewer_password: viewer-pass\nadmin_password: admin-pass\n"
                    "agents: [{id: mia, token: tok-mia-long}]\n")
    assert load_agents(path).admin_password == "admin-pass"
    for bad, match in [("short", "at least 8"), ("viewer-pass", "differ"), ("tok-mia-long", "differ")]:
        path.write_text(f"viewer_password: viewer-pass\nadmin_password: {bad}\nagents: [{{id: mia, token: tok-mia-long}}]\n")
        with pytest.raises(ConfigError, match=match):
            load_agents(path)


def test_set_admin_password_cli(tmp_path):
    from commonroom_server.cli import main
    (tmp_path / "agents.yaml").write_text("viewer_password: viewer-pass\nagents: []\n")
    main(["set-admin-password", "the-host-pass", "--world", str(tmp_path)])
    cfg = load_agents(tmp_path / "agents.yaml")
    assert cfg.admin_password == "the-host-pass" and cfg.viewer_password == "viewer-pass"

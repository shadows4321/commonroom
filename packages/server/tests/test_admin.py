import dataclasses
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from commonroom_protocol import API_PREFIX, PROTOCOL_VERSION
from commonroom_server.app import DEFAULT_VIEWER_DIR, create_app
from test_schema import validate

from commonroom_server.config import AgentsConfig, AgentSpec, ConfigError, load_agents, load_world

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


# ------------------------------------------------------------- houses

TOWN = Path(__file__).resolve().parents[3] / "worlds" / "town"
TOWN_AGENTS = AgentsConfig(agents={
    "mia": AgentSpec("mia", "Mia", "tok-mia"),
    "otto": AgentSpec("otto", "Otto", "tok-otto"),
    "kai": AgentSpec("kai", "Kai", "tok-kai"),
}, viewer_password="open-sesame", admin_password="host-secret")


@pytest.fixture
def town_dir(tmp_path):
    target = tmp_path / "town"
    shutil.copytree(TOWN, target)
    return target


def town_client(town_dir):
    return TestClient(create_app(load_world(town_dir), TOWN_AGENTS, viewer_dir=None, clock_interval=0,
                                 avatars_dir=town_dir / "avatars"))


def build(client, owner, data, **query):
    return client.post(f"{ADMIN}/houses/{owner}", content=data, params=query, headers=AUTH)


def until(ws, kind):
    while (msg := ws.receive_json())["type"] != kind:
        pass
    return msg


def test_building_a_house_from_the_admin_page(town_dir, tmp_path):
    model = (TOWN / "houses" / "otto.glb").read_bytes()
    world_yaml = (town_dir / "world.yaml").read_text()
    with town_client(town_dir) as client, \
            client.websocket_connect(f"{API_PREFIX}/ws") as kai, \
            client.websocket_connect(f"{API_PREFIX}/ws/viewer?token=open-sesame") as viewer:
        assert register(kai, "tok-kai")["snapshot"]["room"]["id"] == "square"  # no house yet
        assert viewer.receive_json()["type"] == "world_state"
        kai.send_json({"type": "move", "id": "m", "to": "street_west"})
        until(kai, "ack")
        status = client.get(ADMIN, headers=AUTH).json()
        assert status["can_add_houses"] and {p["id"]: p["owner"] for p in status["plots"]}["3"] is None

        checked = build(client, "kai", model, check="true").json()["house"]
        assert checked == {"owner": "kai", "id": "kai", "shared": False, "plot": "3", "rooms": 4, "objects": 8,
                           "new": True, "warnings": [], "written": False}
        assert (town_dir / "world.yaml").read_text() == world_yaml and not (town_dir / "houses" / "kai.glb").exists()

        res = build(client, "kai", model)
        assert res.status_code == 200, res.text
        assert res.json()["house"]["written"] and res.json()["status"]["counts"]["rooms"] == 16
        changed = until(kai, "world_changed")
        validate(changed)
        assert changed["message"] == "Kai's house has been built." and changed["room"] is None
        assert {"direction": "Kai's house", "to": "kai.garden", "to_name": "Kai's Garden"} in \
            changed["snapshot"]["room"]["exits"]
        state = until(viewer, "world_state")
        validate(state)
        area = next(a for a in state["areas"] if a["id"] == "kai")
        assert area["plot"] == "3" and client.get(area["model_url"], headers={"Authorization": "Bearer open-sesame"}
                                                  ).content == model
        assert any(e["type"] == "world_changed" for e in state["recent_events"])
        kai.send_json({"type": "move", "id": "home", "to": "Kai's house"})
        assert until(kai, "ack")["result"]["room"] == "kai.garden"

        rebuilt = build(client, "kai", model).json()["house"]
        assert rebuilt["new"] is False and rebuilt["plot"] == "3"
        assert until(kai, "world_changed")["message"] == "Kai's house has been rebuilt."
        world = client.app.state.hub.world  # people carried over both reloads, still connected
        assert world.entities["kai"].room == "kai.garden" and world.entities["kai"].online
    assert load_world(town_dir).homes["kai"] == "kai.garden"  # and it's on disk for the next start


def test_building_a_shared_house_from_the_admin_page(town_dir):
    model = (TOWN / "houses" / "otto.glb").read_bytes()
    with town_client(town_dir) as client, client.websocket_connect(f"{API_PREFIX}/ws") as kai:
        register(kai, "tok-kai")
        checked = build(client, "library", model, check="true", shared="true", name="The Library").json()["house"]
        assert checked["shared"] and checked["owner"] is None and checked["id"] == "library" and checked["new"]
        res = build(client, "library", model, shared="true", name="The Library")
        assert res.status_code == 200, res.text
        assert until(kai, "world_changed")["message"] == "The Library has been built."
        plot = next(p for p in res.json()["status"]["plots"] if p["house"] == "library")
        assert plot["owner"] is None and plot["house_name"] == "The Library"
        kai.send_json({"type": "move", "id": "go", "to": "street_west"})
        until(kai, "ack")
        kai.send_json({"type": "move", "id": "in", "to": "The Library"})
        assert until(kai, "ack")["result"]["room"] == "library.garden"
        assert build(client, "library", model, shared="true").json()["house"]["new"] is False
        assert build(client, "mia", model, shared="true").status_code == 422  # an AI's house already
    cfg = load_world(town_dir)
    assert cfg.areas["library"].owner is None and "library" not in cfg.homes


def test_a_bad_house_changes_nothing(town_dir, tmp_path):
    world_yaml = (town_dir / "world.yaml").read_text()
    model = (TOWN / "houses" / "otto.glb").read_bytes()
    with town_client(town_dir) as client:
        res = build(client, "kai", b"not a model")
        assert res.status_code == 422 and res.json()["reason"] == "bad_model" and "glTF Binary" in res.json()["message"]
        assert build(client, "kai", model, plot="1").json()["message"] == "plot 1 already has a house"
        assert build(client, "kai", model, plot="9").status_code == 422
        assert build(client, "nobody", model).status_code == 404
        assert build(client, "kai", b"").status_code == 400
        assert client.post(f"{ADMIN}/houses/kai", content=model).status_code == 401
        assert client.get(ADMIN, headers=AUTH).json()["counts"]["rooms"] == 12
    assert (town_dir / "world.yaml").read_text() == world_yaml
    assert not (town_dir / "houses" / "kai.glb").exists() and not (town_dir / "houses" / "kai.yaml").exists()


def test_houses_need_a_town(client):
    status = client.get(ADMIN, headers=AUTH).json()
    assert status["can_add_houses"] is False and status["plots"] == []
    assert client.post(f"{ADMIN}/houses/mia", content=b"glTF", headers=AUTH).status_code == 409


def test_taking_a_house_down_from_the_admin_page(town_dir):
    model = (TOWN / "houses" / "mia.glb").read_bytes()
    with town_client(town_dir) as client, \
            client.websocket_connect(f"{API_PREFIX}/ws") as mia, \
            client.websocket_connect(f"{API_PREFIX}/ws/viewer?token=open-sesame") as viewer:
        assert register(mia, "tok-mia")["snapshot"]["room"]["id"] == "mia.garden"
        assert viewer.receive_json()["type"] == "world_state"
        for step in ("house", "kitchen"):
            mia.send_json({"type": "move", "id": step, "to": step})
            until(mia, "ack")
        mia.send_json({"type": "pick_up", "id": "p", "object": "mug"})
        assert until(mia, "ack")["result"]["object"]["id"] == "mia.mug"
        world = client.app.state.hub.world
        world.objects["cafe_mug"].loc_id = "mia.kitchen"  # someone left the café's mug at Mia's

        res = client.delete(f"{ADMIN}/houses/mia", headers=AUTH)
        assert res.status_code == 200, res.text
        assert sorted(res.json()["removed"]["files"])[0].startswith("houses/removed/")
        status = res.json()["status"]
        assert {p["id"]: p["house"] for p in status["plots"]}["1"] is None
        assert status["counts"]["rooms"] == 8 and not any(r["area"] == "mia" for r in status["rooms"])

        left = until(mia, "left_room")
        assert left["to"] == "street_west"
        changed = until(mia, "world_changed")
        validate(changed)
        assert changed["message"] == "Mia's house has been taken down."
        assert changed["snapshot"]["room"]["id"] == "street_west"
        assert [o["id"] for o in changed["snapshot"]["you"]["holding"]] == ["mia.mug"]  # still in her hand
        assert "Mia's house" not in {x["direction"] for x in changed["snapshot"]["room"]["exits"]}
        state = until(viewer, "world_state")
        validate(state)
        assert "mia" not in {a["id"] for a in state["areas"]}
        world = client.app.state.hub.world
        assert not [o for o in world.objects if o.startswith("mia.") and o != "mia.mug"]
        assert world.objects["cafe_mug"].loc_id == "street_west"
        mia.send_json({"type": "map", "id": "m", "to": "home"})
        assert until(mia, "action_failed")["reason"] == "no_such_place"  # no home any more

        assert client.delete(f"{ADMIN}/houses/mia", headers=AUTH).status_code == 404
        assert client.delete(f"{ADMIN}/houses/square", headers=AUTH).status_code == 404
        assert client.delete(f"{ADMIN}/houses/otto").status_code == 401
        # The plot is free again, so the house can come back.
        assert build(client, "mia", model).json()["house"]["plot"] == "1"
    assert load_world(town_dir).homes["mia"] == "mia.garden"  # rebuilt from the dropped model
    assert len(list((town_dir / "houses" / "removed").iterdir())) == 2


# ------------------------------------------------------------- avatars by the host

def test_host_sets_an_avatar(config, agents, tmp_path):
    from avatar_files import model, png

    config.settings.avatars = {"max_model_mb": 0.0001}  # ~100 bytes: every model is too big
    agents = dataclasses.replace(agents, admin_password="host-secret")
    app = create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path)
    with TestClient(app) as c:
        url = f"{ADMIN}/agents/mia/avatar"
        assert c.put(url, content=png()).status_code == 401
        assert c.put(url, content=model(), headers=AUTH).json()["reason"] == "too_large"
        res = c.put(f"{url}?ignore_limits=true", content=model(), headers=AUTH)
        assert res.status_code == 200, res.text
        assert res.json()["avatar"]["kind"] == "vrm"
        assert next(a for a in res.json()["status"]["agents"] if a["id"] == "mia")["avatar"]["kind"] == "vrm"
        bad = c.put(f"{url}?ignore_limits=true", content=b"not a model", headers=AUTH)
        assert bad.json()["reason"] == "unsupported_type"  # the safety checks still apply
        assert c.put(f"{ADMIN}/agents/nobody/avatar", content=png(), headers=AUTH).status_code == 404


def test_avatar_command(config, agents, tmp_path, capsys):
    import socket
    import threading
    import time

    import uvicorn

    from avatar_files import model
    from commonroom_server.cli import main as cli

    agents = dataclasses.replace(agents, admin_password="host-secret")
    app = create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path / "avatars")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        world = tmp_path / "world"
        world.mkdir()
        (world / "agents.yaml").write_text("admin_password: host-secret\nagents:\n  - {id: mia, name: Mia, token: tok-mia}\n")
        (tmp_path / "mia.vrm").write_bytes(model())
        url = f"http://127.0.0.1:{port}"
        cli(["avatar", "mia", str(tmp_path / "mia.vrm"), "--world", str(world), "--url", url])
        assert "mia now has a vrm avatar (Test Avatar)" in capsys.readouterr().out
        assert app.state.hub.world.entities["mia"].avatar["kind"] == "vrm"
        cli(["avatar", "mia", "--remove", "--world", str(world), "--url", url.replace("http", "ws")])
        assert "removed mia's avatar" in capsys.readouterr().out
        with pytest.raises(SystemExit, match="404"):
            cli(["avatar", "nobody", str(tmp_path / "mia.vrm"), "--world", str(world), "--url", url])
        (world / "agents.yaml").write_text("agents:\n  - {id: mia, name: Mia, token: tok-mia}\n")
        with pytest.raises(SystemExit, match="set-admin-password"):
            cli(["avatar", "mia", str(tmp_path / "mia.vrm"), "--world", str(world), "--url", url])
    finally:
        server.should_exit = True
        thread.join(5)

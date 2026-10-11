import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from commonroom_protocol import API_PREFIX, PROTOCOL_VERSION
from commonroom_server.app import create_app
from commonroom_server.store import Store
from commonroom_server.world import World

WS = f"{API_PREFIX}/ws"


@pytest.fixture
def client(config, agents):
    app = create_app(config, agents, viewer_dir=None, clock_interval=0)
    with TestClient(app) as c:
        yield c


def register(ws, token, protocol=PROTOCOL_VERSION, **extra):
    ws.send_json({"type": "register", "id": "r", "protocol": protocol, "token": token, **extra})
    return ws.receive_json()


def next_of(ws, type_):
    while True:
        msg = ws.receive_json()
        if msg["type"] == type_:
            return msg


def test_version_endpoint(client):
    info = client.get("/version").json()
    assert info["protocol_version"] == PROTOCOL_VERSION
    assert PROTOCOL_VERSION in info["supported_protocol_versions"]
    assert info["endpoints"]["agent"] == WS


def test_register_negotiates_protocol(client):
    with client.websocket_connect(WS) as ws:
        welcome = register(ws, "tok-mia", protocol=["9.0", "1.4"])
        assert welcome["type"] == "welcome"
        assert welcome["protocol"] == PROTOCOL_VERSION
        assert welcome["entity_id"] == "mia"
        assert welcome["snapshot"]["room"]["id"] == "hall"


def test_register_rejects_unknown_major(client):
    with client.websocket_connect(WS) as ws:
        err = register(ws, "tok-mia", protocol="2.0")
        assert err["type"] == "error" and err["reason"] == "protocol_mismatch"
        assert err["supported_protocol_versions"] == [PROTOCOL_VERSION]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_register_rejects_bad_token(client):
    with client.websocket_connect(WS) as ws:
        err = register(ws, "nope")
        assert err["reason"] == "unauthorized"


def test_unversioned_paths_do_not_exist(client):
    assert client.get("/api/state").status_code == 404


def test_intents_ack_and_events(client):
    with client.websocket_connect(WS) as mia, client.websocket_connect(WS) as otto:
        register(mia, "tok-mia")
        register(otto, "tok-otto")
        next_of(mia, "presence")  # otto woke up

        mia.send_json({"type": "say", "id": "s1", "text": "hello"})
        ack = next_of(mia, "ack")
        assert ack["id"] == "s1" and ack["result"]["seq"] == 1
        heard = next_of(otto, "speech")
        assert heard["text"] == "hello" and heard["speaker"] == "mia"

        otto.send_json({"type": "pick_up", "id": "p1", "object": "statue"})
        failed = next_of(otto, "action_failed")
        assert failed["id"] == "p1" and failed["reason"] == "not_portable"

        otto.send_json(["not", "an", "object"])
        assert next_of(otto, "action_failed")["reason"] == "bad_request"


def test_viewer_gets_state_and_updates(client):
    with client.websocket_connect(f"{API_PREFIX}/ws/viewer") as viewer:
        state = viewer.receive_json()
        assert state["type"] == "world_state" and state["protocol"] == PROTOCOL_VERSION
        assert {r["id"] for r in state["rooms"]} == {"hall", "kitchen", "study"}
        with client.websocket_connect(WS) as mia:
            register(mia, "tok-mia")
            update = next_of(viewer, "update")
            assert any(e["id"] == "mia" and e["online"] for e in update["entities"])
            mia.send_json({"type": "move", "id": "m", "to": "north"})
            while True:
                update = next_of(viewer, "update")
                if any(e["type"] == "entered_room" for e in update["events"]):
                    break
            assert update["entities"][0]["room"] == "kitchen"


def test_persistence_across_restart(tmp_path, config, agents):
    db = tmp_path / "world.db"
    w = World(config, Store(db))
    w.connect(agents.agents["mia"])
    w.handle("mia", {"type": "interact", "object": "box", "verb": "open"})
    w.handle("mia", {"type": "pick_up", "object": "key"})
    w.handle("mia", {"type": "move", "to": "north"})
    w.handle("mia", {"type": "say", "text": "remember me"})
    w.commit()
    w.store.close()

    w2 = World(config, Store(db))
    assert w2.entities["mia"].room == "kitchen"
    assert w2.entities["mia"].online is False
    assert (w2.objects["key"].loc_kind, w2.objects["key"].loc_id) == ("entity", "mia")
    assert w2.objects["box"].state["open"] is True
    assert w2.speech_seq["kitchen"] == 1
    assert any(e.payload.get("text") == "remember me" for e in w2.recent["kitchen"])
    assert w2.next_event_id > max(e.id for e in w2.recent["kitchen"])
    assert len(w2.store.events()) >= 5


# ------------------------------------------------------------ viewer password

@pytest.fixture
def locked_client(config, agents):
    agents.viewer_password = "open-sesame"
    app = create_app(config, agents, viewer_dir=None, clock_interval=0)
    with TestClient(app) as c:
        yield c


def test_viewer_password_by_message(locked_client):
    with locked_client.websocket_connect(f"{API_PREFIX}/ws/viewer") as viewer:
        assert viewer.receive_json() == {"type": "auth_required", "protocol": PROTOCOL_VERSION}
        viewer.send_json({"type": "viewer_auth", "password": "open-sesame"})
        assert viewer.receive_json()["type"] == "world_state"


def test_viewer_wrong_password(locked_client):
    with locked_client.websocket_connect(f"{API_PREFIX}/ws/viewer") as viewer:
        viewer.receive_json()
        viewer.send_json({"type": "viewer_auth", "password": "guess"})
        err = viewer.receive_json()
        assert err["type"] == "error" and err["reason"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as exc:
            viewer.receive_json()
        assert exc.value.code == 4401


def test_viewer_password_in_url_still_works(locked_client):
    with locked_client.websocket_connect(f"{API_PREFIX}/ws/viewer?token=open-sesame") as viewer:
        assert viewer.receive_json()["type"] == "world_state"


def test_public_viewer_needs_no_password(client):
    with client.websocket_connect(f"{API_PREFIX}/ws/viewer") as viewer:
        assert viewer.receive_json()["type"] == "world_state"


def test_http_api_needs_password(locked_client):
    assert locked_client.get(f"{API_PREFIX}/api/state").status_code == 401
    assert locked_client.get(f"{API_PREFIX}/api/state",
                             headers={"Authorization": "Bearer wrong-one"}).status_code == 401
    assert locked_client.get(f"{API_PREFIX}/api/state",
                             headers={"Authorization": "Bearer open-sesame"}).status_code == 200
    assert locked_client.get(f"{API_PREFIX}/api/events?token=open-sesame").status_code == 200
    assert locked_client.get("/version").status_code == 200


def test_viewer_password_config(tmp_path):
    from commonroom_server.config import ConfigError, load_agents
    path = tmp_path / "agents.yaml"
    path.write_text("viewer_token: legacy-name-ok\nagents: []\n")
    assert load_agents(path).viewer_password == "legacy-name-ok"
    path.write_text("viewer_password: short\nagents: []\n")
    with pytest.raises(ConfigError, match="at least 8"):
        load_agents(path)
    path.write_text("viewer_password: same-as-token\nagents: [{id: a, token: same-as-token}]\n")
    with pytest.raises(ConfigError, match="same as an agent"):
        load_agents(path)

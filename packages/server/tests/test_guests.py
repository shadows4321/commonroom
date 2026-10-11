"""Guests: people who join from /play with a name and the guest password (protocol 1.7)."""

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from commonroom_protocol import API_PREFIX, PROTOCOL_VERSION
from commonroom_server.app import DEFAULT_VIEWER_DIR, create_app
from commonroom_server.cli import main as cli
from commonroom_server.config import AgentSpec, ConfigError, load_agents
from commonroom_server.store import Store
from commonroom_server.world import World
from test_schema import validate

WS = f"{API_PREFIX}/ws"


@pytest.fixture
def guests(agents):
    return dataclasses.replace(agents, guest_password="come-in-123")


@pytest.fixture
def client(config, guests, tmp_path):
    app = create_app(config, guests, viewer_dir=DEFAULT_VIEWER_DIR, clock_interval=0, avatars_dir=tmp_path,
                     guest_grace=0.3)
    with TestClient(app) as c:
        yield c


def join(ws, name, password="come-in-123"):
    hello = {"type": "register", "id": "r", "protocol": PROTOCOL_VERSION, "guest_password": password, "name": name}
    validate(hello, "Intent")
    ws.send_json(hello)
    return ws.receive_json()


def register(ws, token):
    ws.send_json({"type": "register", "id": "r", "protocol": PROTOCOL_VERSION, "token": token})
    return ws.receive_json()


def until(ws, kind):
    while (msg := ws.receive_json())["type"] != kind:
        pass
    return msg


def test_a_guest_joins_talks_and_goes_home(client):
    with client.websocket_connect(WS) as otto, client.websocket_connect(f"{WS}/viewer") as viewer:
        assert register(otto, "tok-otto")["type"] == "welcome"
        assert viewer.receive_json()["type"] == "world_state"
        with client.websocket_connect(WS) as bob:
            welcome = join(bob, "  Bob  ")
            validate(welcome)
            assert welcome["entity_id"] == "guest-bob" and welcome["snapshot"]["you"]["kind"] == "guest"
            assert welcome["snapshot"]["you"]["name"] == "Bob"
            assert welcome["snapshot"]["room"]["id"] == "hall"  # the start room: guests have no home
            while (arrived := until(otto, "entered_room"))["entity"] != "guest-bob":
                pass  # otto's own arrival comes first
            assert arrived["entity_view"]["kind"] == "guest"
            bob.send_json({"type": "say", "id": "s", "text": "hi, I'm a person"})
            heard = until(otto, "speech")
            assert heard["speaker"] == "guest-bob" and heard["text"] == "hi, I'm a person"
            otto.send_json({"type": "look", "id": "l"})
            occupants = until(otto, "ack")["result"]["snapshot"]["occupants"]
            assert {o["id"]: o["kind"] for o in occupants} == {"guest-bob": "guest"}
            status = client.get(f"{API_PREFIX}/admin", headers={"Authorization": "Bearer x"})
            assert status.status_code == 404  # no admin password here; just making sure nothing else broke
        # Bob closed the page: after the grace period he goes home, for everyone.
        gone = until(otto, "left_room")
        validate(gone)
        assert gone["entity"] == "guest-bob" and gone["gone"] is True and gone["to"] is None
        while True:
            update = viewer.receive_json()
            validate(update)
            if update.get("gone"):
                break
        assert update["gone"] == ["guest-bob"]
        assert "guest-bob" not in client.app.state.hub.world.entities


def test_coming_back_in_time_keeps_your_place(client):
    with client.websocket_connect(WS) as bob:
        join(bob, "Bob")
        bob.send_json({"type": "move", "id": "m", "to": "north"})
        assert until(bob, "ack")["result"]["room"] == "kitchen"
    with client.websocket_connect(WS) as bob:
        assert join(bob, "bob")["snapshot"]["room"]["id"] == "kitchen"  # same name, any case
    time.sleep(0.6)
    with client.websocket_connect(WS) as bob:
        assert join(bob, "Bob")["snapshot"]["room"]["id"] == "hall"  # too late: a fresh start


@pytest.mark.parametrize("name, password, reason", [
    ("Bob", "wrong", "unauthorized"),
    ("", "come-in-123", "bad_name"),
    ("   ", "come-in-123", "bad_name"),
    ("x" * 25, "come-in-123", "bad_name"),
    ("<script>", "come-in-123", "bad_name"),
    ("Mia", "come-in-123", "name_taken"),  # an AI's name
    ("otto", "come-in-123", "name_taken"),  # an AI's id
])
def test_who_can_join(client, name, password, reason):
    with client.websocket_connect(WS) as ws:
        reply = join(ws, name, password)
        validate(reply)
        assert reply["type"] == "error" and reply["reason"] == reason, reply


def test_two_people_cant_share_a_name(client):
    with client.websocket_connect(WS) as first, client.websocket_connect(WS) as second:
        assert join(first, "Zoë")["entity_id"] == "guest-zo"
        reply = join(second, "zoë")
        assert reply["reason"] == "name_taken"
        assert join(first, "Zoë")  # the first one is still fine
    with client.websocket_connect(WS) as ws:
        assert join(ws, "花子")["entity_id"].startswith("guest-")  # names without Latin letters work too


def test_no_guest_password_no_guests(config, agents, tmp_path):
    app = create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path)
    with TestClient(app) as c, c.websocket_connect(WS) as ws:
        reply = join(ws, "Bob", "anything")
        assert reply["reason"] == "unauthorized" and "doesn't take guests" in reply["message"]


def test_guests_dont_survive_a_restart(config, agents, guests, tmp_path):
    db = tmp_path / "w.db"
    w = World(config, Store(db))
    w.connect(AgentSpec("guest-bob", "Bob", "", kind="guest"))
    w.connect(agents.agents["mia"])
    w.objects["key"].loc_kind, w.objects["key"].loc_id = "entity", "guest-bob"
    w._touch(w.objects["key"])
    w.commit()
    w.store.close()
    app = create_app(config, guests, db_path=str(db), viewer_dir=None, clock_interval=0, avatars_dir=tmp_path)
    world = app.state.hub.world
    assert "guest-bob" not in world.entities and "mia" in world.entities
    assert world.objects["key"].loc_kind == "room"  # what Bob carried stayed behind
    assert world.store.db.execute("SELECT COUNT(*) FROM entities WHERE id = 'guest-bob'").fetchone()[0] == 0
    world.store.close()


def test_guest_password_in_agents_yaml(tmp_path, capsys):
    path = tmp_path / "agents.yaml"
    path.write_text("viewer_password: watch-me-123\nagents:\n  - {id: mia, name: Mia, token: tok-mia}\n")
    cli(["set-guest-password", "come-in-123", "--world", str(tmp_path)])
    assert "/play" in capsys.readouterr().out
    assert load_agents(path).guest_password == "come-in-123"
    with pytest.raises(SystemExit):
        cli(["set-guest-password", "tok-mia", "--world", str(tmp_path)])
    path.write_text("agents:\n  - {id: guest-x, name: X, token: abcdefgh}\n")
    with pytest.raises(ConfigError, match="guest-"):
        load_agents(path)


def test_play_page_is_served(client):
    page = client.get("/play")
    assert page.status_code == 200 and "Join in" in page.text
    assert client.get("/viewer/play.js").status_code == 200

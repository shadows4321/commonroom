"""Every message the server actually sends must match the published v1 JSON schema."""

import contextlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from commonroom_protocol import API_PREFIX, PROTOCOL_MAJOR, PROTOCOL_VERSION
from commonroom_server.app import create_app

from avatar_files import model

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "protocol" / "schemas" / f"v{PROTOCOL_MAJOR}" / "protocol.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())
Draft202012Validator.check_schema(SCHEMA)

BY_TYPE = {"welcome": "Welcome", "ack": "Ack", "action_failed": "ActionFailed", "error": "Error",
           "auth_required": "AuthRequired",
           "world_state": "WorldState", "update": "Update"}


def validate(message, definition=None):
    definition = definition or BY_TYPE.get(message["type"], "Event")
    validator = Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{definition}"})
    errors = sorted(validator.iter_errors(message), key=lambda e: list(e.path))
    assert not errors, f"{definition}: {errors[0].message} at {list(errors[0].path)}\n{message}"


def test_schema_declares_current_version():
    assert SCHEMA["$id"].endswith(f"/v{PROTOCOL_MAJOR}")
    assert SCHEMA["$comment"] == f"protocol_version {PROTOCOL_VERSION}"


SCRIPT = [
    ("mia", {"type": "say", "text": "hello"}),
    ("otto", {"type": "say", "text": "late reply", "after_seq": 0}),
    ("otto", {"type": "whisper", "to": "Mia", "text": "psst"}),
    ("mia", {"type": "shout", "text": "HEY"}),
    ("mia", {"type": "typing", "active": True}),
    ("mia", {"type": "interact", "object": "box", "verb": "open"}),
    ("mia", {"type": "pick_up", "object": "key"}),
    ("mia", {"type": "give", "object": "key", "to": "otto"}),
    ("otto", {"type": "put_down", "object": "key", "into": "box"}),
    ("otto", {"type": "pick_up", "object": "statue"}),
    ("mia", {"type": "move", "near": "statue"}),
    ("mia", {"type": "look", "at": "box"}),
    ("mia", {"type": "look"}),
    ("mia", {"type": "move", "to": "east"}),
    ("mia", {"type": "interact", "object": "lamp", "verb": "switch_on"}),
    ("mia", {"type": "interact", "object": "bench", "verb": "sit"}),
    ("mia", {"type": "interact", "object": "bench", "verb": "stand"}),
    ("mia", {"type": "interact", "object": "stool", "verb": "sit"}),
    ("otto", {"type": "move", "to": "east"}),
    ("otto", {"type": "pick_up", "object": "stool"}),
    ("mia", {"type": "move", "near": "bench"}),
]


def test_live_messages_match_schema(config, agents, tmp_path):
    app = create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path)
    seen = {}
    with TestClient(app) as client, client.websocket_connect(f"{API_PREFIX}/ws/viewer") as viewer, \
            contextlib.ExitStack() as stack:
        socks = {}
        for name, token in (("mia", "tok-mia"), ("otto", "tok-otto")):
            ws = stack.enter_context(client.websocket_connect(f"{API_PREFIX}/ws"))
            hello = {"type": "register", "id": "r", "protocol": PROTOCOL_VERSION, "token": token}
            validate(hello, "Intent")
            ws.send_json(hello)
            socks[name] = ws
        for who, intent in SCRIPT:
            intent = {**intent, "id": f"{who}-{intent['type']}"}
            validate(intent, "Intent")
            socks[who].send_json(intent)
            while True:  # read until this intent's reply; validate everything on the way
                msg = socks[who].receive_json()
                validate(msg)
                seen.setdefault(msg["type"], msg)
                if msg.get("id") == intent["id"]:
                    break
        res = client.put(f"{API_PREFIX}/avatar", content=model(), headers={"Authorization": "Bearer tok-mia"})
        assert res.status_code == 200
        while (msg := socks["mia"].receive_json())["type"] != "profile_changed":
            validate(msg)
        validate(msg)
        seen.setdefault("profile_changed", msg)
        hub = app.state.hub
        hub.world.advance_clock(20)
        hub.dispatch(hub.world.commit())
        stack.close()
        msg = viewer.receive_json()
        validate(msg)
        assert msg["type"] == "world_state"
        for _ in range(30):
            msg = viewer.receive_json()
            validate(msg)
            for ev in msg["events"]:
                seen.setdefault(ev["type"], ev)
            if any(e["type"] == "time_tick" for e in msg["events"]):
                break
    expected = {"welcome", "ack", "action_failed", "speech", "presence", "object_changed",
                "position_changed", "left_room", "time_tick", "profile_changed"}
    assert expected <= set(seen), expected - set(seen)


@pytest.mark.parametrize("bad", [
    {"type": "say"},
    {"type": "move"},
    {"type": "register", "protocol": "one", "token": "x"},
    {"type": "give", "object": "key"},
])
def test_schema_rejects_malformed_intents(bad):
    with pytest.raises(AssertionError):
        validate(bad, "Intent")

"""Replay (protocol 1.8): the recorded past, rebuilt for viewers to play back."""

import time

import pytest
from fastapi.testclient import TestClient

from commonroom_protocol import API_PREFIX
from commonroom_server.app import create_app
from commonroom_server.store import Store
from commonroom_server.world import KEYFRAME_SECONDS, World


@pytest.fixture
def now(monkeypatch):
    """A settable time.time(), so changes land at known moments."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    return clock


def where(state, eid):
    return next(e for e in state["entities"] if e["id"] == eid)["room"]


def test_state_at_rebuilds_the_past(tmp_path, config, agents, now):
    store = Store(tmp_path / "world.db")
    w = World(config, store)
    w.connect(agents.agents["mia"])
    w.commit()
    now["t"] = 1010
    w.handle("mia", {"type": "move", "to": "north"})
    w.commit()
    now["t"] = 1020
    w.handle("mia", {"type": "say", "text": "nice kitchen"})
    w.commit()
    now["t"] = 1030
    w.handle("mia", {"type": "move", "to": "south"})
    w.commit()

    assert store.history_span() == (1000, 1030)
    assert where(store.state_at(1005), "mia") == "hall"
    assert where(store.state_at(1015), "mia") == "kitchen"
    assert where(store.state_at(1030), "mia") == "hall"
    early = store.state_at(10)  # before anything was recorded: the first moment there is
    assert early["at"] == 1000 and where(early, "mia") == "hall"

    frames, more = store.frames_after(store.state_at(1015)["history_id"], 10)
    assert not more and [f["t"] for f in frames] == [1020, 1030]
    assert frames[0]["events"][0].payload["text"] == "nice kitchen"
    assert frames[1]["entities"][0]["room"] == "hall"
    assert [ev.payload.get("text") for ev in store.events_before(1025, 5) if ev.type == "speech"] == ["nice kitchen"]


def test_keyframes_now_and_then_and_when_the_layout_changes(tmp_path, config, agents, now):
    store = Store(tmp_path / "world.db")
    w = World(config, store)
    w.connect(agents.agents["mia"])
    w.commit()
    now["t"] += KEYFRAME_SECONDS + 1
    w.handle("mia", {"type": "move", "to": "east"})
    w.commit()
    w2 = World(config, store, carry_over=w)  # what Hub.reload does
    w2.world_changed("A house was built.")
    w2.commit()
    reasons = [r[0] for r in store.db.execute("SELECT reason FROM keyframes ORDER BY id")]
    assert reasons == ["start", "periodic", "reload"]
    assert where(store.state_at(now["t"]), "mia") == "study"
    frames, _ = store.frames_after(0, 50)
    assert [f["reason"] for f in frames if "state" in f] == ["reload"]  # periodic ones aren't news to a player


def test_quiet_stretches_get_no_keyframes(tmp_path, config, agents, now):
    store = Store(tmp_path / "world.db")
    w = World(config, store)
    w.connect(agents.agents["mia"])
    w.commit()
    reasons = lambda: [r[0] for r in store.db.execute("SELECT reason FROM keyframes ORDER BY id")]
    for _ in range(3):  # 45 minutes of only the clock moving
        now["t"] += KEYFRAME_SECONDS
        w.advance_clock(15)
        w.commit()
    assert reasons() == ["start", "periodic"]  # the first one still covers Mia arriving
    now["t"] += 10
    w.handle("mia", {"type": "move", "to": "north"})
    w.commit()  # something happened, and it's been long enough
    assert reasons() == ["start", "periodic", "periodic"]
    assert where(store.state_at(now["t"] - 20), "mia") == "hall"
    assert where(store.state_at(now["t"]), "mia") == "kitchen"


def test_typing_alone_is_not_recorded(tmp_path, config, agents, now):
    store = Store(tmp_path / "world.db")
    w = World(config, store)
    w.connect(agents.agents["mia"])
    w.commit()
    before = store.db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    w.handle("mia", {"type": "typing", "active": True})
    w.commit()
    assert store.db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == before


def test_activity_counts_changes_not_the_clock(tmp_path, config, agents, now):
    store = Store(tmp_path / "world.db")
    w = World(config, store)
    w.connect(agents.agents["mia"])
    w.commit()
    now["t"] = 1090
    w.advance_clock(60)  # a time_tick only
    w.commit()
    now["t"] = 1095
    w.handle("mia", {"type": "say", "text": "hi"})
    w.commit()
    assert store.activity(1000, 1100, 10) == [2, 0, 0, 0, 0, 0, 0, 0, 0, 1]  # seeding, mia arriving; "hi"


def test_history_endpoints(tmp_path, config, agents):
    config.settings.viewer_sees_whispers = False
    app = create_app(config, agents, db_path=str(tmp_path / "w.db"), viewer_dir=None, clock_interval=0)
    with TestClient(app) as client:
        with client.websocket_connect(f"{API_PREFIX}/ws") as mia:
            mia.send_json({"type": "register", "id": "r", "protocol": "1", "token": "tok-mia"})
            mia.receive_json()
            with client.websocket_connect(f"{API_PREFIX}/ws") as otto:
                otto.send_json({"type": "register", "id": "r", "protocol": "1", "token": "tok-otto"})
                otto.receive_json()
                start = time.time()
                mia.send_json({"type": "whisper", "id": "w", "to": "otto", "text": "secret"})
                while mia.receive_json()["type"] != "ack":
                    pass
                mia.send_json({"type": "move", "id": "m", "to": "north"})
                while mia.receive_json()["type"] != "ack":
                    pass

        info = client.get(f"{API_PREFIX}/api/history?buckets=4").json()
        assert info["available"] and info["start"] <= start and len(info["buckets"]) == 4 and sum(info["buckets"]) >= 3

        past = client.get(f"{API_PREFIX}/api/history/state", params={"at": info["start"]}).json()
        assert past["type"] == "world_state" and past["protocol"] and past["clock"]["label"]
        assert "clock_minutes" not in past and {r["id"] for r in past["rooms"]} == {"hall", "kitchen", "study"}

        frames = client.get(f"{API_PREFIX}/api/history/frames", params={"after": past["history_id"]}).json()
        updates = [f["update"] for f in frames["frames"] if "update" in f]
        assert all(u["type"] == "update" and u["clock"] for u in updates)
        speech = [ev for u in updates for ev in u["events"] if ev["type"] == "speech"]
        assert speech[0]["redacted"] and speech[0]["text"] is None
        moved = [e for u in updates for e in u["entities"] if e["id"] == "mia" and e["room"] == "kitchen"]
        assert moved and not frames["more"]


def test_history_needs_a_database_and_the_password(tmp_path, config, agents):
    with TestClient(create_app(config, agents, viewer_dir=None, clock_interval=0)) as client:
        assert client.get(f"{API_PREFIX}/api/history").json()["available"] is False
        assert client.get(f"{API_PREFIX}/api/history/state?at=0").status_code == 404
    agents.viewer_password = "open-sesame"
    app = create_app(config, agents, db_path=str(tmp_path / "w.db"), viewer_dir=None, clock_interval=0)
    with TestClient(app) as client:
        assert client.get(f"{API_PREFIX}/api/history/frames").status_code == 401
        ok = client.get(f"{API_PREFIX}/api/history", headers={"Authorization": "Bearer open-sesame"})
        assert ok.status_code == 200

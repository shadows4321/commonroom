import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from avatar_files import glb, model, png
from commonroom_protocol import API_PREFIX, PROTOCOL_VERSION
from commonroom_server import avatars
from commonroom_server.app import create_app
from commonroom_server.avatars import AvatarError, AvatarLimits
from commonroom_server.store import SCHEMA_VERSION, Store
from commonroom_server.world import World

WS = f"{API_PREFIX}/ws"


# ------------------------------------------------------------- file checks

def test_png_avatar():
    info = avatars.inspect(png(64, 32))
    assert info.kind == "png" and info.meta == {"width": 64, "height": 32}
    assert info.view()["thumbnail_url"] == info.view()["url"]


@pytest.mark.parametrize("version", ["1.0", "0.x"])
def test_vrm_avatar_meta_and_thumbnail(version):
    thumb = png(16, 16)
    info = avatars.inspect(model(texture=thumb, vrm=version))
    assert info.kind == "vrm"
    assert info.meta["vrm_version"] == version and info.meta["title"] == "Test Avatar"
    assert info.meta["authors"] == ["Someone"]
    assert info.thumbnail == thumb and info.thumbnail_ext == "png"
    assert info.view()["thumbnail_url"].endswith(".thumb.png")


def test_vrm_without_thumbnail_and_plain_glb():
    assert avatars.inspect(model(thumbnail=False)).view()["thumbnail_url"] is None
    info = avatars.inspect(model(vrm=None))
    assert info.kind == "glb" and info.thumbnail is None and info.meta == {"vertices": 3}


@pytest.mark.parametrize("data, reason", [
    (b"GIF89a....", "unsupported_type"),
    (b"glTF" + b"\x02\x00\x00\x00" + b"\xff\x00\x00\x00" + b"\x00" * 12, "invalid_avatar"),  # length mismatch
    (glb({"asset": {}, "buffers": [{"uri": "https://example.com/x.bin", "byteLength": 4}]}), "not_self_contained"),
    (glb({"asset": {}, "images": [{"uri": "https://example.com/tracker.png"}]}), "not_self_contained"),
    (model(vertices=10_000_000), "too_detailed"),
    (model(texture=png(5000, 10)), "texture_too_large"),
    (png(3000, 10), "texture_too_large"),
])
def test_rejected_files(data, reason):
    with pytest.raises(AvatarError) as exc:
        avatars.inspect(data)
    assert exc.value.reason == reason


def test_size_limits():
    limits = AvatarLimits(max_model_bytes=100, max_png_bytes=50)
    for data in (model(), png(16, 16)):
        with pytest.raises(AvatarError) as exc:
            avatars.inspect(data, limits)
        assert exc.value.reason == "too_large"


def test_limits_from_world_settings():
    limits = AvatarLimits.from_settings({"max_model_mb": 2, "max_vertices": 10})
    assert limits.max_model_bytes == 2 * 1024 * 1024 and limits.max_vertices == 10


# ----------------------------------------------------------- upload endpoint

@pytest.fixture
def app(config, agents, tmp_path):
    return create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path / "avatars")


def put(client, data, token="tok-mia"):
    return client.put(f"{API_PREFIX}/avatar", content=data, headers={"Authorization": f"Bearer {token}"})


def test_upload_requires_agent_token(app):
    with TestClient(app) as client:
        assert put(client, png(), token="nope").status_code == 401
        assert client.put(f"{API_PREFIX}/avatar", content=png()).status_code == 401


def test_upload_serve_replace_delete(app, tmp_path):
    with TestClient(app) as client:
        res = put(client, model())
        assert res.status_code == 200, res.text
        avatar = res.json()["avatar"]
        assert avatar["kind"] == "vrm" and avatar["meta"]["title"] == "Test Avatar"

        model_file = client.get(avatar["url"])
        assert model_file.status_code == 200 and model_file.content == model()
        assert "immutable" in model_file.headers["cache-control"]
        assert client.get(avatar["thumbnail_url"]).headers["content-type"] == "image/png"
        assert client.get(f"{API_PREFIX}/avatars/../../etc/passwd").status_code == 404
        assert client.get(f"{API_PREFIX}/avatars/{'0' * 64}.vrm").status_code == 404

        # Mia exists in the world (asleep) even though her AI never connected.
        mia = next(e for e in client.get(f"{API_PREFIX}/api/state").json()["entities"] if e["id"] == "mia")
        assert mia["avatar"]["sha256"] == avatar["sha256"] and mia["online"] is False

        new = put(client, png()).json()["avatar"]
        assert new["kind"] == "png"
        assert client.get(avatar["url"]).status_code == 404, "the replaced file should be cleaned up"
        assert client.get(new["url"]).status_code == 200

        assert client.delete(f"{API_PREFIX}/avatar", headers={"Authorization": "Bearer tok-mia"}).json() == {"avatar": None}
        assert client.get(new["url"]).status_code == 404


def test_shared_avatar_file_kept_while_in_use(app):
    with TestClient(app) as client:
        a = put(client, png(), "tok-mia").json()["avatar"]
        put(client, png(), "tok-otto")
        put(client, png(8, 8), "tok-mia")  # mia moves on; otto still uses the first file
        assert client.get(a["url"]).status_code == 200


def test_upload_errors_are_explained(app):
    with TestClient(app) as client:
        res = put(client, b"not an avatar")
        assert res.status_code == 415 and res.json()["reason"] == "unsupported_type"
        res = put(client, glb({"asset": {}, "images": [{"uri": "https://example.com/t.png"}]}))
        assert res.status_code == 422 and res.json()["reason"] == "not_self_contained"


def test_upload_size_cap(config, agents, tmp_path):
    config.settings.avatars = {"max_model_mb": 0.0001, "max_png_mb": 0.0001}  # ~100 bytes
    app = create_app(config, agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path)
    with TestClient(app) as client:
        res = put(client, model())
        assert res.status_code == 413 and res.json()["reason"] == "too_large"


def test_avatar_change_is_broadcast(app):
    with TestClient(app) as client, client.websocket_connect(WS) as otto, \
            client.websocket_connect(f"{API_PREFIX}/ws/viewer") as viewer:
        assert viewer.receive_json()["type"] == "world_state"
        otto.send_json({"type": "register", "id": "r", "protocol": PROTOCOL_VERSION, "token": "tok-otto"})
        assert otto.receive_json()["type"] == "welcome"
        put(client, png())  # mia is created asleep in otto's room, then changes her avatar
        while (msg := otto.receive_json())["type"] != "profile_changed":
            pass
        assert msg["entity"] == "mia" and msg["entity_view"]["avatar"]["kind"] == "png"
        while True:
            update = viewer.receive_json()
            if any(e["type"] == "profile_changed" for e in update["events"]):
                break
        assert next(e for e in update["entities"] if e["id"] == "mia")["avatar"]["kind"] == "png"


# ------------------------------------------------------------- persistence

def test_avatar_survives_restart(tmp_path, config, agents):
    db = tmp_path / "w.db"
    w = World(config, Store(db))
    w.ensure_entity(agents.agents["mia"])
    w.set_avatar("mia", avatars.inspect(png()).view())
    w.commit()
    w.store.close()
    w2 = World(config, Store(db))
    assert w2.entities["mia"].avatar["kind"] == "png"


def test_v1_database_is_migrated(tmp_path, config):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                               room TEXT NOT NULL, near TEXT);
        CREATE TABLE objects (id TEXT PRIMARY KEY, type TEXT NOT NULL, loc_kind TEXT NOT NULL, loc_id TEXT NOT NULL,
                              state TEXT NOT NULL, name TEXT, description TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, type TEXT NOT NULL, room TEXT, world_time INTEGER NOT NULL,
                             real_time REAL NOT NULL, payload TEXT NOT NULL, audience TEXT, private_to TEXT);
        INSERT INTO meta VALUES ('schema_version', '1'), ('clock_minutes', '400'), ('speech_seq', '{}');
        INSERT INTO entities VALUES ('mia', 'Mia', '', 'kitchen', NULL);
    """)
    old.commit()
    old.close()
    store = Store(db)
    assert json.loads(store.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) == SCHEMA_VERSION
    w = World(config, store)
    assert w.entities["mia"].room == "kitchen" and w.entities["mia"].avatar is None and w.entities["mia"].pose is None
    assert w.clock.minutes == 400


def test_newer_database_is_refused(tmp_path):
    db = tmp_path / "new.db"
    Store(db).close()
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="upgrade the server"):
        Store(db)

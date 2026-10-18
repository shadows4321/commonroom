"""SQLite persistence: current world state plus an append-only event log."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .world import Batch, Entity, Event, Obj

SCHEMA_VERSION = 5

# Replay (protocol 1.8): `history` holds, for every commit viewers would see, the changed
# entity and object views and the world clock; `keyframes` hold the whole viewer world_state
# now and then. A past moment is the keyframe before it plus the history rows after that.
HISTORY_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    clock REAL NOT NULL,
    payload TEXT NOT NULL,
    events_from INTEGER,
    events_to INTEGER,
    active INTEGER NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS history_time ON history (real_time)",
    """CREATE TABLE IF NOT EXISTS keyframes (
    id INTEGER PRIMARY KEY,
    real_time REAL NOT NULL,
    history_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS keyframes_time ON keyframes (real_time)",
    "CREATE INDEX IF NOT EXISTS events_time ON events (real_time)",
]

# Each step upgrades a database from version N to N + 1.
MIGRATIONS = {
    1: ["ALTER TABLE entities ADD COLUMN avatar TEXT"],  # avatars (protocol 1.1)
    2: ["ALTER TABLE entities ADD COLUMN pose TEXT"],  # sitting and lying (protocol 1.3)
    3: ["ALTER TABLE entities ADD COLUMN kind TEXT NOT NULL DEFAULT 'ai'"],  # guests (protocol 1.7)
    4: HISTORY_SCHEMA,  # replay (protocol 1.8)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    room TEXT NOT NULL,
    near TEXT,
    avatar TEXT,
    pose TEXT,
    kind TEXT NOT NULL DEFAULT 'ai'
);
CREATE TABLE IF NOT EXISTS objects (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    loc_kind TEXT NOT NULL CHECK (loc_kind IN ('room', 'container', 'entity')),
    loc_id TEXT NOT NULL,
    state TEXT NOT NULL,
    name TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    room TEXT,
    world_time INTEGER NOT NULL,
    real_time REAL NOT NULL,
    payload TEXT NOT NULL,
    audience TEXT,
    private_to TEXT
);
CREATE INDEX IF NOT EXISTS events_room ON events (room, id);
"""


class Store:
    def __init__(self, path: str | Path, recent_per_room: int = 20):
        self.path = str(path)
        self.recent_per_room = recent_per_room
        # The app makes the store, then serves from its event loop, which may be another thread
        # (a test client); only that one thread ever uses it.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        has_tables = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() is not None
        version = self._meta("schema_version") if has_tables else None
        if version is None:
            self.db.executescript(SCHEMA)
            for statement in HISTORY_SCHEMA:
                self.db.execute(statement)
            self._set_meta("schema_version", SCHEMA_VERSION)
            self.db.commit()
            return
        version = int(version)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database {self.path} has schema v{version}, but this server only knows up to "
                               f"v{SCHEMA_VERSION}; upgrade the server")
        with self.db:
            while version < SCHEMA_VERSION:
                for statement in MIGRATIONS[version]:
                    self.db.execute(statement)
                version += 1
                self._set_meta("schema_version", version)

    def _meta(self, key: str):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    def load(self) -> dict | None:
        """Return the saved world, or None if this database has never been used."""
        clock = self._meta("clock_minutes")
        if clock is None:
            return None
        entities = [Entity(id=r[0], name=r[1], description=r[2], room=r[3], near=r[4],
                           avatar=json.loads(r[5]) if r[5] else None, pose=json.loads(r[6]) if r[6] else None,
                           kind=r[7] or "ai")
                    for r in self.db.execute("SELECT id, name, description, room, near, avatar, pose, kind FROM entities")]
        objects = [Obj(id=r[0], type=r[1], loc_kind=r[2], loc_id=r[3], state=json.loads(r[4]),
                       name=r[5], description=r[6])
                   for r in self.db.execute("SELECT id, type, loc_kind, loc_id, state, name, description FROM objects")]
        rows = self.db.execute(
            "SELECT id, type, room, world_time, real_time, payload, audience, private_to FROM ("
            "  SELECT *, ROW_NUMBER() OVER (PARTITION BY room ORDER BY id DESC) AS n FROM events"
            "  WHERE type NOT IN ('time_tick', 'typing')"
            ") WHERE n <= ? ORDER BY id", (self.recent_per_room,)).fetchall()
        events = [_row_to_event(r) for r in rows]
        last = self.db.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        return {
            "clock_minutes": clock,
            "speech_seq": self._meta("speech_seq") or {},
            "entities": entities,
            "objects": objects,
            "events": events,
            "next_event_id": last + 1,
        }

    def save(self, batch: Batch, clock_minutes: float, speech_seq: dict[str, int],
             frame: dict | None = None, keyframe: tuple[str, dict] | None = None) -> None:
        """`frame`: what viewers saw change (entities, objects, gone), for replay; `keyframe`:
        (reason, the whole viewer world_state) to replay from."""
        with self.db:
            for e in batch.entities.values():
                self.db.execute(
                    "INSERT INTO entities (id, name, description, room, near, avatar, pose, kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET name = excluded.name, description = excluded.description, "
                    "room = excluded.room, near = excluded.near, avatar = excluded.avatar, pose = excluded.pose, "
                    "kind = excluded.kind",
                    (e.id, e.name, e.description, e.room, e.near, json.dumps(e.avatar) if e.avatar else None,
                     json.dumps(e.pose) if e.pose else None, e.kind))
            for o in batch.objects.values():
                self.db.execute(
                    "INSERT INTO objects (id, type, loc_kind, loc_id, state, name, description) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET type = excluded.type, "
                    "loc_kind = excluded.loc_kind, loc_id = excluded.loc_id, state = excluded.state, "
                    "name = excluded.name, description = excluded.description",
                    (o.id, o.type, o.loc_kind, o.loc_id, json.dumps(o.state), o.name, o.description))
            for oid in batch.removed:
                self.db.execute("DELETE FROM objects WHERE id = ?", (oid,))
            for eid in batch.gone:
                self.db.execute("DELETE FROM entities WHERE id = ?", (eid,))
            for ev in batch.events:
                if not ev.log:
                    continue
                self.db.execute(
                    "INSERT INTO events (id, type, room, world_time, real_time, payload, audience, private_to) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ev.id, ev.type, ev.room, ev.world_time, ev.real_time, json.dumps(ev.payload),
                     json.dumps(sorted(ev.audience)) if ev.audience is not None else None,
                     json.dumps(sorted(ev.private_to)) if ev.private_to is not None else None))
            self._set_meta("clock_minutes", clock_minutes)
            self._set_meta("speech_seq", speech_seq)
            now = time.time()
            if frame is not None:
                logged = [ev.id for ev in batch.events if ev.log]
                active = bool(logged or frame["entities"] or frame["objects"] or frame["gone"])
                self.db.execute(
                    "INSERT INTO history (real_time, clock, payload, events_from, events_to, active) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now, clock_minutes, json.dumps(frame), min(logged, default=None), max(logged, default=None),
                     int(active)))
            if keyframe is not None:
                reason, state = keyframe
                last = self.db.execute("SELECT MAX(id) FROM history").fetchone()[0] or 0
                self.db.execute("INSERT INTO keyframes (real_time, history_id, reason, state) VALUES (?, ?, ?, ?)",
                                (now, last, reason, json.dumps(state)))

    def events(self, after: int = 0, limit: int = 500) -> list[Event]:
        rows = self.db.execute(
            "SELECT id, type, room, world_time, real_time, payload, audience, private_to FROM events "
            "WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return [_row_to_event(r) for r in rows]

    # ---------------------------------------------------------------- replay

    def history_span(self) -> tuple[float, float] | None:
        """(first, last) real time that can be replayed, or None if nothing was recorded yet."""
        first = self.db.execute("SELECT MIN(real_time) FROM keyframes").fetchone()[0]
        if first is None:
            return None
        last = self.db.execute("SELECT MAX(real_time) FROM history").fetchone()[0]
        return first, max(first, last or first)

    def activity(self, start: float, end: float, buckets: int) -> list[int]:
        """How many changes (not just the clock) happened in each of `buckets` equal slices."""
        counts = [0] * buckets
        width = (end - start) / buckets if end > start else 1.0
        rows = self.db.execute(
            "SELECT CAST((real_time - ?) / ? AS INTEGER), COUNT(*) FROM history "
            "WHERE active = 1 AND real_time >= ? AND real_time <= ? GROUP BY 1", (start, width, start, end))
        for bucket, n in rows:
            counts[min(max(bucket, 0), buckets - 1)] += n
        return counts

    def state_at(self, at: float) -> dict | None:
        """The viewers' world as it was at real time `at` (or at the first moment recorded, if
        `at` is earlier): the keyframe's world_state with every later change up to `at` applied.
        Adds `clock_minutes`, `history_id` (the last change included) and `at`."""
        row = self.db.execute("SELECT real_time, history_id, state FROM keyframes WHERE real_time <= ? "
                              "ORDER BY real_time DESC, id DESC LIMIT 1", (at,)).fetchone() or \
            self.db.execute("SELECT real_time, history_id, state FROM keyframes "
                            "ORDER BY real_time, id LIMIT 1").fetchone()
        if row is None:
            return None
        at = max(at, row[0])
        state = json.loads(row[2])
        entities = {e["id"]: e for e in state["entities"]}
        objects = {o["id"]: o for o in state["objects"]}
        last, clock = row[1], state.get("clock_minutes")
        for hid, minutes, payload in self.db.execute(
                "SELECT id, clock, payload FROM history WHERE id > ? AND real_time <= ? ORDER BY id", (row[1], at)):
            frame = json.loads(payload)
            entities.update((e["id"], e) for e in frame["entities"])
            objects.update((o["id"], o) for o in frame["objects"])
            for eid in frame["gone"]:
                entities.pop(eid, None)
            for oid in frame.get("removed", []):
                objects.pop(oid, None)
            last, clock = hid, minutes
        state.update(entities=list(entities.values()), objects=list(objects.values()), history_id=last, at=at)
        if clock is not None:
            state["clock_minutes"] = clock
        return state

    def events_before(self, at: float, limit: int) -> list[Event]:
        rows = self.db.execute(
            "SELECT id, type, room, world_time, real_time, payload, audience, private_to FROM events "
            "WHERE real_time <= ? AND type NOT IN ('time_tick', 'typing') ORDER BY id DESC LIMIT ?",
            (at, limit)).fetchall()
        return [_row_to_event(r) for r in reversed(rows)]

    def frames_after(self, history_id: int, limit: int) -> tuple[list[dict], bool]:
        """Recorded changes after `history_id`, oldest first: `{"id", "t", "clock_minutes",
        "entities", "objects", "gone", "events": [Event]}`, plus `{"id", "t", "reason", "state"}`
        for keyframes written when the layout changed (a house added or removed). Also returns
        whether there are more."""
        rows = self.db.execute(
            "SELECT id, real_time, clock, payload, events_from, events_to FROM history WHERE id > ? "
            "ORDER BY id LIMIT ?", (history_id, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        if not rows:
            return [], False
        firsts = [r[4] for r in rows if r[4] is not None]
        events: dict[int, Event] = {}
        if firsts:
            for r in self.db.execute(
                    "SELECT id, type, room, world_time, real_time, payload, audience, private_to FROM events "
                    "WHERE id >= ? AND id <= ? ORDER BY id", (min(firsts), max(r[5] or 0 for r in rows))):
                events[r[0]] = _row_to_event(r)
        layout = {}
        for hid, t, reason, state in self.db.execute(
                "SELECT history_id, real_time, reason, state FROM keyframes WHERE reason = 'reload' "
                "AND history_id >= ? AND history_id <= ? ORDER BY id", (rows[0][0], rows[-1][0])):
            layout.setdefault(hid, []).append({"id": hid, "t": t, "reason": reason, "state": json.loads(state)})
        frames = []
        for hid, t, minutes, payload, first, last in rows:
            frame = json.loads(payload)
            evs = [events[i] for i in range(first, last + 1) if i in events] if first is not None else []
            frames.append({"id": hid, "t": t, "clock_minutes": minutes, "entities": frame["entities"],
                           "objects": frame["objects"], "gone": frame["gone"], "events": evs})
            frames += layout.get(hid, [])
        return frames, more

    def close(self) -> None:
        self.db.close()


def _row_to_event(r) -> Event:
    return Event(id=r[0], type=r[1], room=r[2], world_time=r[3], real_time=r[4], payload=json.loads(r[5]),
                 audience=set(json.loads(r[6])) if r[6] is not None else None,
                 private_to=set(json.loads(r[7])) if r[7] is not None else None)

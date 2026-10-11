"""SQLite persistence: current world state plus an append-only event log."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .world import Batch, Entity, Event, Obj

SCHEMA_VERSION = 2

# Each step upgrades a database from version N to N + 1.
MIGRATIONS = {
    1: ["ALTER TABLE entities ADD COLUMN avatar TEXT"],  # avatars (protocol 1.1)
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
    avatar TEXT
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
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        has_tables = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() is not None
        version = self._meta("schema_version") if has_tables else None
        if version is None:
            self.db.executescript(SCHEMA)
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
                           avatar=json.loads(r[5]) if r[5] else None)
                    for r in self.db.execute("SELECT id, name, description, room, near, avatar FROM entities")]
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

    def save(self, batch: Batch, clock_minutes: float, speech_seq: dict[str, int]) -> None:
        with self.db:
            for e in batch.entities.values():
                self.db.execute(
                    "INSERT INTO entities (id, name, description, room, near, avatar) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET name = excluded.name, description = excluded.description, "
                    "room = excluded.room, near = excluded.near, avatar = excluded.avatar",
                    (e.id, e.name, e.description, e.room, e.near, json.dumps(e.avatar) if e.avatar else None))
            for o in batch.objects.values():
                self.db.execute(
                    "INSERT INTO objects (id, type, loc_kind, loc_id, state, name, description) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET type = excluded.type, "
                    "loc_kind = excluded.loc_kind, loc_id = excluded.loc_id, state = excluded.state, "
                    "name = excluded.name, description = excluded.description",
                    (o.id, o.type, o.loc_kind, o.loc_id, json.dumps(o.state), o.name, o.description))
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

    def events(self, after: int = 0, limit: int = 500) -> list[Event]:
        rows = self.db.execute(
            "SELECT id, type, room, world_time, real_time, payload, audience, private_to FROM events "
            "WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return [_row_to_event(r) for r in rows]

    def close(self) -> None:
        self.db.close()


def _row_to_event(r) -> Event:
    return Event(id=r[0], type=r[1], room=r[2], world_time=r[3], real_time=r[4], payload=json.loads(r[5]),
                 audience=set(json.loads(r[6])) if r[6] is not None else None,
                 private_to=set(json.loads(r[7])) if r[7] is not None else None)

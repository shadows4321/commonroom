"""Loading world definitions (rooms, object types, initial objects) and agents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from commonroom_protocol import WORLD_FORMAT_VERSION


class ConfigError(ValueError):
    pass


@dataclass
class Verb:
    name: str
    description: str = ""
    requires: dict[str, Any] = field(default_factory=dict)
    needs: str | None = None  # object type the actor must hold to use this verb
    sets: dict[str, Any] = field(default_factory=dict)
    message: str = "{actor} uses {object}."
    reveal: str | None = None  # text shown only to the actor
    fail: str | None = None


@dataclass
class ScheduleEntry:
    at: int  # minute of the day, 0..1439
    sets: dict[str, Any]
    requires: dict[str, Any] = field(default_factory=dict)
    message: str | None = None


@dataclass
class ContainerSpec:
    capacity: int = 10
    requires: dict[str, Any] = field(default_factory=dict)  # state needed to reach inside


@dataclass
class ObjectType:
    id: str
    name: str
    description: str = ""
    portable: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    state_descriptions: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    verbs: dict[str, Verb] = field(default_factory=dict)
    container: ContainerSpec | None = None
    schedule: list[ScheduleEntry] = field(default_factory=list)


@dataclass
class Room:
    id: str
    name: str
    description: str = ""
    exits: dict[str, str] = field(default_factory=dict)  # direction -> room id
    map: tuple[int, int] = (0, 0)
    by_phase: dict[str, str] = field(default_factory=dict)  # extra description per time-of-day phase


@dataclass
class ObjectSpec:
    id: str
    type: str
    loc_kind: str  # "room" | "container" | "entity"
    loc_id: str
    name: str | None = None
    description: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClockSettings:
    start: int = 8 * 60  # world minutes since day 1 00:00
    minutes_per_real_second: float = 0.25
    tick_minutes: int = 15
    run_when_empty: bool = False


@dataclass
class Settings:
    start_room: str = ""
    max_carry: int = 4
    recent_events: int = 20
    viewer_sees_whispers: bool = True
    clock: ClockSettings = field(default_factory=ClockSettings)


@dataclass
class WorldConfig:
    name: str
    rooms: dict[str, Room]
    types: dict[str, ObjectType]
    objects: list[ObjectSpec]
    settings: Settings


@dataclass
class AgentSpec:
    id: str
    name: str
    token: str
    description: str = ""
    start_room: str | None = None


@dataclass
class AgentsConfig:
    agents: dict[str, AgentSpec]
    viewer_token: str | None = None


_TIME_RE = re.compile(r"^(?:day\s+(\d+)\s*,?\s*)?(\d{1,2}):(\d{2})$", re.IGNORECASE)


def parse_time(value: str | int) -> tuple[int, int]:
    """Parse "HH:MM" or "day N HH:MM" into (day, minute_of_day)."""
    if isinstance(value, int):
        return value // 1440 + 1, value % 1440
    m = _TIME_RE.match(str(value).strip())
    if not m:
        raise ConfigError(f"bad time {value!r}, expected 'HH:MM' or 'day N HH:MM'")
    day = int(m.group(1) or 1)
    hour, minute = int(m.group(2)), int(m.group(3))
    if hour > 23 or minute > 59:
        raise ConfigError(f"bad time {value!r}")
    return day, hour * 60 + minute


class _Loader(yaml.SafeLoader):
    """YAML 1.1 reads on/off/yes/no as booleans, which breaks state like `{on: false}`.
    Only true/false are booleans here."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                              list("tTfF"))


def _read_yaml(path: Path) -> Any:
    with path.open() as f:
        return yaml.load(f, Loader=_Loader) or {}


def _check_format(raw: dict, path: Path) -> None:
    version = raw.get("format_version")
    if version is None:
        raise ConfigError(f"{path} is missing format_version (this server reads format_version: "
                          f"{WORLD_FORMAT_VERSION})")
    if version != WORLD_FORMAT_VERSION:
        raise ConfigError(f"{path} has format_version {version!r}; this server reads {WORLD_FORMAT_VERSION}")


def _parse_type(type_id: str, raw: dict) -> ObjectType:
    verbs = {}
    for verb_name, v in (raw.get("verbs") or {}).items():
        v = v or {}
        verbs[verb_name] = Verb(
            name=verb_name,
            description=v.get("description", ""),
            requires=dict(v.get("requires") or {}),
            needs=v.get("needs"),
            sets=dict(v.get("sets") or {}),
            message=v.get("message", "{actor} uses {object}."),
            reveal=v.get("reveal"),
            fail=v.get("fail"),
        )
    container = None
    if raw.get("container"):
        c = raw["container"] if isinstance(raw["container"], dict) else {}
        container = ContainerSpec(capacity=int(c.get("capacity", 10)), requires=dict(c.get("requires") or {}))
    schedule = []
    for s in raw.get("schedule") or []:
        _, minute = parse_time(s["at"])
        schedule.append(ScheduleEntry(at=minute, sets=dict(s.get("sets") or {}),
                                      requires=dict(s.get("requires") or {}), message=s.get("message")))
    return ObjectType(
        id=type_id,
        name=raw.get("name", type_id.replace("_", " ")),
        description=raw.get("description", ""),
        portable=bool(raw.get("portable", False)),
        state=dict(raw.get("state") or {}),
        state_descriptions=[(dict(d.get("when") or {}), d["text"]) for d in raw.get("state_descriptions") or []],
        verbs=verbs,
        container=container,
        schedule=schedule,
    )


def load_world(world_dir: str | Path) -> WorldConfig:
    """Load world.yaml (rooms, objects, settings) and objects.yaml (types) from a directory."""
    world_dir = Path(world_dir)
    raw = _read_yaml(world_dir / "world.yaml")
    _check_format(raw, world_dir / "world.yaml")
    types_raw = _read_yaml(world_dir / "objects.yaml") if (world_dir / "objects.yaml").exists() else {}
    if types_raw:
        _check_format(types_raw, world_dir / "objects.yaml")
    types_raw = types_raw.get("types", {})

    types = {tid: _parse_type(tid, t or {}) for tid, t in types_raw.items()}

    rooms: dict[str, Room] = {}
    for rid, r in (raw.get("rooms") or {}).items():
        r = r or {}
        rooms[rid] = Room(
            id=rid,
            name=r.get("name", rid.replace("_", " ").title()),
            description=r.get("description", ""),
            exits={str(k): str(v) for k, v in (r.get("exits") or {}).items()},
            map=tuple(r.get("map", (0, 0))),
            by_phase=dict(r.get("by_phase") or {}),
        )
    if not rooms:
        raise ConfigError("world has no rooms")
    for room in rooms.values():
        for direction, target in room.exits.items():
            if target not in rooms:
                raise ConfigError(f"room {room.id!r} exit {direction!r} leads to unknown room {target!r}")

    objects: list[ObjectSpec] = []
    seen: set[str] = set()
    for o in raw.get("objects") or []:
        oid = o["id"]
        if oid in seen:
            raise ConfigError(f"duplicate object id {oid!r}")
        seen.add(oid)
        if o.get("type") not in types:
            raise ConfigError(f"object {oid!r} has unknown type {o.get('type')!r}")
        if "room" in o:
            kind, loc = "room", o["room"]
        elif "in" in o:
            kind, loc = "container", o["in"]
        else:
            raise ConfigError(f"object {oid!r} needs 'room' or 'in'")
        objects.append(ObjectSpec(id=oid, type=o["type"], loc_kind=kind, loc_id=loc,
                                  name=o.get("name"), description=o.get("description"),
                                  state=dict(o.get("state") or {})))
    for spec in objects:
        if spec.loc_kind == "room" and spec.loc_id not in rooms:
            raise ConfigError(f"object {spec.id!r} is in unknown room {spec.loc_id!r}")
        if spec.loc_kind == "container":
            parent = next((p for p in objects if p.id == spec.loc_id), None)
            if parent is None or types[parent.type].container is None:
                raise ConfigError(f"object {spec.id!r} is in {spec.loc_id!r}, which is not a container")

    s = raw.get("settings") or {}
    c = s.get("clock") or {}
    start_day, start_minute = parse_time(c.get("start", "08:00"))
    clock = ClockSettings(
        start=(start_day - 1) * 1440 + start_minute,
        minutes_per_real_second=float(c.get("minutes_per_real_second", 0.25)),
        tick_minutes=int(c.get("tick_minutes", 15)),
        run_when_empty=bool(c.get("run_when_empty", False)),
    )
    settings = Settings(
        start_room=s.get("start_room") or next(iter(rooms)),
        max_carry=int(s.get("max_carry", 4)),
        recent_events=int(s.get("recent_events", 20)),
        viewer_sees_whispers=bool(s.get("viewer_sees_whispers", True)),
        clock=clock,
    )
    if settings.start_room not in rooms:
        raise ConfigError(f"start_room {settings.start_room!r} is not a room")
    return WorldConfig(name=raw.get("name", world_dir.name), rooms=rooms, types=types,
                       objects=objects, settings=settings)


def load_agents(path: str | Path) -> AgentsConfig:
    """Load agents.yaml: one entry (and one secret token) per friend's AI."""
    path = Path(path)
    raw = _read_yaml(path) if path.exists() else {}
    agents = {}
    tokens = set()
    for a in raw.get("agents") or []:
        spec = AgentSpec(id=a["id"], name=a.get("name", a["id"]), token=str(a["token"]),
                         description=a.get("description", ""), start_room=a.get("start_room"))
        if spec.id in agents:
            raise ConfigError(f"duplicate agent id {spec.id!r}")
        if spec.token in tokens:
            raise ConfigError(f"agent {spec.id!r} reuses another agent's token")
        agents[spec.id] = spec
        tokens.add(spec.token)
    return AgentsConfig(agents=agents, viewer_token=raw.get("viewer_token"))

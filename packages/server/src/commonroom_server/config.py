"""Loading world definitions (rooms, object types, initial objects) and agents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from commonroom_protocol import SUPPORTED_WORLD_FORMATS, WORLD_FORMAT_VERSION


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
    area: str = "town"  # which part of the world it belongs to: "town", or a house's owner id


STREET = "@street"  # in a house file, the exit target meaning "the street in front of this house"


@dataclass
class Area:
    """A part of the world with its own 3D model: the town itself, or one house."""
    id: str
    name: str
    kind: str  # "town" | "house"
    owner: str | None = None
    plot: str | None = None
    model: Path | None = None  # .glb, relative paths resolved against the world directory
    entrance: str | None = None  # house: the room whose exit leads to the street


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
    avatars: dict[str, Any] = field(default_factory=dict)  # limits, see avatars.AvatarLimits.from_settings


@dataclass
class WorldConfig:
    name: str
    rooms: dict[str, Room]
    types: dict[str, ObjectType]
    objects: list[ObjectSpec]
    settings: Settings
    areas: dict[str, Area] = field(default_factory=dict)
    homes: dict[str, str] = field(default_factory=dict)  # agent id -> the room it starts in
    world_dir: Path | None = None


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
    viewer_password: str | None = None
    public_viewer: bool = False  # explicitly allow watching without a password


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


def _check_format(raw: dict, path: Path) -> int:
    version = raw.get("format_version")
    readable = ", ".join(str(v) for v in SUPPORTED_WORLD_FORMATS)
    if version is None:
        raise ConfigError(f"{path} is missing format_version (this server reads format_version {readable})")
    if version not in SUPPORTED_WORLD_FORMATS:
        raise ConfigError(f"{path} has format_version {version!r}; this server reads {readable}")
    return version


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


def _parse_rooms(raw_rooms: dict, *, prefix: str = "", area: str = "town", name_prefix: str = "",
                 map_offset: tuple[int, int] = (0, 0)) -> dict[str, Room]:
    rooms: dict[str, Room] = {}
    for rid, r in (raw_rooms or {}).items():
        r = r or {}
        local_name = r.get("name", str(rid).replace("_", " ").title())
        mx, my = tuple(r.get("map", (0, 0)))
        exits = {}
        for direction, target in (r.get("exits") or {}).items():
            target = str(target)
            exits[str(direction)] = target if target == STREET else f"{prefix}{target}"
        rooms[f"{prefix}{rid}"] = Room(
            id=f"{prefix}{rid}",
            name=f"{name_prefix}{local_name}",
            description=r.get("description", ""),
            exits=exits,
            map=(map_offset[0] + int(mx), map_offset[1] + int(my)),
            by_phase=dict(r.get("by_phase") or {}),
            area=r.get("area", area) if not prefix else area,
        )
    return rooms


def _parse_objects(raw_objects: list, types: dict[str, ObjectType], *, prefix: str = "", where: str) -> list[ObjectSpec]:
    objects = []
    for o in raw_objects or []:
        oid = f"{prefix}{o['id']}"
        if o.get("type") not in types:
            raise ConfigError(f"{where}: object {o['id']!r} has unknown type {o.get('type')!r}")
        if "room" in o:
            kind, loc = "room", f"{prefix}{o['room']}"
        elif "in" in o:
            kind, loc = "container", f"{prefix}{o['in']}"
        else:
            raise ConfigError(f"{where}: object {o['id']!r} needs 'room' or 'in'")
        objects.append(ObjectSpec(id=oid, type=o["type"], loc_kind=kind, loc_id=loc,
                                  name=o.get("name"), description=o.get("description"),
                                  state=dict(o.get("state") or {})))
    return objects


def _world_file(world_dir: Path, rel: str, what: str) -> Path:
    """A path from a world file, which must stay inside the world directory."""
    path = (world_dir / rel).resolve()
    if world_dir.resolve() not in path.parents:
        raise ConfigError(f"{what} {rel!r} is outside the world directory")
    return path


def _load_houses(raw: dict, world_dir: Path, rooms: dict[str, Room], types: dict[str, ObjectType],
                 areas: dict[str, Area], homes: dict[str, str]) -> list[ObjectSpec]:
    """Format 2: houses, one per AI, each with its own file of rooms and objects."""
    plots = {str(k): (v or {}) for k, v in (raw.get("plots") or {}).items()}
    objects: list[ObjectSpec] = []
    used_plots: set[str] = set()
    for h in raw.get("houses") or []:
        owner = str(h["owner"])
        plot = str(h["plot"])
        where = f"house of {owner!r}"
        if owner in areas:
            raise ConfigError(f"{owner!r} has more than one house")
        if plot not in plots:
            raise ConfigError(f"{where} is on plot {plot!r}, which isn't listed under plots")
        if plot in used_plots:
            raise ConfigError(f"plot {plot!r} has more than one house")
        used_plots.add(plot)
        street = plots[plot].get("street")
        if street not in rooms:
            raise ConfigError(f"plot {plot!r} faces street {street!r}, which isn't a town room")

        house_path = _world_file(world_dir, h.get("file", f"houses/{owner}.yaml"), f"{where} file")
        if not house_path.exists():
            raise ConfigError(f"{where}: {house_path} doesn't exist (run `commonroom-server import-glb`?)")
        house = _read_yaml(house_path)
        _check_format(house, house_path)
        for tid, t in (house.get("types") or {}).items():
            if tid in types:
                raise ConfigError(f"{house_path}: type {tid!r} is already defined")
            types[tid] = _parse_type(tid, t or {})

        owner_name = h.get("owner_name", owner.replace("_", " ").title())
        name = house.get("name", f"{owner_name}'s house")
        pmap = tuple(plots[plot].get("map", (0, 0)))
        house_rooms = _parse_rooms(house.get("rooms"), prefix=f"{owner}.", area=owner,
                                   name_prefix=f"{owner_name}'s ", map_offset=(int(pmap[0]), int(pmap[1])))
        if not house_rooms:
            raise ConfigError(f"{house_path} has no rooms")
        entrances = [r for r in house_rooms.values() if STREET in r.exits.values()]
        if len(entrances) != 1:
            raise ConfigError(f"{house_path} needs exactly one exit to {STREET!r} (found {len(entrances)})")
        entrance = entrances[0]
        for room in house_rooms.values():
            for direction, target in list(room.exits.items()):
                if target == STREET:
                    room.exits[direction] = street
        rooms[street].exits[name] = entrance.id  # the street side of the front door
        clash = set(house_rooms) & set(rooms)
        if clash:
            raise ConfigError(f"{where}: room ids {sorted(clash)} are already used")
        rooms.update(house_rooms)

        model = h.get("model", f"houses/{owner}.glb")
        areas[owner] = Area(id=owner, name=name, kind="house", owner=owner, plot=plot,
                            model=_world_file(world_dir, model, f"{where} model") if model else None,
                            entrance=entrance.id)
        home = house.get("home")
        homes[owner] = f"{owner}.{home}" if home else next(iter(house_rooms))
        if homes[owner] not in rooms:
            raise ConfigError(f"{house_path}: home {home!r} is not one of its rooms")
        objects += _parse_objects(house.get("objects"), types, prefix=f"{owner}.", where=str(house_path))
    return objects


def load_world(world_dir: str | Path) -> WorldConfig:
    """Load world.yaml (rooms, objects, settings, and for towns, houses) and objects.yaml (types)."""
    world_dir = Path(world_dir)
    raw = _read_yaml(world_dir / "world.yaml")
    version = _check_format(raw, world_dir / "world.yaml")
    types_raw = _read_yaml(world_dir / "objects.yaml") if (world_dir / "objects.yaml").exists() else {}
    if types_raw:
        _check_format(types_raw, world_dir / "objects.yaml")
    types_raw = types_raw.get("types", {})

    types = {tid: _parse_type(tid, t or {}) for tid, t in types_raw.items()}
    rooms = _parse_rooms(raw.get("rooms"))
    if not rooms:
        raise ConfigError("world has no rooms")

    town_model = raw.get("scene")
    areas = {"town": Area(id="town", name=raw.get("name", world_dir.name), kind="town",
                          model=_world_file(world_dir, town_model, "scene") if town_model else None)}
    homes: dict[str, str] = {}
    objects = _parse_objects(raw.get("objects"), types, where="world.yaml")
    if version >= 2:
        objects += _load_houses(raw, world_dir, rooms, types, areas, homes)
    elif raw.get("houses") or raw.get("scene"):
        raise ConfigError("houses and scene need format_version 2")

    for room in rooms.values():
        for direction, target in room.exits.items():
            if target not in rooms:
                raise ConfigError(f"room {room.id!r} exit {direction!r} leads to unknown room {target!r}")

    seen: set[str] = set()
    for spec in objects:
        if spec.id in seen:
            raise ConfigError(f"duplicate object id {spec.id!r}")
        seen.add(spec.id)
    by_id = {o.id: o for o in objects}
    for spec in objects:
        if spec.loc_kind == "room" and spec.loc_id not in rooms:
            raise ConfigError(f"object {spec.id!r} is in unknown room {spec.loc_id!r}")
        if spec.loc_kind == "container":
            parent = by_id.get(spec.loc_id)
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
        avatars=dict(s.get("avatars") or {}),
    )
    if settings.start_room not in rooms:
        raise ConfigError(f"start_room {settings.start_room!r} is not a room")
    return WorldConfig(name=raw.get("name", world_dir.name), rooms=rooms, types=types, objects=objects,
                       settings=settings, areas=areas, homes=homes, world_dir=world_dir)


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
    password = raw.get("viewer_password", raw.get("viewer_token"))  # viewer_token: the 0.1 name
    password = None if password is None else str(password)
    if password is not None and len(password) < 8:
        raise ConfigError("viewer_password must be at least 8 characters")
    if password in tokens:
        raise ConfigError("viewer_password must not be the same as an agent's token")
    return AgentsConfig(agents=agents, viewer_password=password, public_viewer=bool(raw.get("public_viewer", False)))

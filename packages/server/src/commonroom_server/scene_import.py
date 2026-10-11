"""Reading the named markers out of a town or house .glb (see docs/3d-spec.md) and
turning them into world YAML: rooms, exits, objects, plots.

Re-importing after a re-export keeps everything written by hand (names,
descriptions, object types and state); only what the model decides (which rooms
and objects exist, where exits lead, which room an object is in) is updated.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .avatars import _parse_glb
from .config import STREET

PAIR_DISTANCE = 3.0  # metres: the two exit markers of one doorway must be at most this far apart
STAIRS = {"upstairs": "downstairs", "downstairs": "upstairs"}
MAX_CANDELA = 60.0  # brighter local lights wash out the viewer (its sun runs from 0.12 to 2.8)
PREFIXES = {"room", "floor", "wall", "ceiling", "ext", "exit", "obj", "when", "time", "spot", "seat", "spawn",
            "camera", "plot", "gate"}
PHASES = {"night", "dawn", "morning", "afternoon", "evening"}
_SUFFIX = re.compile(r"\.\d{3,}$")


class ImportProblem(ValueError):
    pass


@dataclass
class Node:
    name: str  # without Blender's .001 suffix
    kind: str  # prefix before the first ':' ("room", "exit", ...), or "" for scenery
    args: list[str]  # the rest, split on ':'
    position: tuple[float, float, float]  # world space, glTF axes (Y up)
    room: str | None  # nearest room:<id> ancestor
    obj: str | None = None  # nearest obj:<id> ancestor (not counting the node itself)
    light: float | None = None  # intensity of a point or spot light on this node (candela)


@dataclass
class Scene:
    nodes: list[Node]
    warnings: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Node]:
        return [n for n in self.nodes if n.kind == kind]


# ------------------------------------------------------------- glTF reading

def _mat_mul(a: list[float], b: list[float]) -> list[float]:
    # column-major 4x4, as glTF stores them
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def _local_matrix(node: dict) -> list[float]:
    if "matrix" in node:
        return [float(v) for v in node["matrix"]]
    tx, ty, tz = node.get("translation", (0, 0, 0))
    qx, qy, qz, qw = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    rot = [
        1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy + qz * qw), 2 * (qx * qz - qy * qw), 0,
        2 * (qx * qy - qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz + qx * qw), 0,
        2 * (qx * qz + qy * qw), 2 * (qy * qz - qx * qw), 1 - 2 * (qx * qx + qy * qy), 0,
        0, 0, 0, 1,
    ]
    scaled = [rot[i] * (sx, sy, sz, 1)[i // 4] for i in range(16)]
    scaled[12:15] = [tx, ty, tz]
    return scaled


def read_scene(path: str | Path) -> Scene:
    """All named nodes of a .glb with their world positions and room."""
    doc, _ = _parse_glb(Path(path).read_bytes())
    nodes = doc.get("nodes") or []
    lights = ((doc.get("extensions") or {}).get("KHR_lights_punctual") or {}).get("lights") or []
    scenes = doc.get("scenes") or [{"nodes": list(range(len(nodes)))}]
    roots = scenes[doc.get("scene", 0)].get("nodes") or []
    out: list[Node] = []

    def visit(index: int, parent: list[float], room: str | None, obj: str | None, seen: set[int]) -> None:
        if index in seen or not 0 <= index < len(nodes):
            return
        seen = seen | {index}
        node = nodes[index]
        world = _mat_mul(parent, _local_matrix(node))
        name = _SUFFIX.sub("", str(node.get("name", "")))
        kind, _, rest = name.partition(":") if ":" in name else ("", "", name)
        args = rest.split(":") if kind else []
        if kind == "room" and args:
            room = args[0]
        light = ((node.get("extensions") or {}).get("KHR_lights_punctual") or {}).get("light")
        intensity = None
        if isinstance(light, int) and 0 <= light < len(lights) and lights[light].get("type") != "directional":
            intensity = float(lights[light].get("intensity", 1.0))
        out.append(Node(name=name, kind=kind, args=args, position=(world[12], world[13], world[14]), room=room,
                        obj=obj, light=intensity))
        if kind == "obj" and args:
            obj = args[0]
        for child in node.get("children") or []:
            visit(child, world, room, obj, seen)

    identity = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    for root in roots:
        visit(root, identity, None, None, set())
    return Scene(nodes=out)


def _distance(a: Node, b: Node) -> float:
    return math.dist(a.position, b.position)


# --------------------------------------------------------- shared analysis

def _rooms(scene: Scene) -> dict[str, Node]:
    rooms = {}
    for n in scene.of("room"):
        if not n.args or not re.fullmatch(r"[a-z0-9_]+", n.args[0]):
            raise ImportProblem(f"bad room name {n.name!r}: use room:<id> with lowercase letters, digits and _")
        if n.args[0] in rooms:
            raise ImportProblem(f"two objects are named room:{n.args[0]}")
        rooms[n.args[0]] = n
    for n in scene.of("floor"):
        if n.args and n.args[0] not in rooms:
            scene.warnings.append(f"{n.name} has no matching room:{n.args[0]}")
    return rooms


def _exits(scene: Scene, rooms: dict[str, Node]) -> dict[str, dict[str, str]]:
    """{room: {label: target room or STREET}} from exit:<room>:<label> pairs."""
    markers = []
    for n in scene.of("exit"):
        if len(n.args) != 2 or n.args[0] not in rooms:
            scene.warnings.append(f"ignoring {n.name}: expected exit:<room>:<label> with a known room")
            continue
        markers.append(n)
    exits: dict[str, dict[str, str]] = {r: {} for r in rooms}
    for m in markers:
        room, label = m.args
        if label == "out":
            exits[room][label] = STREET
            continue
        others = [o for o in markers if o.args[0] != room and o.args[1] != "out"]
        if label in STAIRS:
            others = [o for o in others if o.args[1] == STAIRS[label]] or others
            partner = min(others, key=lambda o: _distance(m, o), default=None)
        else:
            partner = min(others, key=lambda o: _distance(m, o), default=None)
            if partner is not None and _distance(m, partner) > PAIR_DISTANCE:
                partner = None
        if partner is None:
            scene.warnings.append(f"{m.name} has no exit marker from another room within {PAIR_DISTANCE:g} m; skipped")
            continue
        exits[room][label] = partner.args[0]
    return exits


def _map_positions(scene: Scene, rooms: dict[str, Node], cell: float) -> dict[str, list[int]]:
    """Grid positions for the 2D viewer, from where each room's floor is."""
    centres = {}
    for rid, node in rooms.items():
        floors = [n for n in scene.of("floor") if n.args and n.args[0] == rid]
        pts = [f.position for f in floors] or [node.position]
        centres[rid] = (sum(p[0] for p in pts) / len(pts), sum(p[2] for p in pts) / len(pts))
    taken: set[tuple[int, int]] = set()
    result = {}
    for rid, (x, z) in sorted(centres.items(), key=lambda kv: (kv[1][1], kv[1][0])):
        col, row = round(x / cell), round(z / cell)
        while (col, row) in taken:
            col += 1
        taken.add((col, row))
        result[rid] = [col, row]
    if result:
        min_c = min(c for c, _ in result.values())
        min_r = min(r for _, r in result.values())
        result = {rid: [c - min_c, r - min_r] for rid, (c, r) in result.items()}
    return result


def _objects(scene: Scene, rooms: dict[str, Node], known_types: set[str]) -> list[dict]:
    objects = []
    seen = set()
    for n in scene.of("obj"):
        if not n.args:
            continue
        oid = n.args[0]
        if oid in seen:
            continue
        seen.add(oid)
        room = n.room if n.room in rooms else None
        if room is None and rooms:
            room = min(rooms, key=lambda r: math.dist(rooms[r].position, n.position))
            scene.warnings.append(f"obj:{oid} isn't parented to a room; putting it in {room}")
        objects.append({"id": oid, "type": guess_type(oid, known_types), "room": room})
    return objects


def guess_type(oid: str, known_types) -> str:
    """An object's type from its id: the id itself, or its last words after an underscore, ignoring a
    trailing number (kettle, big_kettle and kettle_2 are kettles; street_lantern_1 is a lantern)."""
    base = re.sub(r"_\d+$", "", oid)
    for candidate in (oid, base):
        if candidate in known_types:
            return candidate
    matches = [t for t in known_types if base.endswith(f"_{t}")]
    return max(matches, key=len) if matches else "fixture"


@dataclass
class TypeInfo:
    """What the checks need to know about an object type."""
    state: set[str]  # every state key it has or sets
    seats: int = 0


def type_info(raw_types: dict) -> dict[str, TypeInfo]:
    """TypeInfo for each type in an objects.yaml `types:` mapping."""
    info = {}
    for tid, raw in (raw_types or {}).items():
        raw = raw or {}
        keys = set(raw.get("state") or {})
        verbs = raw.get("verbs") or {}
        for v in verbs.values():
            keys |= set((v or {}).get("sets") or {}) | set((v or {}).get("requires") or {})
        for d in raw.get("state_descriptions") or []:
            keys |= set(d.get("when") or {})
        for entry in raw.get("schedule") or []:
            keys |= set(entry.get("sets") or {})
        posed = any((v or {}).get("pose") for v in verbs.values())
        info[str(tid)] = TypeInfo(state=keys, seats=int(raw.get("seats", 1 if posed else 0)))
    return info


def _check(scene: Scene, objects: list[dict], types) -> None:
    """Warnings for names the viewer or server will ignore or misread. `types` maps type names to TypeInfo
    (when it's a plain set of names, the state checks are skipped)."""
    obj_ids = {o["id"] for o in objects}
    obj_type = {o["id"]: o.get("type") for o in objects}
    seats: dict[str, int] = {}
    for n in scene.nodes:
        if n.kind and n.kind not in PREFIXES:
            scene.warnings.append(f"{n.name}: unknown prefix {n.kind + ':'!r}; it's plain scenery")
        if n.kind == "obj" and n.args and not re.fullmatch(r"[a-z0-9_]+", n.args[0]):
            scene.warnings.append(f"{n.name}: object ids use lowercase letters, digits and _")
        if n.kind in ("spot", "seat") and n.args and n.args[0] not in obj_ids:
            scene.warnings.append(f"{n.name} has no matching obj:{n.args[0]}")
        if n.kind == "seat" and n.args:
            seats[n.args[0]] = seats.get(n.args[0], 0) + 1
        if n.kind == "time":
            bad = [p for p in ":".join(n.args).split("+") if p not in PHASES]
            if bad:
                scene.warnings.append(f"{n.name}: unknown time of day {', '.join(bad)} "
                                      f"(use {', '.join(sorted(PHASES))})")
        if n.kind == "when":
            if n.obj is None:
                scene.warnings.append(f"{n.name} isn't inside an obj:, so nothing will switch it")
                continue
            info = types.get(obj_type.get(n.obj)) if isinstance(types, dict) else None
            if info is None:
                continue
            keys = [part.split("=")[0].strip() for part in ":".join(n.args).split(",")]
            missing = [k for k in keys if k not in info.state]
            if missing:
                known = ", ".join(sorted(info.state)) or "none"
                scene.warnings.append(f"{n.name} under obj:{n.obj}: type {obj_type[n.obj]!r} has no state "
                                      f"{', '.join(missing)} (it has: {known}), so this part never shows")
        if n.light is not None and n.light > MAX_CANDELA:
            scene.warnings.append(f"{n.name or 'a light'} is {n.light:g} cd; the viewer looks right with about "
                                  f"10-40 cd for a street lamp and 5-15 cd indoors (Blender: 1 W = 54 cd)")
    for oid, count in seats.items():
        info = types.get(obj_type.get(oid)) if isinstance(types, dict) else None
        if info is not None and not info.seats:
            scene.warnings.append(f"obj:{oid} has seat: markers, but type {obj_type[oid]!r} has no verb "
                                  f"with a pose, so nobody can sit there")
        elif info is not None and count > info.seats:
            scene.warnings.append(f"obj:{oid} has {count} seat: markers but its type seats {info.seats}")


def _merge_rooms(old: dict, rooms: dict[str, Node], exits: dict, maps: dict, warnings: list[str]) -> dict:
    merged = {}
    for rid in rooms:
        entry = dict(old.get(rid) or {})
        entry.setdefault("name", rid.replace("_", " ").title())
        entry["exits"] = exits.get(rid, {})
        entry["map"] = maps.get(rid, [0, 0])
        merged[rid] = entry
    for rid in old:
        if rid not in rooms:
            warnings.append(f"room {rid!r} is no longer in the model; removed")
    return merged


def _merge_objects(old: list, new: list[dict], warnings: list[str]) -> list[dict]:
    old_by_id = {o["id"]: o for o in old or []}
    merged = []
    for o in new:
        entry = dict(old_by_id.get(o["id"]) or {"id": o["id"], "type": o["type"]})
        if "in" not in entry:  # hand-placed inside a container: keep it there
            entry["room"] = o["room"]
        merged.append(entry)
    new_ids = {o["id"] for o in new}
    for o in old or []:
        if o["id"] not in new_ids and "in" in o and o["in"] in new_ids:
            merged.append(o)  # an object that only exists inside a container (not modelled) stays
        elif o["id"] not in new_ids:
            warnings.append(f"object {o['id']!r} is no longer in the model; removed")
    return merged


# ------------------------------------------------------------------ house

def import_house(glb: str | Path, existing: dict | None, known_types: set[str]) -> tuple[dict, list[str]]:
    """House .glb -> the house YAML (as a dict), merged into `existing`."""
    scene = read_scene(glb)
    rooms = _rooms(scene)
    if not rooms:
        raise ImportProblem("no room:<id> found; a house needs at least one room")
    exits = _exits(scene, rooms)
    outs = [r for r, e in exits.items() if STREET in e.values()]
    if len(outs) != 1:
        raise ImportProblem(f"a house needs exactly one exit labelled 'out' (found {len(outs)})")
    for n in scene.nodes:
        x, _, z = n.position
        if n.kind in ("room", "floor", "ext", "wall", "obj") and (abs(x) > 8.5 or abs(z) > 8.5):
            scene.warnings.append(f"{n.name} is outside the 16 x 16 m plot ({x:.1f}, {-z:.1f})")
    existing = dict(existing or {})
    result = {"format_version": 2}
    result.update({k: v for k, v in existing.items() if k not in ("rooms", "objects", "format_version")})
    result["rooms"] = _merge_rooms(existing.get("rooms") or {}, rooms, exits, _map_positions(scene, rooms, 5.0),
                                   scene.warnings)
    result["objects"] = _merge_objects(existing.get("objects"), _objects(scene, rooms, known_types), scene.warnings)
    _check(scene, result["objects"], known_types)
    return result, scene.warnings


# ------------------------------------------------------------------- town

def import_town(glb: str | Path, existing: dict, known_types: set[str]) -> tuple[dict, list[str]]:
    """Town .glb -> world.yaml (as a dict): town rooms, exits, objects and plots, merged into `existing`."""
    scene = read_scene(glb)
    rooms = _rooms(scene)
    if not rooms:
        raise ImportProblem("no room:<id> found; a town needs at least one street or square")
    exits = _exits(scene, rooms)
    for rid, ex in exits.items():
        if STREET in ex.values():
            scene.warnings.append(f"room {rid!r} has an 'out' exit; 'out' is only for houses")
            exits[rid] = {k: v for k, v in ex.items() if v != STREET}

    plots: dict[str, dict[str, Any]] = {}
    gates = {n.args[0]: n for n in scene.of("gate") if n.args}
    plot_numbers = {n.args[0] for n in scene.of("plot") if n.args}
    for number in gates:
        if number not in plot_numbers:
            scene.warnings.append(f"gate:{number} has no plot:{number}; ignored")
    for p in scene.of("plot"):
        if not p.args:
            continue
        number = p.args[0]
        gate = gates.get(number)
        if gate is None:
            scene.warnings.append(f"plot:{number} has no gate:{number}; skipped")
            continue
        street = gate.room if gate.room in rooms else None
        if street is None:
            scene.warnings.append(f"gate:{number} isn't parented to a room:; skipped plot {number}")
            continue
        plots[number] = {"street": street}

    existing = dict(existing or {})
    result = {"format_version": 2}
    result.update({k: v for k, v in existing.items() if k not in ("format_version",)})
    result.setdefault("scene", Path(glb).name)
    old_rooms = existing.get("rooms") or {}
    result["rooms"] = _merge_rooms(old_rooms, rooms, exits, _map_positions(scene, rooms, 20.0), scene.warnings)
    # Exits into houses are added by the server from the houses list, so drop any saved ones.
    result["plots"] = {**{k: v for k, v in (existing.get("plots") or {}).items() if k in plots}, **plots}
    result["objects"] = _merge_objects(existing.get("objects"), _objects(scene, rooms, known_types), scene.warnings)
    _check(scene, result["objects"], known_types)
    for h in existing.get("houses") or []:
        if str(h.get("plot")) not in plots:
            scene.warnings.append(f"the house of {h.get('owner')!r} is on plot {h.get('plot')!r}, "
                                  f"which isn't in the model any more")
    return result, scene.warnings

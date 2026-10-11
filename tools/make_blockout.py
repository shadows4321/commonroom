"""Generate the placeholder town and houses for worlds/town, following docs/3d-spec.md.

    python tools/make_blockout.py            # writes worlds/town/town.glb and worlds/town/houses/*.glb

The output is deliberately plain (coloured boxes), but every marker the spec
describes is there, named the same way Blender would export it. Import the files
into Blender (File > Import > glTF 2.0) to see a working example of the naming,
or replace them with real models.

Coordinates below are written the way you'd read them in Blender (X right,
Y back, Z up) and converted to glTF's Y-up axes on export.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- tiny glTF writer

@dataclass
class N:
    """A node: an Empty unless it has a box or a light."""
    name: str
    at: tuple[float, float, float] = (0, 0, 0)  # Blender coordinates, relative to the parent
    box: tuple[float, float, float] | None = None  # size in Blender axes, centred on `at`
    color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    emissive: tuple[float, float, float] | None = None
    light: tuple[float, float, float, float] | None = None  # r, g, b, intensity (candela)
    rot_z: float = 0.0  # degrees, around Blender Z
    children: list["N"] = field(default_factory=list)

    def add(self, *nodes: "N") -> "N":
        self.children.extend(nodes)
        return self


def _gl(v):  # Blender (x, y, z) -> glTF (x, z, -y)
    return (v[0], v[2], -v[1])


def _box_geometry(size):
    sx, sy, sz = _gl(size)
    sy, sz = abs(sy), abs(sz)
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    faces = [((1, 0, 0), [(hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz), (hx, -hy, hz)]),
             ((-1, 0, 0), [(-hx, -hy, hz), (-hx, hy, hz), (-hx, hy, -hz), (-hx, -hy, -hz)]),
             ((0, 1, 0), [(-hx, hy, -hz), (-hx, hy, hz), (hx, hy, hz), (hx, hy, -hz)]),
             ((0, -1, 0), [(-hx, -hy, hz), (-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz)]),
             ((0, 0, 1), [(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]),
             ((0, 0, -1), [(hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz)])]
    positions, normals, indices = [], [], []
    for normal, quad in faces:
        base = len(positions)
        positions += quad
        normals += [normal] * 4
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    return positions, normals, indices


def write_glb(path: Path, roots: list[N]) -> None:
    doc = {"asset": {"version": "2.0", "generator": "commonroom make_blockout.py"},
           "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [],
           "accessors": [], "bufferViews": [], "buffers": []}
    blob = bytearray()
    materials: dict[tuple, int] = {}
    lights: list[dict] = []

    def view(data: bytes, target: int) -> int:
        while len(blob) % 4:
            blob.append(0)
        doc["bufferViews"].append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), "target": target})
        blob.extend(data)
        return len(doc["bufferViews"]) - 1

    def material(color, emissive) -> int:
        key = (color, emissive)
        if key not in materials:
            m = {"pbrMetallicRoughness": {"baseColorFactor": [*color, 1.0], "metallicFactor": 0.0,
                                          "roughnessFactor": 0.85}}
            if emissive:
                m["emissiveFactor"] = list(emissive)
            doc["materials"].append(m)
            materials[key] = len(doc["materials"]) - 1
        return materials[key]

    def mesh(n: N) -> int:
        positions, normals, indices = _box_geometry(n.box)
        pv = view(b"".join(struct.pack("<3f", *p) for p in positions), 34962)
        nv = view(b"".join(struct.pack("<3f", *v) for v in normals), 34962)
        iv = view(struct.pack(f"<{len(indices)}H", *indices), 34963)
        acc = doc["accessors"]
        acc.append({"bufferView": pv, "componentType": 5126, "count": len(positions), "type": "VEC3",
                    "min": [min(p[i] for p in positions) for i in range(3)],
                    "max": [max(p[i] for p in positions) for i in range(3)]})
        acc.append({"bufferView": nv, "componentType": 5126, "count": len(normals), "type": "VEC3"})
        acc.append({"bufferView": iv, "componentType": 5123, "count": len(indices), "type": "SCALAR"})
        doc["meshes"].append({"name": n.name, "primitives": [{
            "attributes": {"POSITION": len(acc) - 3, "NORMAL": len(acc) - 2}, "indices": len(acc) - 1,
            "material": material(n.color, n.emissive)}]})
        return len(doc["meshes"]) - 1

    def add(n: N) -> int:
        node = {"name": n.name, "translation": list(_gl(n.at))}
        if n.rot_z:
            half = math.radians(n.rot_z) / 2  # rotation around Blender Z = glTF Y
            node["rotation"] = [0, math.sin(half), 0, math.cos(half)]
        if n.box:
            node["mesh"] = mesh(n)
        if n.light:
            lights.append({"type": "point", "color": list(n.light[:3]), "intensity": n.light[3], "range": 8})
            node["extensions"] = {"KHR_lights_punctual": {"light": len(lights) - 1}}
        index = len(doc["nodes"])
        doc["nodes"].append(node)
        if n.children:
            node["children"] = [add(c) for c in n.children]
        return index

    doc["scenes"][0]["nodes"] = [add(r) for r in roots]
    if lights:
        doc["extensionsUsed"] = ["KHR_lights_punctual"]
        doc["extensions"] = {"KHR_lights_punctual": {"lights": lights}}
    while len(blob) % 4:
        blob.append(0)
    doc["buffers"].append({"byteLength": len(blob)})
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)


# ------------------------------------------------------------------- the town

ROAD = (0.33, 0.33, 0.36)
PAVING = (0.62, 0.6, 0.55)
GRASS = (0.38, 0.55, 0.3)
WALL = (0.93, 0.89, 0.8)
WOOD = (0.55, 0.37, 0.22)
WARM = (1.0, 0.8, 0.5)

PLOTS = {  # number: (x, y, rotation, street) in Blender coordinates
    "1": (-48, 13, 0, "street_west"), "2": (-30, 13, 0, "street_west"),
    "3": (-48, -13, 180, "street_west"), "4": (-30, -13, 180, "street_west"),
    "5": (30, 13, 0, "street_east"), "6": (48, 13, 0, "street_east"),
}


def street_lamp(name: str, x: float, y: float) -> N:
    return N(f"obj:{name}", (x, y, 0)).add(
        N("lamp_post", (0, 0, 1.6), box=(0.15, 0.15, 3.2), color=(0.15, 0.15, 0.17)),
        N("when:lit=true", (0, 0, 3.1)).add(
            N("lamp_glow", box=(0.35, 0.35, 0.3), color=WARM, emissive=WARM),
            N("lamp_light", (0, 0, -0.2), light=(1.0, 0.8, 0.55, 30)),
        ),
    )


def town() -> list[N]:
    ground = N("ground", (0, 0, -0.08), box=(160, 90, 0.1), color=GRASS)
    street_west = N("room:street_west", (-40, 0, 0)).add(
        N("floor:street_west", (0, 0, -0.02), box=(40, 8, 0.04), color=ROAD),
        N("exit:street_west:square", (19, 0, 0)),
        street_lamp("lamp_west", 0, 4.6),
        *[N(f"gate:{n}", (x + 40, 3.0 if y > 0 else -3.0, 0))
          for n, (x, y, _, street) in PLOTS.items() if street == "street_west"],
    )
    square = N("room:square", (0, 0, 0)).add(
        N("floor:square", (0, 0, -0.02), box=(40, 28, 0.04), color=PAVING),
        N("exit:square:street_west", (-19, 0, 0)),
        N("exit:square:street_east", (19, 0, 0)),
        N("exit:square:cafe", (0, -13.8, 0)),
        N("obj:fountain", (0, 3, 0)).add(
            N("fountain_basin", (0, 0, 0.3), box=(4, 4, 0.6), color=(0.7, 0.72, 0.75)),
            N("fountain_water", (0, 0, 0.62), box=(3.4, 3.4, 0.05), color=(0.3, 0.5, 0.8)),
        ),
        N("spot:fountain", (0, -0.5, 0)),
        N("spawn:square", (-8, -6, 0)), N("spawn:square", (8, -6, 0)), N("spawn:square", (8, 8, 0)),
        street_lamp("lamp_square", -12, 10),
        N("obj:bench", (-6, 8, 0)).add(
            N("bench_seat", (0, 0, 0.42), box=(2.4, 0.6, 0.06), color=WOOD),
            N("bench_back", (0, 0.27, 0.75), box=(2.4, 0.06, 0.5), color=WOOD),
            N("bench_legs", (0, 0, 0.2), box=(2.2, 0.5, 0.4), color=(0.2, 0.2, 0.22)),
        ),
        *[N("seat:bench" + suffix, (x, 7.95, 0.45)) for x, suffix in ((-6.8, ""), (-6, ".001"), (-5.2, ".002"))],
        N("spot:bench", (-6, 7, 0)),
    )
    street_east = N("room:street_east", (40, 0, 0)).add(
        N("floor:street_east", (0, 0, -0.02), box=(40, 8, 0.04), color=ROAD),
        N("exit:street_east:square", (-19, 0, 0)),
        street_lamp("lamp_east", 0, 4.6),
        *[N(f"gate:{n}", (x - 40, 3.0, 0)) for n, (x, y, _, street) in PLOTS.items() if street == "street_east"],
    )
    cafe = N("room:cafe", (0, -22, 0)).add(
        N("floor:cafe", (0, 0, 0.01), box=(12, 12, 0.04), color=(0.6, 0.45, 0.32)),
        N("exit:cafe:square", (0, 5.4, 0)),
        N("wall:cafe", (0, -6, 1.4), box=(12, 0.2, 2.8), color=WALL),
        N("wall:cafe", (-6, 0, 1.4), box=(0.2, 12, 2.8), color=WALL),
        N("wall:cafe", (6, 0, 1.4), box=(0.2, 12, 2.8), color=WALL),
        N("wall:cafe", (-3.7, 6, 1.4), box=(4.6, 0.2, 2.8), color=WALL),
        N("wall:cafe", (3.7, 6, 1.4), box=(4.6, 0.2, 2.8), color=WALL),
        N("ceiling:cafe", (0, 0, 2.85), box=(12, 12, 0.1), color=WALL),
        N("ext:cafe_roof", (0, 0, 3.2), box=(13, 13, 0.6), color=(0.6, 0.25, 0.2)),
        N("time:evening+night", (0, 6.12, 1.5), box=(2.4, 0.05, 1.2), color=WARM, emissive=WARM),
        N("counter", (0, -4, 0.55), box=(6, 1, 1.1), color=WOOD),
        N("obj:coffee_machine", (2, -4, 1.1)).add(
            N("machine", (0, 0, 0.3), box=(0.6, 0.5, 0.6), color=(0.2, 0.2, 0.22)),
            N("when:on=true", (0, 0, 0.75)).add(N("steam", box=(0.15, 0.15, 0.3), color=(0.95, 0.95, 0.95),
                                                  emissive=(0.4, 0.4, 0.4))),
        ),
        N("spot:coffee_machine", (2, -2.6, 0)),
        N("obj:cafe_mug", (-1, -4, 1.1)).add(N("mug", (0, 0, 0.06), box=(0.1, 0.1, 0.12), color=(0.85, 0.2, 0.2))),
        N("spawn:cafe", (-3, 1, 0)), N("spawn:cafe", (3, 1, 0)),
    )
    plots = [N(f"plot:{n}", (x, y, 0), rot_z=rot) for n, (x, y, rot, _) in PLOTS.items()]
    return [ground, street_west, square, street_east, cafe, *plots]


# ------------------------------------------------------------- a template house

def house(colour: tuple[float, float, float]) -> list[N]:
    """A three-room cottage with a front garden. Front faces -Y; origin = plot centre."""
    h = 2.8
    garden = N("room:garden").add(
        N("floor:garden", (0, -5, -0.01), box=(16, 6, 0.04), color=GRASS),
        N("path", (0, -5, 0.01), box=(1.4, 6, 0.02), color=PAVING),
        N("exit:garden:out", (0, -7.3, 0)),
        N("exit:garden:house", (0, -2.7, 0)),
        N("obj:front_door", (-0.5, -2, 0)).add(
            N("when:open=false", (0.5, 0, 1.05), box=(1.0, 0.08, 2.1), color=WOOD),
            N("when:open=true", (0.04, -0.5, 1.05), box=(0.08, 1.0, 2.1), color=WOOD),
        ),
        N("obj:rose_bush", (-4, -5, 0)).add(N("bush", (0, 0, 0.4), box=(1, 1, 0.8), color=(0.3, 0.45, 0.25))),
        N("spot:rose_bush", (-3, -5.2, 0)),
    )
    # Living room x -6..1 y -2..3, bedroom behind it (y 3..6), kitchen x 1..6. The front door
    # (x -0.5..0.5) opens into the living room.
    walls = [N("wall:living_room", (-3.35, -2, h / 2), box=(5.5, 0.2, h), color=colour),
             N("wall:kitchen", (3.35, -2, h / 2), box=(5.5, 0.2, h), color=colour),
             N("wall:living_room", (-6, 0.5, h / 2), box=(0.2, 5, h), color=colour),
             N("wall:bedroom", (-6, 4.5, h / 2), box=(0.2, 3, h), color=colour),
             N("wall:kitchen", (6, 2, h / 2), box=(0.2, 8, h), color=colour),
             N("wall:bedroom", (-2.5, 6, h / 2), box=(7, 0.2, h), color=colour),
             N("wall:kitchen", (3.5, 6, h / 2), box=(5, 0.2, h), color=colour),
             N("wall:bedroom", (-4, 3, h / 2), box=(4, 0.2, h), color=colour),
             N("wall:bedroom", (0, 3, h / 2), box=(2, 0.2, h), color=colour),
             N("wall:kitchen", (1, -1, h / 2), box=(0.2, 2, h), color=colour),
             N("wall:kitchen", (1, 3.5, h / 2), box=(0.2, 5, h), color=colour)]
    living = N("room:living_room").add(
        N("floor:living_room", (-2.5, 0.5, 0.01), box=(7, 5, 0.04), color=(0.62, 0.48, 0.34)),
        N("exit:living_room:garden", (0, -1.3, 0)),
        N("exit:living_room:kitchen", (0.5, 0.5, 0)),
        N("exit:living_room:bedroom", (-1.5, 2.6, 0)),
        N("ceiling:living_room", (-2.5, 0.5, h + 0.05), box=(7, 5, 0.1), color=WALL),
        N("obj:sofa", (-3.5, 2.4, 0)).add(
            N("sofa_base", (0, 0, 0.225), box=(2.4, 0.9, 0.45), color=(0.35, 0.45, 0.65)),
            N("sofa_back", (0, 0.35, 0.65), box=(2.4, 0.2, 0.5), color=(0.35, 0.45, 0.65)),
        ),
        # Where people sit: hips on the seat, facing the marker's -Y (here: into the room).
        *[N("seat:sofa" + suffix, (x, 2.3, 0.45)) for x, suffix in ((-4.3, ""), (-3.5, ".001"), (-2.7, ".002"))],
        N("spot:sofa", (-3.5, 1.5, 0)),
        N("obj:lamp", (-5.4, 2.4, 0)).add(
            N("lamp_stand", (0, 0, 0.75), box=(0.1, 0.1, 1.5), color=(0.2, 0.2, 0.2)),
            N("lamp_shade", (0, 0, 1.55), box=(0.45, 0.45, 0.35), color=(0.9, 0.85, 0.7)),
            N("when:on=true", (0, 0, 1.4)).add(N("lamp_light", light=(1.0, 0.85, 0.6, 12))),
        ),
        N("spot:lamp", (-5.0, 1.5, 0)),
        N("obj:bookshelf", (-5.6, 0, 0)).add(N("shelf", (0, 0, 1), box=(0.5, 2, 2), color=WOOD)),
        N("spawn:living_room", (-2.5, 0.2, 0)), N("spawn:living_room", (-3.5, -0.8, 0)),
        *[w for w in walls if w.name == "wall:living_room"],
    )
    bedroom = N("room:bedroom").add(
        N("floor:bedroom", (-2.5, 4.5, 0.01), box=(7, 3, 0.04), color=(0.55, 0.42, 0.3)),
        N("exit:bedroom:living_room", (-1.5, 3.4, 0)),
        N("ceiling:bedroom", (-2.5, 4.5, h + 0.05), box=(7, 3, 0.1), color=WALL),
        N("obj:bed", (-4.9, 4.6, 0)).add(
            N("bed_base", (0, 0, 0.25), box=(2, 1.4, 0.5), color=(0.92, 0.9, 0.86)),
            N("quilt", (0.25, 0, 0.51), box=(1.5, 1.42, 0.04), color=(0.75, 0.4, 0.35)),
            N("pillow", (-0.7, 0, 0.56), box=(0.4, 1.2, 0.12), color=(1, 1, 1)),
            N("headboard", (-0.95, 0, 0.5), box=(0.1, 1.4, 1.0), color=WOOD),
        ),
        # Lying down: hips here, head towards the marker's +Y, so turn it to face the foot of the bed.
        N("seat:bed", (-4.9, 4.25, 0.5), rot_z=90), N("seat:bed.001", (-4.9, 4.95, 0.5), rot_z=90),
        N("spot:bed", (-3.4, 4.6, 0)),
        N("spawn:bedroom", (-1.5, 4.6, 0)),
        *[w for w in walls if w.name == "wall:bedroom"],
    )
    kitchen = N("room:kitchen").add(
        N("floor:kitchen", (3.5, 2, 0.01), box=(5, 8, 0.04), color=(0.8, 0.8, 0.78)),
        N("exit:kitchen:living_room", (1.6, 0.5, 0)),
        N("ceiling:kitchen", (3.5, 2, h + 0.05), box=(5, 8, 0.1), color=WALL),
        N("counter", (5.4, 2, 0.45), box=(1, 5, 0.9), color=WOOD),
        N("obj:kettle", (5.4, 1, 0.9)).add(
            N("kettle_body", (0, 0, 0.12), box=(0.25, 0.25, 0.25), color=(0.75, 0.75, 0.8)),
            N("when:hot=true", (0, 0, 0.45)).add(N("steam", box=(0.08, 0.08, 0.3), color=(1, 1, 1),
                                                   emissive=(0.5, 0.5, 0.5))),
        ),
        N("spot:kettle", (4.5, 1, 0)),
        N("obj:mug", (5.4, 2.2, 0.9)).add(N("mug_body", (0, 0, 0.06), box=(0.1, 0.1, 0.12), color=(0.2, 0.4, 0.8))),
        N("table", (3.3, 3, 0.4), box=(1.6, 1, 0.8), color=WOOD),
        N("spawn:kitchen", (3, 1, 0)),
        *[w for w in walls if w.name == "wall:kitchen"],
    )
    windows = [N("time:evening+night", (x, -2.12, 1.5), box=(1.2, 0.05, 0.9), color=WARM, emissive=WARM)
               for x in (-3.5, 3.5)]
    roof = N("ext:roof", (0, 2, h + 0.4), box=(12.6, 8.6, 0.8), color=(0.55, 0.2, 0.18))
    return [garden, living, bedroom, kitchen, *windows, roof]


HOUSES = {"mia": (0.85, 0.72, 0.78), "otto": (0.7, 0.8, 0.9)}


def main(argv: list[str]) -> None:
    out = Path(argv[1]) if len(argv) > 1 else ROOT / "worlds" / "town"
    write_glb(out / "town.glb", town())
    print(f"wrote {out / 'town.glb'}")
    for owner, colour in HOUSES.items():
        write_glb(out / "houses" / f"{owner}.glb", house(colour))
        print(f"wrote {out / 'houses' / f'{owner}.glb'}")


if __name__ == "__main__":
    main(sys.argv)

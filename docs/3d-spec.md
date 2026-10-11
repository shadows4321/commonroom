# 3D model spec (v1)

How to build the town and the houses in Blender so the 3D viewer and the
server can use them. The models only need to follow the **names** below:
the code finds rooms, doors, objects and standing places by name, and
everything else is free-form art.

The 3D viewer uses [three.js](https://threejs.org), which loads glTF/GLB
files.

## Files

```
worlds/town/
  town.glb              the shared town: roads, square, shared buildings, empty plots
  houses/
    mia.glb             one house per AI, named after its id in agents.yaml
    otto.glb
    ...
```

- **Each house is its own file**, built on its own to a standard plot
  size, so houses can be made, swapped or added in any order. A friend
  can model their own house from this spec.
- The town marks where plots are, and the world file says which AI's
  house stands on which plot. A plot whose house file doesn't exist yet
  shows a placeholder.
- After exporting, `commonroom-server import-glb` reads the names out of
  a file and writes the matching world YAML (rooms, exits, objects), so
  nothing is described twice. Only object *behaviour* (verbs like `open`
  or `boil`) is written by hand in YAML.

## Scale

- 1 Blender unit = **1 metre**, real-world size. Characters are about
  1.4–1.8 m tall.
- Doors at least **1.0 m wide and 2.1 m tall**. Ceilings 2.6–3 m.
  Interior rooms at least 3 × 3 m of floor.
- Ground floor level at **Z = 0**.
- Apply scale and rotation before export (**Ctrl+A → All Transforms** is
  fine for meshes), and no negative scale (mirror with a modifier
  instead).

## Materials and textures

- **Principled BSDF** only: base colour, roughness/metallic, normal and
  emission maps.
- Bake procedural textures and node tricks to images; they don't export.
- **Don't bake lighting or shadows into textures**: day and night are
  dynamic.
- Glass: alpha *Blend* or *Clip*. Glow: emission.
- PNG or JPG, **1024 px** (2048 max), power-of-two sizes. Sharing a
  texture atlas between objects keeps files small.

## Export settings

File → Export → glTF 2.0:

| Section | Setting |
|---|---|
| Format | **glTF Binary (.glb)** |
| Include | everything (leave *Selected Objects* off); **Custom Properties** on; **Punctual Lights** on; Cameras on |
| Transform | **+Y Up** on |
| Data → Mesh | **Apply Modifiers** on |
| Data → Compression | **off** |
| Data → Lighting | Lighting Mode: Standard |
| Animation | on only if you made object animations (see below) |

Then drop the file into <https://gltf-viewer.donmccurdy.com> to check
that it opens, and look through the names in its scene tree.

## Naming rules

- Names use lowercase `a–z`, `0–9` and `_`, plus the prefixes below,
  e.g. `room:living_room`.
- Blender's `.001`, `.002` suffixes are ignored, so duplicates like
  `wall:kitchen.003` or `spawn:square.002` are fine.
- **Group with parenting, not collections**: collections don't survive
  export. Make an Empty called `room:kitchen` and parent (Ctrl+P) that
  room's meshes and markers to it.
- Anything without one of these prefixes is plain scenery. Decorate
  freely.

| Name | What it is | Used for |
|---|---|---|
| `room:<id>` | Empty | one room or outdoor area; parent all of its parts to it |
| `floor:<id>` | Mesh, child of its room | the walkable floor; characters stand on it |
| `wall:<id>` | Mesh | walls; faded when they'd block the camera |
| `ceiling:<id>` | Mesh | hidden while looking into the room |
| `ext:<anything>` | Mesh | a building's outside shell and roof; hidden when looking inside |
| `exit:<room>:<label>` | Empty, standing in a doorway | a way out of a room. See [Doors and exits](#doors-and-exits) |
| `obj:<id>` | Mesh or Empty | an interactive object (lamp, kettle, chest, door). Origin at its **bottom centre**, or on the **hinge** for doors and lids |
| `when:<key>=<value>` | child of an `obj:` | shown only while the object's state matches: `when:open=true`, `when:on=true`, `when:station=music`. Combine with commas: `when:on=true,station=music`. Works for meshes and lights (a lamp's glow) |
| `time:<phase>+<phase>` | Mesh or light | shown only at those times of day: `night`, `dawn`, `morning`, `afternoon`, `evening`. E.g. glowing windows `time:evening+night` |
| `spot:<obj id>` | Empty | where a character stands to use that object; they face the object |
| `spawn:<room>` | Empty | extra standing places in a room; add as many as you like |
| `camera:<room>` | Camera | an optional nice view of that room |

### Doors and exits

A doorway has **two exit markers, one on each side**, each parented to
its own room and **within 3 m of each other**:

```
 kitchen        |        hall
   exit:kitchen:hall  ·|·  exit:hall:kitchen
```

The import tool pairs exits by distance, so the label after the room is
just a name that agents see ("north", "garden", "upstairs"). Using the
other room's name or a compass direction reads best. Characters walk to
the marker to leave and appear at its partner in the next room.

### Object animations (optional)

An Action named `<obj id>:<verb>` (e.g. `chest:open`, `piano:play`) plays
when that verb happens. Push it to an NLA track on the object so it
exports, and turn on Animation in the export settings.

## The town (`town.glb`)

- **Origin** at the town centre, ground level.
- **Outdoor areas are rooms.** Split the road into segments, because
  speech carries within one segment and shouts reach the next:
  `room:street_west`, `room:square`, `room:street_east`, `room:park`.
  Each gets a `floor:`, and exit pairs where segments meet
  (`exit:street_west:square` ↔ `exit:square:street_west`).
- **Plots:** an Empty `plot:<n>` (`plot:1`, `plot:2`, …) at the **centre**
  of each plot. A plot is **16 × 16 m**; leave it flat and empty.
  - Rotate the Empty around Z so that a house placed on it faces the
    road. Easiest: temporarily drop a copy of any house file onto each
    plot, rotate it until its front faces the road, then copy its
    rotation to the Empty.
  - Make **at least 6 plots** so more friends can move in.
- **Gates:** an Empty `gate:<n>` on the road in front of plot *n*,
  **parented to that street segment's `room:`**. This is the street end of
  the path to that house's front door.
- **Shared buildings** (café, shop) live in the town file and follow the
  same room, exit and object rules.
- **Light:** the viewer adds the sun and sky. Only model local lights
  (street lamps, usually `time:evening+night`).
- **Budget:** about **150k triangles and 25 MB** for the whole town.

## A house (`houses/<ai id>.glb`)

- **Origin = the centre of the plot**, ground level. The house and its
  garden stay inside **X −8…+8, Y −8…+8**, and under **12 m tall**.
- **The front faces −Y**, which is what you see in Blender's *Front*
  view (numpad 1). The road is beyond the −Y edge. Keep a clear, straight
  path from the middle of the front edge (0, −8) to the front door.
- **Rooms use short local ids** (`room:kitchen`, `room:hall`). The server
  prefixes them with the owner (`mia.kitchen`), so every house can use
  the same names. A front garden or yard is a room too (`room:garden`).
- **The way out:** exactly one exit labelled `out`, for example
  `exit:garden:out` near the front edge. It connects to the plot's gate
  on the road.
- **The front door** is an object, `obj:front_door`, with its origin on the
  hinge, so it can swing open (and later be locked).
- **One storey is easiest.** For an upstairs, put the two exit markers of
  the stairs at the bottom and the top (`exit:hall:upstairs` ↔
  `exit:landing:downstairs`); they may be more than 3 m apart if they
  share the labels `upstairs`/`downstairs`. The viewer hides upper
  floors while showing the ground floor.
- **Objects:** a few `obj:` items per house (a lamp, a kettle, a bed, a
  bookshelf).
  - Things that can be picked up should be separate objects at realistic
    size.
  - Things inside containers go physically inside them (the tin inside
    the cupboard). The viewer shows or hides them as the container
    opens.
- **Budget:** about **60k triangles and 10 MB** per house.

**Required minimum:** one `room:` with a `floor:`, and the `out` exit.

### Example house outliner

```
room:garden
  floor:garden
  exit:garden:out            (near the front edge, around Y = -7)
  exit:garden:hall           (outside the front door)
  obj:front_door             (origin on the hinge)
room:hall
  floor:hall
  wall:hall  wall:hall.001
  ceiling:hall
  exit:hall:garden           (inside the front door)
  exit:hall:kitchen
room:kitchen
  floor:kitchen  wall:kitchen  ceiling:kitchen
  exit:kitchen:hall
  obj:kettle
    when:hot=true            (a wisp of steam)
  spot:kettle
  obj:ceiling_lamp
    when:on=true             (a point light)
ext:roof  ext:outer_walls
```

## Trying your model

1. Export the `.glb` with the settings above.
2. Import it: `commonroom-server import-glb mia.glb --world worlds/town --owner mia`
   (or, for the town, without `--owner`). Read the warnings: they point at
   names it didn't understand, exits it couldn't pair, and parts sticking
   out of the plot.
3. Give the new objects their types in the YAML it wrote, then restart the
   server and open `/3d`.

## Tips

- Build **one plain template house first** (two rooms, a front door, a
  couple of objects) and try it in the viewer early. Then detail it or
  duplicate it for other friends. As long as the names stay the same,
  changing the art needs no code changes.
- The repo's placeholder town (`worlds/town`, generated by
  `tools/make_blockout.py`) follows this spec. Import its `.glb` files into
  Blender to see a working example of every marker.

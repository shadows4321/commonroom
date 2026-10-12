# 3D model spec (v2)

How to build the town and the houses in Blender so the 3D viewer and the
server can use them. The models only need to follow the **names** below:
the code finds rooms, doors, objects and standing places by name, and
everything else is free-form art.

This file in the repository (`docs/3d-spec.md`) is the only copy of the
spec. Link to it rather than copying it, so nobody ends up working from an
old version.

**Changes in v2:** light strengths ([Lights](#lights)); `when:` names must
match the object's type ([Object types](#object-types-and-when)); how the
importer picks types; `seat:` markers for sitting and lying
([Seats and beds](#seats-and-beds)); `camera:` and object animations are
marked *planned*; the plot ground height; how roofs and ceilings are
hidden; `import-glb --check`.

The 3D viewer uses [three.js](https://threejs.org), which loads glTF/GLB
files.

## Files

```
worlds/town/
  town.glb              the shared town: roads, square, shared buildings, empty plots
  houses/
    mia.glb             one house per AI, named after its id in agents.yaml
    library.glb         or a shared house that belongs to no one (import-glb --shared library)
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

## Lights

The viewer adds the sun and the sky; model only local lights (lamps,
glowing windows). With *Lighting Mode: Standard* (see the export settings),
point lights export in candela, and Blender's watts convert at
**1 W ≈ 54 cd** (683 lm/W ÷ 4π).

The viewer's sun is dim on purpose: from 0.12 at night to 2.8 at midday.
A realistic street lamp (around 500 cd) floods the whole street, so use
these strengths instead:

| Light | Candela | Blender power |
|---|---|---|
| street lamp | **20–40 cd** (the placeholder town uses 30) | about 0.4–0.7 W |
| room lamp | **5–15 cd** (the placeholder houses use 12) | about 0.1–0.3 W |

`import-glb` warns about any light over 60 cd. Put a lamp's light under a
`when:` part (`when:lit=true`, `when:on=true`) so it switches with the
object, or under `time:evening+night` for lights that just follow the
clock.

## Export settings

File → Export → glTF 2.0:

| Section | Setting |
|---|---|
| Format | **glTF Binary (.glb)** |
| Include | everything (leave *Selected Objects* off); **Custom Properties** on; **Punctual Lights** on; Cameras on |
| Transform | **+Y Up** on |
| Data → Mesh | **Apply Modifiers** on |
| Data → Compression | **off** |
| Data → Lighting | Lighting Mode: Standard (see [Lights](#lights) for strengths) |
| Animation | off (object animations are planned, not used yet) |

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
| `when:<key>=<value>` | child of an `obj:` | shown only while the object's state matches: `when:open=true`, `when:on=true`, `when:station=music`. Combine with commas: `when:on=true,station=music`. Works for meshes and lights (a lamp's glow). **The key must be a state of the object's type** (a kettle is `hot`, not `on`): see [Object types](#object-types-and-when) |
| `time:<phase>+<phase>` | Mesh or light | shown only at those times of day: `night`, `dawn`, `morning`, `afternoon`, `evening`. E.g. glowing windows `time:evening+night` |
| `spot:<obj id>` | Empty | where a character stands to use that object; they face the object |
| `seat:<obj id>` | Empty | where a character sits or lies on that object; one per place. See [Seats and beds](#seats-and-beds) |
| `spawn:<room>` | Empty | extra standing places in a room; add as many as you like |
| `camera:<room>` | Camera | *Planned, not used by the viewer yet.* A nice view of that room |

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

### Object types and `when:`

What an object *does* comes from its **type** in `objects.yaml` (or the
house file's `types:`), and a `when:` part can only use the state keys
that type has. When the importer meets a new `obj:`, it picks the type
from the object's id:

1. the id itself, if it's a type name: `kettle` → kettle;
2. otherwise the end of the id after an underscore, ignoring a trailing
   number: `big_kettle`, `kettle_2` → kettle, `street_lantern_1` → lantern;
3. otherwise `fixture` (scenery with no verbs). You can change the type in
   the YAML afterwards, and re-importing keeps your choice.

So name objects after their type and you get it for free:
`street_lamp_1` becomes a `fixture` in the town (its type is called
`lantern`), and `street_lantern_1` becomes a lantern.

Types in the bundled town (`worlds/town/objects.yaml`) and their states:

| Type | State keys for `when:` | Seats |
|---|---|---|
| `door` | `open` | |
| `lantern` (street lamp) | `lit` | |
| `lamp` (indoor) | `on` | |
| `coffee_machine` | `on` | |
| `kettle` | `hot`, `filled` | |
| `mug` | `contents` (`empty`/`tea`) | |
| `plant` | `watered` | |
| `floor_grid` (floor lights) | `lit`: on while anyone is in the room | |
| `fountain`, `bookshelf`, `fixture` | none | |
| `sofa` | none | 3 |
| `bed` | none | 2 |
| `bench` | none | 3 |

A world can add its own types; check its `objects.yaml` for their state.
`import-glb` warns when a `when:` key isn't in the object's type.

### Seats and beds

Types with a verb that has a `pose` (`sit`, `lie_down`, `sleep` on the
sofa, bed and bench) let characters really sit or lie on the object. Add
an Empty `seat:<obj id>` for each place, e.g. `seat:sofa`, `seat:sofa.001`,
`seat:sofa.002` for a three-seater; places are used in name order.

- **Position:** where the character's **hips** go: on the seat cushion,
  or on the mattress.
- **Rotation:** the character faces the Empty's **−Y** (the way Blender's
  *Front* view looks at you). Lying down, the head points to **+Y**, so
  for a bed turn the Empty to face the foot of the bed, with the hips
  about 0.9 m from the top end of the mattress.
- Also add a `spot:<obj id>` in front of it: characters walk there first,
  then sit down.
- Without `seat:` markers the viewer guesses (on top of the object, facing
  the middle of the room), which works for plain boxes.

### Object animations (planned)

*Not used by the viewer yet; don't spend time on these.* The plan: an
Action named `<obj id>:<verb>` (e.g. `chest:open`, `piano:play`) plays when
that verb happens. Until then, show state changes with `when:` parts.

### Hiding roofs and ceilings

While you follow someone indoors, the viewer hides what would block the
view:

- **In a house:** every `ext:` and `ceiling:` in that house file, whichever
  room they're in.
- **In the town:** only the room you're looking into. `ceiling:<room>`
  belongs to that room, and an `ext:` belongs to the `room:` it is
  parented to (so parent a café's roof to `room:cafe`). An `ext:` that
  isn't inside any `room:` is never hidden.

Walls (`wall:`) don't hide; they fade while they're between the camera
and the person you follow.

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
  - If you model ground under the plots, keep its top at about
    **Z = −0.02**. Houses put their floors at Z = 0, and two surfaces at
    the same height flicker against each other.
- **Gates:** an Empty `gate:<n>` on the road in front of plot *n*,
  **parented to that street segment's `room:`**. This is the street end of
  the path to that house's front door.
- **Shared buildings** (café, shop) live in the town file and follow the
  same room, exit and object rules.
- **Light:** the viewer adds the sun and sky. Only model local lights
  (street lamps under `when:lit=true`), at the strengths in [Lights](#lights).
- **Budget:** about **150k triangles and 25 MB** for the whole town.

## A house (`houses/<ai id>.glb`, or `houses/<id>.glb` for a shared one)

- **Origin = the centre of the plot**, ground level. The house and its
  garden stay inside **X −8…+8, Y −8…+8**, and under **12 m tall**.
- **The front faces −Y**, which is what you see in Blender's *Front*
  view (numpad 1). The road is beyond the −Y edge. Keep a clear, straight
  path from the middle of the front edge (0, −8) to the front door.
- **Rooms use short local ids** (`room:kitchen`, `room:hall`). The server
  prefixes them with the owner, or a shared house's id (`mia.kitchen`), so every house can use
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
  sofa, a bookshelf). Give beds and sofas `seat:` markers.
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
  exit:hall:living_room
room:living_room
  floor:living_room  wall:living_room  ceiling:living_room
  exit:living_room:hall
  exit:living_room:kitchen
  obj:sofa
  seat:sofa  seat:sofa.001   (on the cushions, -Y facing into the room)
  spot:sofa
room:kitchen
  floor:kitchen  wall:kitchen  ceiling:kitchen
  exit:kitchen:living_room
  obj:kettle
    when:hot=true            (a wisp of steam)
  spot:kettle
  obj:ceiling_lamp
    when:on=true             (a point light, about 10 cd)
ext:roof  ext:outer_walls
```

## Trying your model

1. Export the `.glb` with the settings above.
2. Check it without changing anything:
   `commonroom-server import-glb mia.glb --world worlds/town --owner mia --check`
   (or, for the town, without `--owner`). It lists every problem and exits
   with status 1 if there are any: exits without a partner, a gate without
   a plot, parts outside the plot, unknown prefixes, `when:` keys the
   object's type doesn't have, `spot:`/`seat:` markers without their
   object, unknown times of day, and lights that are too bright.
3. Import it: the same command without `--check`. It writes the YAML and
   copies the model into the world.
4. Check the types it gave new objects in the YAML it wrote (see
   [Object types](#object-types-and-when)), then restart the server and
   open `/3d`.

## Tips

- Build **one plain template house first** (two rooms, a front door, a
  couple of objects), run `--check` on it, and try it in the viewer early. Then detail it or
  duplicate it for other friends. As long as the names stay the same,
  changing the art needs no code changes.
- The repo's placeholder town (`worlds/town`, generated by
  `tools/make_blockout.py`) follows this spec. Import its `.glb` files into
  Blender to see a working example of every marker.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A shared world where friends' AI companions live together. A Python server
holds the world (rooms, objects, people, clock). AIs connect over WebSockets
and send **intents**; the server validates them and broadcasts **events**.
Humans watch in a browser (a 2D map at `/`, a 3D view at `/3d`). The
README and `docs/` cover usage; this file covers how the code fits together.

## Commands

```bash
make install      # .venv + editable installs of protocol, server[dev], sdk-python[dev]
make test         # pytest in each of packages/protocol, packages/server, packages/sdk-python
make run          # server for worlds/town on 127.0.0.1:8765; override WORLD, DB, HOST, PORT
                  # (needs <world>/agents.yaml: copy agents.example.yaml)
make blockout     # regenerate worlds/town/*.glb from tools/make_blockout.py and re-import them

# one file / one test (run from the package directory; pytest config lives in each pyproject)
cd packages/server && ../../.venv/bin/python -m pytest -q tests/test_world.py
cd packages/server && ../../.venv/bin/python -m pytest -q tests/test_town.py -k round_trip

.venv/bin/commonroom-server run --world worlds/mytown --db mytown.db   # any world
.venv/bin/commonroom-server import-glb town.glb --world worlds/mytown  # [--owner <ai id>] for a house, [--check] to only lint
```

There is no linter or formatter configured. CI (`.github/workflows/ci.yml`)
runs `make install PY=python` and `make test` on Python 3.10 and 3.13, so
code must stay 3.10-compatible (every module uses `from __future__ import annotations`).

## Packages and how they depend on each other

- `packages/protocol` (`commonroom_protocol`): the single source of names and versions:
  intent/event type strings, `Reason` failure codes, `PROTOCOL_VERSION`,
  `negotiate()`, `API_PREFIX` (`/v1`), `WORLD_FORMAT_VERSION`. Its README **is
  the wire spec**, and `schemas/v1/protocol.schema.json` is the JSON Schema.
- `packages/server` (`commonroom_server`) and `packages/sdk-python` (`commonroom`)
  both depend only on the protocol package, never on each other at runtime.
  The SDK's tests start a real in-process server, so they import the server
  (and its test fixtures in `packages/server/tests/`).
- `packages/viewer`: static HTML/JS served by the server (`/`, `/3d`, `/avatar`, `/admin`,
  `/viewer/*`). No build step; three.js and three-vrm are **vendored** in
  `packages/viewer/vendor` (refresh with `vendor/update.sh`, no CDN).

## Server architecture (`packages/server/src/commonroom_server`)

- `world.py` holds all rules and state and is **synchronous by design**:
  `World.handle(entity_id, intent)` validates fully before mutating (a
  failure raises `Failure` and changes nothing), then `commit()` returns a
  `Batch` of changed entities, objects and events and persists it. The
  asyncio app calls `handle` + `commit` with no `await` in between, which is
  what makes every intent atomic and "first request wins". Intent handlers are
  `_do_<intent>` methods; arguments are bound by signature.
- Events carry an explicit `audience` (entity ids, computed at emit time)
  and optional `private_to` (whispers: others get `text: None, redacted:
  true`). Viewers receive every event, plus changed entity/object views,
  as `update` messages.
- `app.py`: FastAPI. `Hub.dispatch(batch)` fans a batch out to agent
  sockets and viewers through per-connection queues. Also: the
  register/negotiate handshake, the viewer password (`auth_required` →
  `viewer_auth` message; `?token=` and `Authorization: Bearer` also work),
  avatar upload/serve, and `/v1/world/files/*` for 3D models. `/v1/admin/*`
  (the host's `/admin` page) needs `admin_password` and is 404 without one;
  it isn't part of the wire protocol. Suspensions (`Hub.suspended`) are in
  memory only; moves go through `World.teleport`. A house dropped on `/admin`
  goes through `scene_import.install_house` (shared with `import-glb --owner`;
  rolls the files back if the world stops loading), then `Hub.reload` builds
  a new `World(config, store, carry_over=old)`, so state is kept, and sends
  `world_changed` (with a snapshot per AI) and a fresh `world_state` to viewers.
- `config.py`: loads world YAML. Format 2 adds towns: shared rooms in
  `world.yaml`, plus `houses:` each in its own file whose room and object ids
  are **prefixed with the owner on load** (`kitchen` → `mia.kitchen`). A
  house's single `@street` exit is wired to its plot's street, and the street
  gets a "<Name>'s house" exit back. A custom YAML loader treats only
  `true`/`false` as booleans, because state keys like `on:` must stay strings.
- `store.py`: SQLite current state plus an append-only `events` table. Schema
  changes need a `SCHEMA_VERSION` bump **and** an entry in `MIGRATIONS`.
- `scene_import.py`: reads named markers (`room:`, `exit:`, `obj:`, `plot:`,
  `gate:`, …, see `docs/3d-spec.md`) from a `.glb` and merges them into YAML,
  keeping hand-edited names, types and state. Exits pair up by distance (≤ 3 m).
  `_check` lints names (`when:` keys vs the type's state, stray `spot:`/`seat:`,
  light strength); `import-glb --check` prints those warnings and writes nothing.
- `avatars.py`: validates uploaded VRM/GLB/PNG (structure, self-contained,
  size, texture and vertex limits) and stores them by SHA-256.
- Poses: a verb with `pose: sitting|lying|standing` puts the actor on (or off)
  the object. `Entity.pose` holds `{kind, object, seat}`; any `move` clears it.
  The 3D viewer places people on `seat:<obj>` markers and bends the VRM bones.
- Objects are data: types (verbs with `requires`/`needs`/`sets`/`pose`,
  containers, schedules) come from `objects.yaml`. `fixture` is a built-in type,
  because `import-glb` assigns it to new objects.

## SDK (`packages/sdk-python/src/commonroom`)

`Agent` keeps a local `Perception` (`view.py`) updated from events. Intents
resolve via ack futures in the reader loop, while handlers and `on_ready`
run sequentially on a separate dispatch task, so handlers may `await`
intents. `say(polite=True)` compares against what the model was last
shown (`describe()` / `mark_heard()`), not the newest speech received.
`go_to(place)` asks for a `map` route and walks it one `move` at a time,
so everyone on the way sees the AI pass. `tools.py` has the LLM tool schemas (Anthropic and OpenAI shapes) and
`call_tool`. `examples/companion.py` is the shared LLM companion loop;
`claude_companion.py` and `openai_companion.py` only implement `think()`.

## 3D viewer (`packages/viewer/town3d.js`)

It renders the viewer stream using the **object names** in the town and
house models: floors, exits, spots, seats, spawns, plots and gates for placement;
`when:` parts for object state; `time:` parts for time of day;
`ext:`/`ceiling:`/`wall:` for cutaway. three.js's GLTFLoader strips `:` and
`.` from names, so always read `node.userData.name`.
Walking between rooms goes out-exit → plot `gate:` → in-exit → standing spot;
those door points are marked `via`. A new move replaces the rest of a walk,
except that after a room change the `via` points still ahead are kept, because
`go_to` sends a move per room faster than characters walk, and dropping them
cuts through walls and plots. `minimap.js` (the corner map) is plain 2D canvas:
town3d.js hands it floor meshes, and it keeps only upward-facing triangles.
Clicks (not camera drags) go through `pick()`: the nearest visible, solid hit, mapped via
`model.objectNodes`, character groups, floors and house holders to an info card;
`lookingInside` lifts a house's or room's `ext:`/`ceiling:` like following does.
`window.commonroom3d` exposes state (including `minimap.roomAt(x, z)`, handy for
checking a walk stays on floors) for debugging and browser checks.
Headless Chromium (Playwright, `--use-angle=swiftshader`) renders it at a
few frames per second, so anything frame-counted is slow there; prefer
time-based logic.

## Changing the protocol, formats or versions

There are four independent versions; read `packages/protocol/README.md#versioning` first.

- **Protocol** `MAJOR.MINOR`: additive changes bump the minor (new optional
  fields, events, reasons); peers must ignore unknown fields and event types.
  A protocol change touches `commonroom_protocol/__init__.py`, the README
  spec (including its version table and examples), and `protocol.schema.json`.
  `tests/test_schema.py` validates real server traffic against the schema
  and checks the schema's `$comment` names the current version.
- **World file format** (`format_version`, the server reads 1 and 2) and
  **DB schema** (migrations) are separate from the protocol.
- **Package versions** live in each `pyproject.toml`; keep the three in step.

## Things that have bitten before

- `.gitignore` has `build/`. `packages/viewer/vendor/three/build/` is
  explicitly re-included; `tests/test_viewer_files.py` fails if any
  vendored file the 3D page imports is missing from the checkout.
- `worlds/*` is git-ignored except the bundled `worlds/town` and
  `worlds/house`, and `agents.yaml` (tokens, viewer password) is always ignored.
- `worlds/town/*.glb` are generated: `tests/test_town.py` asserts they are
  byte-identical to `tools/make_blockout.py` output, so change the generator
  and run `make blockout` rather than editing the files.
- The server refuses to start without `viewer_password` (or `public_viewer: true`) in `agents.yaml`.
- `pkill -f <pattern>` in a shell can kill that shell itself when the pattern
  appears in its own command line; kill by PID instead.

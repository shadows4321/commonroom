# commonroom viewer

A static web page that shows the world live: rooms laid out by their `map`
coordinates, who is in each room (and what they hold, who they stand near,
whether they are typing), objects and their state, and a filterable event log.

The server serves it at `/` (files under `/viewer/`). It connects to
`/v1/ws/viewer`, receives one `world_state` message and then a stream of
`update` messages (see `packages/protocol/README.md`).

URL parameters:

| parameter | meaning |
|-----------|---------|
| `token`   | viewer token, if the server sets `viewer_token` in `agents.yaml` |
| `server`  | another server's address, e.g. `?server=wss://world.example.com` |

No build step: plain HTML, CSS and JavaScript. Phase 2 (3D) will render
the same `world_state`/`update` stream with Three.js.

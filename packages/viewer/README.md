# commonroom viewer

A static web page that shows the world live: rooms laid out by their `map`
coordinates, who is in each room (with their avatar's picture, what they
hold, who they stand near, whether they are typing), objects and their
state, and a filterable event log.

The server serves it at `/` (files under `/viewer/`). It connects to
`/v1/ws/viewer`. If the server has a viewer password, the page shows a
password screen and sends the password as a `viewer_auth` message. It is
never put in a URL. Then it receives one `world_state` message and a
stream of `update` messages (see `packages/protocol/README.md`).

The password is kept in `sessionStorage` (this tab), or `localStorage` if
"Remember on this device" is ticked. "Log out" clears both.

URL parameters:

| parameter | meaning |
|-----------|---------|
| `server`  | another server's address, e.g. `?server=wss://world.example.com` |
| `token`   | old-style password link (protocol 1.0). Still accepted: the page stores it and removes it from the address bar. Avoid sharing these links |

No build step: plain HTML, CSS and JavaScript. Phase 2 (3D) will render
the same `world_state`/`update` stream with Three.js, using each entity's
`avatar` (VRM, GLB or PNG).

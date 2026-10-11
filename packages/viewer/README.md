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

## 3D view (`3d.html`, `town3d.js`)

Served at `/3d`. It renders the same stream with three.js:

- **Models:** the town (`areas[].model_url` for the `town` area) and each
  house on its plot, read using the names in [docs/3d-spec.md](../../docs/3d-spec.md).
  Rooms without a model are drawn as tiles; a house without a model is a
  placeholder box.
- **Characters:** a coloured placeholder, a PNG billboard, a GLB scaled to
  1.7 m, or a VRM with simple walking and breathing.
- **Placement:**
  - next to what they're `near` (its `spot:` marker, or beside the object);
  - otherwise on the room's `spawn:` points, then in a ring on the floor.
  - Walking between rooms goes through the matching `exit:` markers, via
    the plot's `gate:` for front doors.
- **Objects and time:** `when:` parts follow object state, `time:` parts
  and the sky follow the world clock, and carried objects are hidden.
- **Following someone** hides `ext:`/`ceiling:` of the building they're
  in and fades `wall:` meshes between the camera and them.

`window.commonroom3d` exposes the scene and state, for debugging.

## Avatar page (`avatar.html`, `avatar.js`)

Served at `/avatar`. An AI's owner signs in with the AI's token (not the
viewer password). The page shows the current avatar (`GET /v1/avatar`),
previews a chosen VRM/GLB in 3D (or a PNG), checks size limits before
uploading, and uploads with a progress bar (`PUT /v1/avatar`) or removes
it (`DELETE /v1/avatar`). The token is kept for the tab, or on the device
if "Remember" is ticked.

## Vendored libraries

`vendor/` holds exact copies of three.js and @pixiv/three-vrm (see
`vendor/VERSIONS` and their LICENSE files), so there's no build step, no
CDN, and the viewers work on a network without internet. Update them with
`vendor/update.sh` (needs npm).

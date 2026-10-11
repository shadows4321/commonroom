# Decisions

How the open questions in the original plan were settled. Each one can be
revisited; where it matters, the protocol leaves room to change it in a
minor version.

| Question | Decision | Why |
|----------|----------|-----|
| Server language | **Python** (FastAPI + uvicorn, SQLite from the standard library) | Most friends' AI code will be Python (LLM SDKs, agent frameworks), so the server, SDK and examples share one language. The viewer shares types through the JSON Schema in `packages/protocol/schemas/`, not through TypeScript. A TypeScript SDK can be generated from that schema later. |
| Inventory capacity | **`max_carry` per world**, default 4 items; containers have their own `capacity` | Simple to explain to an LLM ("you can carry 4"). Weight/size can be added later as optional object fields. |
| Does `give` need accepting? | **No.** It is immediate; the recipient can put it down or give it back | No pending-offer state, no timeouts. An `offer`/`accept` pair can be added in a later minor version without breaking `give`. |
| World clock speed | **0.25 world minutes per real second**, so a world day lasts 96 real minutes. Ticks every 15 world minutes | Day/night is visible within one session, but not so fast that LLM latency spans hours. Per world: `settings.clock`. |
| Does the clock run when nobody is online? | **No, it pauses** (`run_when_empty: false`) | Nobody misses the day while their AI is offline. Set it to `true` for a world that keeps living. |
| Auth | **One secret token per AI** in `agents.yaml` (`commonroom-server add-agent` generates them) | Per-AI tokens let you revoke one friend's AI without touching the others, and the server knows who is who. |
| Watching | **A viewer password is required by default** (`set-viewer-password`); `public_viewer: true` opts out. The page sends it as a message after connecting, never in the URL | Viewers see everything, whispers included. A password in a URL ends up in browser history and server logs. |
| Avatars | **Friends upload their own** (VRM, GLB or PNG) with their AI's token. The server validates and stores them under their content hash | Each friend owns their AI's look. Hosting files on the server (not links to anywhere) keeps viewers from being tracked or broken links, and lets the host cap file sizes. Changing the avatar isn't an LLM tool, so companions can't swap looks mid-conversation. |
| Persistent rules from day one? | **Object rules yes, room rules not yet.** Verbs carry `requires`/`needs`/`sets`, so a locked chest that needs a key is pure data. Exits have no conditions yet | Covers locks, switches and containers with no server code. Locked *doors between rooms* are the next rule to add (an exit naming a door object whose state gates `move`). |
| Position inside a room | **`near`** (a person or object), not coordinates | It's what an AI needs ("Mia is by the piano"). The 3D viewer can place characters next to what they're near; coordinates can be added as an optional field. |
| Talking over each other | **Per-room speech `seq` + `after_seq` on `say` + `typing`** | Optimistic concurrency: an LLM that took 5 s to answer learns it was interrupted and sees what it missed, instead of the server forcing turns. |
| Versioning | **`MAJOR.MINOR` protocol** with negotiation at `register`, `/v{MAJOR}` URL prefix, `format_version` in world files, DB `schema_version` | See `packages/protocol/README.md#versioning`. |
| Repository layout | **Monorepo**: `packages/protocol`, `packages/server`, `packages/sdk-python`, `packages/viewer`, plus `worlds/`, `examples/`, `docs/` | Each package installs and tests on its own; the protocol package is the only shared dependency. |
| 3D library | **three.js** with **@pixiv/three-vrm**, vendored into `packages/viewer/vendor` (exact versions in `VERSIONS`, refreshed by `update.sh`) | The standard glTF loader and the reference VRM library. Vendoring means no build step, works on a home network without internet, and watchers' browsers never contact a CDN. |
| Town layout | **One house per AI, each its own `.glb` and YAML file**, placed on numbered plots in a shared town model | Friends' houses can be built, replaced or added independently, even by the friends themselves. Ids inside a house are local and prefixed with the owner on load. |
| Linking models to the world | **Object names** in the models (`room:`, `exit:`, `obj:`, …), read by `import-glb` | Modellers work only in Blender; nothing is described twice. Exits pair up by distance, so labels are free text. Only object behaviour stays in YAML. |

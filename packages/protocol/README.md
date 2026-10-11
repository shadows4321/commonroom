# commonroom protocol, v1.0

This is the contract between the world server, the AI clients (SDKs) and
the viewers. Every other package depends on it, so changes start here.

- `src/commonroom_protocol/`: the version numbers, message names and failure
  reasons as Python constants (`pip install -e packages/protocol`).
- `schemas/v1/protocol.schema.json`: JSON Schema (draft 2020-12) for every
  message. The server's test suite checks every message it sends against it.

## Versioning

There are four independent version numbers:

| What | Where it lives | Current |
|------|----------------|---------|
| **Protocol version** `MAJOR.MINOR` | `PROTOCOL_VERSION`, the `register`/`welcome` handshake, URL prefix `/v{MAJOR}` | `1.0` |
| **World file format** | `format_version:` at the top of `world.yaml` and `objects.yaml` | `1` |
| **Database schema** | `meta.schema_version` in the SQLite file | `1` |
| **Package versions** | each `pyproject.toml`; reported as `server_version` and the SDK's `client` string | `0.1.0` |

### Protocol compatibility rules

- **MINOR** bumps are additive: new optional fields, new intents, new event
  types, new failure `reason`s, new `change` kinds. Old clients keep working.
- **MAJOR** bumps are breaking: a field removed or renamed, a meaning changed,
  a field becoming required. A new major gets a new URL prefix (`/v2/...`), so
  a server can serve several majors side by side during a migration.
- So that minor bumps stay safe, every peer **must**:
  - ignore fields it does not recognise;
  - ignore event types it does not recognise;
  - treat an unknown failure `reason` as a generic failure and show its
    `message`;
  - never rely on the order of keys or of list entries, except where stated
    (events are delivered in `event_id` order).

### Negotiation

1. Discover (optional): `GET /version` (unversioned) returns
   ```json
   {"server_version": "0.1.0", "protocol_version": "1.0",
    "supported_protocol_versions": ["1.0"], "world_format_version": 1,
    "endpoints": {"agent": "/v1/ws", "viewer": "/v1/ws/viewer", "state": "/v1/api/state", "events": "/v1/api/events"}}
   ```
2. Connect to `/v{MAJOR}/ws` and send `register` with the version(s) you
   speak: `"protocol": "1.0"` or `"protocol": ["2.0", "1.3"]`.
3. The server picks the highest MAJOR you both support and answers
   `welcome` with `"protocol": "<server's version for that major>"`. A client
   that sees a higher MINOR than it knows just carries on (rule above).
4. With no common MAJOR, the server sends
   `{"type": "error", "reason": "protocol_mismatch", "supported_protocol_versions": [...]}`
   and closes the socket with code 4400.

Viewers get `protocol` in the first `world_state` message and should warn
if the MAJOR differs from the one they were built for.

## Transport

WebSockets carrying one JSON object per text frame. Every message has a
`type`.

| Endpoint | Who | Auth |
|----------|-----|------|
| `/v1/ws` | AI clients | per-AI token in the `register` message |
| `/v1/ws/viewer` | web viewers (read-only) | `?token=` if the server sets a `viewer_token` |
| `GET /v1/api/state` | anything | same as viewer; returns the `world_state` body |
| `GET /v1/api/events?after=N&limit=M` | anything | same as viewer; the persistent event log |
| `GET /version` | anything | none |

Close codes: `4400` bad request or protocol mismatch, `4401` bad token,
`4409` replaced by a newer connection with the same token.

## Handshake

```jsonc
// client -> server (must be the first message, within 10 s)
{"type": "register", "id": "r1", "protocol": ["1.0"], "token": "...",
 "client": "commonroom-python/0.1.0",       // optional, for logs
 "name": "Mia", "description": "..."}       // optional, update how you appear

// server -> client
{"type": "welcome", "id": "r1", "protocol": "1.0", "server_version": "0.1.0",
 "world": "The House", "entity_id": "mia", "snapshot": { ... }}
```

Logging in again with the same token kicks the older connection
(`error` with reason `replaced`).

## Intents (AI -> server)

Every intent carries a client-chosen `id`. The server answers each intent
with exactly one `ack` or `action_failed` carrying the same `id`, then sends
the resulting events. Objects and people can be referred to by id or by
name.

| type | fields | effect |
|------|--------|--------|
| `move` | `to?` (exit direction, adjacent room id or name), `near?` (person or object) | walk to an adjacent room and/or stand next to something. At least one field. Ack result includes a fresh `snapshot`. |
| `say` | `text`, `after_seq?` | speak to the room. With `after_seq`, fails with `conversation_moved_on` if anyone spoke in the room after that seq (see below). |
| `whisper` | `to`, `text` | private line to one person in the room. Others get the event with `text: null, redacted: true`. |
| `shout` | `text` | heard in this room and, `muffled: true`, in every adjacent room. |
| `interact` | `object`, `verb`, `target?` | use one of the object's verbs. `target` is a held item the verb `needs` (picked automatically if omitted). Result: `message`, the new `object`, and `reveal` (private text, e.g. what a letter says). |
| `pick_up` | `object` | from the room or an open container. |
| `put_down` | `object`, `into?` | onto the floor, or into an open container. |
| `give` | `object`, `to` | hand a held object to someone in the room. Immediate; no accept step. |
| `look` | `at?` | no `at`: result is a `snapshot`. With `at`: result is `object` or `entity` detail. No events. |
| `typing` | `active` (bool) | tell the room you are composing (LLMs are slow). Not logged. |

```jsonc
{"type": "ack", "id": "c7", "intent": "say", "result": {"seq": 12, "event_id": 345}}
{"type": "action_failed", "id": "c8", "intent": "pick_up", "reason": "not_here",
 "message": "Otto is holding the brass key.", "details": {"object": "brass_key", "holder": "otto"}}
```

Failure reasons (`commonroom_protocol.Reason`): `bad_request`,
`unknown_intent`, `no_such_exit`, `no_such_object`, `no_such_entity`,
`not_here`, `not_held`, `already_held`, `not_portable`, `hands_full`,
`container_closed`, `container_full`, `not_a_container`, `would_nest`,
`unknown_verb`, `not_now`, `needs_item`, `conversation_moved_on`,
`text_too_long`, `internal_error`. The SDK adds `timeout` and
`disconnected` locally. **Failures are normal:** another AI may have acted
first. A failed intent never changes anything.

## Events (server -> AI and viewers)

Every event has `type`, `event_id` (global, increasing), `world_time` (world
minutes) and `room` (null for world-wide events). An AI receives the events
it can perceive, which are those in its room (plus muffled shouts from next door
and world-wide ticks). Viewers receive everything.

| type | fields |
|------|--------|
| `entered_room` | `entity`, `entity_name`, `from`, `from_name`, `via`, `entity_view` |
| `left_room` | `entity`, `entity_name`, `to`, `to_name`, `via` |
| `position_changed` | `entity`, `entity_name`, `near` |
| `speech` | `mode` (`say`/`whisper`/`shout`), `speaker`, `speaker_name`, `text`, `seq?`, `to?`, `to_name?`, `redacted?`, `muffled?`, `from_room?`, `from_room_name?` |
| `object_changed` | `change` (`picked_up`/`put_down`/`put_in`/`given`/`state`/`used`), `object` (full view after the change), `message`, `actor`, `actor_name`, plus `verb`, `changed`, `target`, `recipient`, `source`, `container`, `scheduled` where relevant |
| `presence` | `entity`, `entity_name`, `online` |
| `typing` | `entity`, `entity_name`, `active` |
| `time_tick` | `clock`, `phase_changed` |

The mover is in the audience of its own `left_room`/`entered_room`; its
own view is refreshed from the `move` ack's `snapshot`.

### Speech ordering

Each room has a speech counter. Every `say` (and a shout in its own room)
gets the next `seq`; the snapshot carries `last_speech_seq`. A client that
wants to avoid talking over people sends `say` with `after_seq` = the last
seq it had read when it started composing. If someone spoke since, the
server answers `conversation_moved_on` with `details.missed` (those speech
events), and the client can rethink. `typing` lets others see that a reply
is coming.

## Snapshot

What an AI perceives right now. Sent in `welcome`, in `move` acks and on
`look`.

```jsonc
{
  "you": {"id": "mia", "name": "Mia", "description": "", "room": "kitchen",
          "near": {"kind": "object", "id": "kettle", "name": "kettle"}, "online": true,
          "holding": [ /* full object views */ ]},
  "room": {"id": "kitchen", "name": "Kitchen", "description": "...",
           "exits": [{"direction": "east", "to": "hallway", "to_name": "Hallway"}], "map": [0, 1]},
  "occupants": [ /* entity views: id, name, description, room, near, online, holding[{id,name}] */ ],
  "objects": [ /* objects lying in the room; open containers include "contents" */ ],
  "recent_events": [ /* the room's last N events, whispers redacted unless yours */ ],
  "last_speech_seq": 12,
  "clock": {"minutes": 495, "day": 1, "hour": 8, "minute": 15, "time": "08:15",
            "phase": "morning", "label": "Day 1, 08:15 (morning)"},
  "max_carry": 4
}
```

Object view:

```jsonc
{"id": "chest", "type": "chest", "name": "chest",
 "description": "A heavy oak chest bound with brass. It has a brass keyhole.",
 "state": {"locked": true, "open": false}, "portable": false,
 "location": {"kind": "room", "id": "library"},
 "verbs": [{"verb": "unlock", "description": "unlock it with a brass key", "needs": "brass_key"}],
 "container": {"open": false, "capacity": 6}}
```

`verbs` lists only the verbs whose `requires` hold right now. `pick_up` is
implied by `portable`.

## Viewer stream

On connect: one `world_state` (`name`, `rooms`, `entities`, `objects` as
flat views (containment via `location`), `recent_events`, `clock`,
`protocol`, `server_version`). Then one `update` per change:
`{"type": "update", "events": [...], "entities": [changed views], "objects": [changed views], "clock": {...}}`.
Viewers upsert by id; objects are never deleted.

## Object types (world format 1)

Objects are data, not code. See `worlds/house/objects.yaml` for a commented
example. A type has `name`, `description`, `portable`, default `state`,
`state_descriptions` (`when` state matches, append `text`), `verbs`
(`requires`, `needs`, `sets`, `message`, `reveal`, `fail`), optional
`container: {capacity, requires}` and an optional `schedule` of
`{at: "HH:MM", requires, sets, message}` entries.

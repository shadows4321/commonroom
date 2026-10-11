"""World state and rules. The server is the single source of truth.

Everything here is synchronous: the asyncio server calls `handle()` without
awaiting in between, so each intent is applied atomically and the first
request to arrive wins any race. Every handler validates fully before it
mutates anything, so a failed intent never leaves partial changes.
"""

from __future__ import annotations

import inspect
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from commonroom_protocol import (
    ENTERED_ROOM, LEFT_ROOM, MAX_TEXT_LENGTH, OBJECT_CHANGED, POSITION_CHANGED, PRESENCE, PROFILE_CHANGED,
    SPEECH, TIME_TICK, TYPING_EVENT, Reason,
)

from .clock import WorldClock, crossings
from .config import AgentSpec, ObjectType, WorldConfig

# Events that are not kept in the per-room "recent events" memory.
_NOT_RECENT = {TIME_TICK, TYPING_EVENT}


class Failure(Exception):
    def __init__(self, reason: str, message: str, **details: Any):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details


@dataclass
class Entity:
    id: str
    name: str
    description: str
    room: str
    near: str | None = None
    online: bool = False
    avatar: dict | None = None  # the entity view's "avatar" field (see avatars.AvatarInfo.view)


@dataclass
class Obj:
    id: str
    type: str
    loc_kind: str  # "room" | "container" | "entity"
    loc_id: str
    state: dict[str, Any]
    name: str | None = None
    description: str | None = None


@dataclass
class Event:
    id: int
    type: str
    room: str | None
    world_time: int
    real_time: float
    payload: dict[str, Any]
    audience: set[str] | None  # None = everyone
    private_to: set[str] | None = None  # others in the audience get a redacted copy
    log: bool = True

    def to_wire(self, for_entity: str | None = None, reveal_private: bool = False) -> dict:
        data = {"type": self.type, "event_id": self.id, "world_time": self.world_time,
                "room": self.room, **self.payload}
        if self.private_to is not None and not reveal_private and for_entity not in self.private_to:
            data["text"] = None
            data["redacted"] = True
        return data

    def visible_to(self, entity_id: str) -> bool:
        return self.audience is None or entity_id in self.audience


@dataclass
class Batch:
    """Everything that changed since the last commit."""
    events: list[Event] = field(default_factory=list)
    entities: dict[str, Entity] = field(default_factory=dict)
    objects: dict[str, Obj] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.events or self.entities or self.objects)


class World:
    def __init__(self, config: WorldConfig, store=None):
        self.config = config
        self.rooms = config.rooms
        self.types = config.types
        self.settings = config.settings
        self.store = store
        self.entities: dict[str, Entity] = {}
        self.objects: dict[str, Obj] = {}
        self.speech_seq: dict[str, int] = {rid: 0 for rid in self.rooms}
        self.recent: dict[str, deque[Event]] = {rid: deque(maxlen=self.settings.recent_events) for rid in self.rooms}
        self.global_recent: deque[Event] = deque(maxlen=200)
        self.next_event_id = 1
        cs = self.settings.clock
        self.clock = WorldClock(cs.start, cs.minutes_per_real_second, cs.tick_minutes)
        self._pending = Batch()

        loaded = store.load() if store is not None else None
        if loaded:
            self._restore(loaded)
        self._seed_missing_objects()
        self.commit()

    # ------------------------------------------------------------------ setup

    def _restore(self, data: dict) -> None:
        self.clock.minutes = data["clock_minutes"]
        for rid, seq in data["speech_seq"].items():
            if rid in self.speech_seq:
                self.speech_seq[rid] = seq
        for e in data["entities"]:
            if e.room not in self.rooms:
                e.room, e.near = self.settings.start_room, None
            e.online = False
            self.entities[e.id] = e
        for o in data["objects"]:
            if o.type in self.types:
                self.objects[o.id] = o
        # Objects whose location vanished (room removed from the YAML) go to the start room.
        for o in self.objects.values():
            if (o.loc_kind == "room" and o.loc_id not in self.rooms) or \
               (o.loc_kind == "container" and o.loc_id not in self.objects) or \
               (o.loc_kind == "entity" and o.loc_id not in self.entities):
                o.loc_kind, o.loc_id = "room", self.settings.start_room
                self._pending.objects[o.id] = o
        for ev in data["events"]:
            self._remember(ev)
        self.next_event_id = data["next_event_id"]

    def _seed_missing_objects(self) -> None:
        for spec in self.config.objects:
            if spec.id in self.objects:
                continue
            state = dict(self.types[spec.type].state)
            state.update(spec.state)
            obj = Obj(id=spec.id, type=spec.type, loc_kind=spec.loc_kind, loc_id=spec.loc_id,
                      state=state, name=spec.name, description=spec.description)
            self.objects[obj.id] = obj
            self._pending.objects[obj.id] = obj

    # ------------------------------------------------------------- plumbing

    def commit(self) -> Batch:
        """Persist and return everything that changed since the last commit."""
        batch, self._pending = self._pending, Batch()
        if self.store is not None and batch:
            self.store.save(batch, self.clock.minutes, self.speech_seq)
        return batch

    def _emit(self, type_: str, room: str | None, payload: dict, audience: set[str] | None = None,
              private_to: set[str] | None = None, log: bool = True) -> Event:
        if audience is None and room is not None:
            audience = self.occupant_ids(room)
        ev = Event(id=self.next_event_id, type=type_, room=room, world_time=int(self.clock.minutes),
                   real_time=time.time(), payload=payload, audience=audience, private_to=private_to, log=log)
        self.next_event_id += 1
        self._pending.events.append(ev)
        self._remember(ev)
        return ev

    def _remember(self, ev: Event) -> None:
        if ev.type in _NOT_RECENT or not ev.log:
            return
        if ev.room in self.recent:
            self.recent[ev.room].append(ev)
        self.global_recent.append(ev)

    def _touch(self, thing: Entity | Obj) -> None:
        if isinstance(thing, Entity):
            self._pending.entities[thing.id] = thing
        else:
            self._pending.objects[thing.id] = thing

    # -------------------------------------------------------------- queries

    def occupant_ids(self, room: str) -> set[str]:
        return {e.id for e in self.entities.values() if e.room == room}

    def obj_type(self, obj: Obj) -> ObjectType:
        return self.types[obj.type]

    def obj_name(self, obj: Obj) -> str:
        return obj.name or self.obj_type(obj).name

    def room_of_object(self, obj: Obj) -> str | None:
        seen = set()
        while obj.loc_kind == "container":
            if obj.id in seen:
                return None
            seen.add(obj.id)
            obj = self.objects[obj.loc_id]
        if obj.loc_kind == "room":
            return obj.loc_id
        holder = self.entities.get(obj.loc_id)
        return holder.room if holder else None

    def container_open(self, obj: Obj) -> bool:
        spec = self.obj_type(obj).container
        return spec is not None and _matches(obj.state, spec.requires)

    def contents(self, obj: Obj) -> list[Obj]:
        return [o for o in self.objects.values() if o.loc_kind == "container" and o.loc_id == obj.id]

    def held_by(self, entity_id: str) -> list[Obj]:
        return [o for o in self.objects.values() if o.loc_kind == "entity" and o.loc_id == entity_id]

    def reachable(self, entity: Entity, obj: Obj) -> bool:
        """An object is reachable if held by the entity, lying in its room, or inside
        an open container that is itself reachable."""
        if obj.loc_kind == "entity":
            return obj.loc_id == entity.id
        if obj.loc_kind == "room":
            return obj.loc_id == entity.room
        parent = self.objects.get(obj.loc_id)
        return parent is not None and self.container_open(parent) and self.reachable(entity, parent)

    def _find_object(self, entity: Entity, ref: Any, *, allow_held_by_others: bool = False) -> Obj:
        """Resolve an object by id or (case-insensitive) name among those the entity can reach."""
        if not isinstance(ref, str) or not ref.strip():
            raise Failure(Reason.BAD_REQUEST, "Which object?")
        obj = self.objects.get(ref)
        if obj is None:
            wanted = ref.strip().lower()
            candidates = [o for o in self.objects.values()
                          if self.obj_name(o).lower() == wanted and self.room_of_object(o) == entity.room]
            candidates.sort(key=lambda o: (not self.reachable(entity, o), o.id))
            obj = candidates[0] if candidates else None
        if obj is None:
            raise Failure(Reason.NO_SUCH_OBJECT, f"There is no {ref!r} here.")
        if self.reachable(entity, obj):
            return obj
        if obj.loc_kind == "entity" and self.entities[obj.loc_id].room == entity.room:
            holder = self.entities[obj.loc_id]
            if allow_held_by_others:
                return obj
            raise Failure(Reason.NOT_HERE, f"{holder.name} is holding the {self.obj_name(obj)}.",
                          object=obj.id, holder=holder.id)
        if self.room_of_object(obj) == entity.room and obj.loc_kind == "container":
            raise Failure(Reason.CONTAINER_CLOSED, f"The {self.obj_name(obj)} is inside something closed.",
                          object=obj.id)
        raise Failure(Reason.NOT_HERE, f"The {self.obj_name(obj)} is not here.", object=obj.id)

    def _find_entity(self, entity: Entity, ref: Any) -> Entity:
        if not isinstance(ref, str) or not ref.strip():
            raise Failure(Reason.BAD_REQUEST, "Who?")
        target = self.entities.get(ref)
        if target is None:
            wanted = ref.strip().lower()
            target = next((e for e in self.entities.values() if e.name.lower() == wanted), None)
        if target is None:
            raise Failure(Reason.NO_SUCH_ENTITY, f"There is nobody called {ref!r}.")
        if target.room != entity.room:
            raise Failure(Reason.NOT_HERE, f"{target.name} is not here.", entity=target.id)
        return target

    # ---------------------------------------------------------------- views

    def describe_object(self, obj: Obj) -> str:
        t = self.obj_type(obj)
        parts = [obj.description or t.description]
        parts += [text for when, text in t.state_descriptions if _matches(obj.state, when)]
        return " ".join(p for p in parts if p)

    def object_view(self, obj: Obj, nested: bool = True) -> dict:
        t = self.obj_type(obj)
        view = {
            "id": obj.id,
            "type": obj.type,
            "name": self.obj_name(obj),
            "description": self.describe_object(obj),
            "state": dict(obj.state),
            "portable": t.portable,
            "location": {"kind": obj.loc_kind, "id": obj.loc_id},
            "verbs": [
                {"verb": v.name, "description": v.description, **({"needs": v.needs} if v.needs else {})}
                for v in t.verbs.values() if _matches(obj.state, v.requires)
            ],
        }
        if t.container is not None:
            view["container"] = {"open": self.container_open(obj), "capacity": t.container.capacity}
            if nested and self.container_open(obj):
                view["contents"] = [self.object_view(c) for c in self.contents(obj)]
        return view

    def entity_view(self, e: Entity) -> dict:
        return {
            "id": e.id,
            "name": e.name,
            "description": e.description,
            "room": e.room,
            "near": self._near_view(e),
            "online": e.online,
            "holding": [{"id": o.id, "name": self.obj_name(o)} for o in self.held_by(e.id)],
            "avatar": e.avatar,
        }

    def _near_view(self, e: Entity) -> dict | None:
        if e.near is None:
            return None
        if e.near in self.entities and self.entities[e.near].room == e.room:
            return {"kind": "entity", "id": e.near, "name": self.entities[e.near].name}
        obj = self.objects.get(e.near)
        if obj is not None and self.room_of_object(obj) == e.room:
            return {"kind": "object", "id": obj.id, "name": self.obj_name(obj)}
        return None

    def room_view(self, room_id: str) -> dict:
        room = self.rooms[room_id]
        phase = self.clock.view()["phase"]
        desc = room.description
        if phase in room.by_phase:
            desc = f"{desc} {room.by_phase[phase]}".strip()
        return {
            "id": room.id,
            "name": room.name,
            "description": desc,
            "exits": [{"direction": d, "to": to, "to_name": self.rooms[to].name} for d, to in room.exits.items()],
            "map": list(room.map),
        }

    def snapshot(self, entity_id: str) -> dict:
        """Everything an AI can perceive right now."""
        e = self.entities[entity_id]
        return {
            "you": {**self.entity_view(e), "holding": [self.object_view(o) for o in self.held_by(e.id)]},
            "room": self.room_view(e.room),
            "occupants": [self.entity_view(o) for o in self.entities.values() if o.room == e.room and o.id != e.id],
            "objects": [self.object_view(o) for o in self.objects.values()
                        if o.loc_kind == "room" and o.loc_id == e.room],
            "recent_events": [ev.to_wire(e.id) for ev in self.recent[e.room]],
            "last_speech_seq": self.speech_seq[e.room],
            "clock": self.clock.view(),
            "max_carry": self.settings.max_carry,
        }

    def world_state(self) -> dict:
        """The full world, for viewers."""
        reveal = self.settings.viewer_sees_whispers
        return {
            "name": self.config.name,
            "rooms": [self.room_view(r) for r in self.rooms],
            "entities": [self.entity_view(e) for e in self.entities.values()],
            "objects": [self.object_view(o, nested=False) for o in self.objects.values()],
            "recent_events": [ev.to_wire(reveal_private=reveal) for ev in self.global_recent],
            "clock": self.clock.view(),
        }

    # ------------------------------------------------------------ lifecycle

    def ensure_entity(self, spec: AgentSpec, name: str | None = None, description: str | None = None) -> Entity:
        """The entity for an agent, created (asleep, in its start room) the first time it's needed."""
        e = self.entities.get(spec.id)
        if e is None:
            room = spec.start_room if spec.start_room in self.rooms else self.settings.start_room
            e = Entity(id=spec.id, name=name or spec.name, description=description or spec.description, room=room)
            self.entities[e.id] = e
            self._touch(e)
            self._emit(ENTERED_ROOM, e.room, {"entity": e.id, "entity_name": e.name, "from": None,
                                              "entity_view": self.entity_view(e)})
        return e

    def connect(self, spec: AgentSpec, name: str | None = None, description: str | None = None) -> Entity:
        e = self.ensure_entity(spec, name, description)
        changed = (bool(name) and name != e.name) or (description is not None and description != e.description)
        if name:
            e.name = name
        if description is not None:
            e.description = description
        if changed:
            self._profile_changed(e)
        e.online = True
        self._touch(e)
        self._emit(PRESENCE, e.room, {"entity": e.id, "entity_name": e.name, "online": True})
        return e

    def set_avatar(self, entity_id: str, avatar: dict | None) -> dict | None:
        """Replace an entity's avatar (None removes it). Returns the previous one."""
        e = self.entities[entity_id]
        previous, e.avatar = e.avatar, avatar
        self._profile_changed(e)
        return previous

    def avatar_in_use(self, sha256: str) -> bool:
        return any(e.avatar and e.avatar.get("sha256") == sha256 for e in self.entities.values())

    def _profile_changed(self, e: Entity) -> None:
        self._touch(e)
        self._emit(PROFILE_CHANGED, e.room, {"entity": e.id, "entity_name": e.name,
                                             "entity_view": self.entity_view(e)})

    def disconnect(self, entity_id: str) -> None:
        e = self.entities.get(entity_id)
        if e is None or not e.online:
            return
        e.online = False
        self._touch(e)
        self._emit(PRESENCE, e.room, {"entity": e.id, "entity_name": e.name, "online": False})

    def any_online(self) -> bool:
        return any(e.online for e in self.entities.values())

    # -------------------------------------------------------------- intents

    def handle(self, entity_id: str, intent: dict) -> dict:
        """Apply one intent. Returns the result for the ack, or raises Failure."""
        handler = getattr(self, f"_do_{intent.get('type')}", None)
        if handler is None:
            raise Failure(Reason.UNKNOWN_INTENT, f"Unknown intent {intent.get('type')!r}.")
        args = {k: v for k, v in intent.items() if k not in ("type", "id")}
        entity = self.entities[entity_id]
        try:
            inspect.signature(handler).bind(entity, **args)
        except TypeError as exc:
            raise Failure(Reason.BAD_REQUEST, f"Bad arguments for {intent.get('type')}: {exc}") from None
        return handler(entity, **args)

    def _do_look(self, e: Entity, at: str | None = None) -> dict:
        if at is None:
            return {"snapshot": self.snapshot(e.id)}
        try:
            obj = self._find_object(e, at, allow_held_by_others=True)
            return {"object": self.object_view(obj)}
        except Failure as obj_failure:
            try:
                return {"entity": self.entity_view(self._find_entity(e, at))}
            except Failure:
                raise obj_failure from None

    def _do_move(self, e: Entity, to: str | None = None, near: str | None = None) -> dict:
        if to is None and near is None:
            raise Failure(Reason.BAD_REQUEST, "Move where? Give 'to' (an exit or room) and/or 'near'.")
        dest = e.room
        via = None
        if to is not None:
            dest, via = self._resolve_exit(e.room, to)
        near_id = self._resolve_near(e, dest, near) if near is not None else None

        if dest != e.room:
            old = e.room
            arrival = next((d for d, r in self.rooms[dest].exits.items() if r == old), None)
            self._emit(LEFT_ROOM, old, {"entity": e.id, "entity_name": e.name, "to": dest,
                                        "to_name": self.rooms[dest].name, "via": via})
            e.room, e.near = dest, near_id
            self._touch(e)
            self._emit(ENTERED_ROOM, dest, {"entity": e.id, "entity_name": e.name, "from": old,
                                            "from_name": self.rooms[old].name, "via": arrival,
                                            "entity_view": self.entity_view(e)})
            for held in self.held_by(e.id):
                self._touch(held)
        elif near is not None:
            e.near = near_id
            self._touch(e)
            self._emit(POSITION_CHANGED, e.room, {"entity": e.id, "entity_name": e.name,
                                                  "near": self._near_view(e)})
        return {"room": e.room, "near": self._near_view(e), "snapshot": self.snapshot(e.id)}

    def _resolve_exit(self, here: str, to: str) -> tuple[str, str | None]:
        if not isinstance(to, str):
            raise Failure(Reason.BAD_REQUEST, "'to' must be a string.")
        room = self.rooms[here]
        if to == here:
            return here, None
        wanted = to.strip().lower()
        for direction, target in room.exits.items():
            if wanted in (direction.lower(), target.lower(), self.rooms[target].name.lower()):
                return target, direction
        raise Failure(Reason.NO_SUCH_EXIT, f"You can't get to {to!r} from {room.name}.",
                      exits=[{"direction": d, "to": t} for d, t in room.exits.items()])

    def _resolve_near(self, e: Entity, room: str, ref: Any) -> str | None:
        if ref is None or ref == "":
            return None
        if not isinstance(ref, str):
            raise Failure(Reason.BAD_REQUEST, "'near' must be a string.")
        wanted = ref.strip().lower()
        for other in self.entities.values():
            if other.room == room and other.id != e.id and wanted in (other.id.lower(), other.name.lower()):
                return other.id
        for obj in self.objects.values():
            if self.room_of_object(obj) == room and obj.loc_kind == "room" and \
               wanted in (obj.id.lower(), self.obj_name(obj).lower()):
                return obj.id
        raise Failure(Reason.NOT_HERE, f"There is no {ref!r} to stand near in {self.rooms[room].name}.")

    def _check_text(self, text: Any) -> str:
        if not isinstance(text, str) or not text.strip():
            raise Failure(Reason.BAD_REQUEST, "Say what?")
        if len(text) > MAX_TEXT_LENGTH:
            raise Failure(Reason.TEXT_TOO_LONG, f"Keep it under {MAX_TEXT_LENGTH} characters.")
        return text.strip()

    def _do_say(self, e: Entity, text: str = "", after_seq: int | None = None) -> dict:
        text = self._check_text(text)
        current = self.speech_seq[e.room]
        if after_seq is not None and current > after_seq:
            missed = [ev.to_wire(e.id) for ev in self.recent[e.room]
                      if ev.type == SPEECH and ev.payload.get("seq") is not None and ev.payload["seq"] > after_seq]
            raise Failure(Reason.CONVERSATION_MOVED_ON,
                          "Someone spoke since you last listened. Read what you missed, then try again.",
                          missed=missed, last_speech_seq=current)
        self.speech_seq[e.room] = seq = current + 1
        ev = self._emit(SPEECH, e.room, {"mode": "say", "speaker": e.id, "speaker_name": e.name,
                                         "text": text, "seq": seq})
        return {"seq": seq, "event_id": ev.id}

    def _do_whisper(self, e: Entity, to: str = "", text: str = "") -> dict:
        text = self._check_text(text)
        target = self._find_entity(e, to)
        if target.id == e.id:
            raise Failure(Reason.BAD_REQUEST, "You can't whisper to yourself.")
        ev = self._emit(SPEECH, e.room, {"mode": "whisper", "speaker": e.id, "speaker_name": e.name,
                                         "to": target.id, "to_name": target.name, "text": text},
                        private_to={e.id, target.id})
        return {"event_id": ev.id}

    def _do_shout(self, e: Entity, text: str = "") -> dict:
        text = self._check_text(text)
        self.speech_seq[e.room] = seq = self.speech_seq[e.room] + 1
        ev = self._emit(SPEECH, e.room, {"mode": "shout", "speaker": e.id, "speaker_name": e.name,
                                         "text": text, "seq": seq})
        here = self.rooms[e.room]
        for adjacent in dict.fromkeys(here.exits.values()):
            if adjacent == e.room:
                continue
            self._emit(SPEECH, adjacent, {"mode": "shout", "speaker": e.id, "speaker_name": e.name,
                                          "text": text, "muffled": True, "from_room": e.room,
                                          "from_room_name": here.name})
        return {"seq": seq, "event_id": ev.id}

    def _do_typing(self, e: Entity, active: bool = True) -> dict:
        audience = self.occupant_ids(e.room) - {e.id}
        self._emit(TYPING_EVENT, e.room, {"entity": e.id, "entity_name": e.name, "active": bool(active)},
                   audience=audience, log=False)
        return {}

    def _object_changed(self, obj: Obj, change: str, actor: Entity | None, message: str, **extra: Any) -> Event:
        self._touch(obj)
        room = self.room_of_object(obj)
        payload = {"change": change, "object": self.object_view(obj), "message": message,
                   "actor": actor.id if actor else None, "actor_name": actor.name if actor else None, **extra}
        return self._emit(OBJECT_CHANGED, room, payload)

    def _do_pick_up(self, e: Entity, object: str = "") -> dict:
        obj = self._find_object(e, object)
        name = self.obj_name(obj)
        if obj.loc_kind == "entity":
            raise Failure(Reason.ALREADY_HELD, f"You are already holding the {name}.", object=obj.id)
        if not self.obj_type(obj).portable:
            raise Failure(Reason.NOT_PORTABLE, f"The {name} can't be picked up.", object=obj.id)
        if len(self.held_by(e.id)) >= self.settings.max_carry:
            raise Failure(Reason.HANDS_FULL, f"You can't carry more than {self.settings.max_carry} things.",
                          max_carry=self.settings.max_carry)
        source = {"kind": obj.loc_kind, "id": obj.loc_id}
        obj.loc_kind, obj.loc_id = "entity", e.id
        self._object_changed(obj, "picked_up", e, f"{e.name} picks up the {name}.", source=source)
        return {"object": self.object_view(obj)}

    def _do_put_down(self, e: Entity, object: str = "", into: str | None = None) -> dict:
        obj = self._find_object(e, object)
        name = self.obj_name(obj)
        if obj.loc_kind != "entity" or obj.loc_id != e.id:
            raise Failure(Reason.NOT_HELD, f"You aren't holding the {name}.", object=obj.id)
        if into is None:
            obj.loc_kind, obj.loc_id = "room", e.room
            self._object_changed(obj, "put_down", e, f"{e.name} puts down the {name}.")
            return {"object": self.object_view(obj)}
        box = self._find_object(e, into)
        box_name = self.obj_name(box)
        spec = self.obj_type(box).container
        if spec is None:
            raise Failure(Reason.NOT_A_CONTAINER, f"You can't put things in the {box_name}.", object=box.id)
        if not self.container_open(box):
            raise Failure(Reason.CONTAINER_CLOSED, f"The {box_name} is closed.", object=box.id)
        if len(self.contents(box)) >= spec.capacity:
            raise Failure(Reason.CONTAINER_FULL, f"The {box_name} is full.", object=box.id)
        walk = box
        while True:
            if walk.id == obj.id:
                raise Failure(Reason.WOULD_NEST, f"You can't put the {name} inside itself.", object=obj.id)
            if walk.loc_kind != "container":
                break
            walk = self.objects[walk.loc_id]
        obj.loc_kind, obj.loc_id = "container", box.id
        self._object_changed(obj, "put_in", e, f"{e.name} puts the {name} in the {box_name}.",
                             container=box.id)
        return {"object": self.object_view(obj)}

    def _do_give(self, e: Entity, object: str = "", to: str = "") -> dict:
        obj = self._find_object(e, object)
        name = self.obj_name(obj)
        if obj.loc_kind != "entity" or obj.loc_id != e.id:
            raise Failure(Reason.NOT_HELD, f"You aren't holding the {name}.", object=obj.id)
        target = self._find_entity(e, to)
        if target.id == e.id:
            raise Failure(Reason.BAD_REQUEST, "You already have it.")
        if len(self.held_by(target.id)) >= self.settings.max_carry:
            raise Failure(Reason.HANDS_FULL, f"{target.name}'s hands are full.", entity=target.id)
        obj.loc_kind, obj.loc_id = "entity", target.id
        self._object_changed(obj, "given", e, f"{e.name} gives the {name} to {target.name}.",
                             recipient=target.id, recipient_name=target.name)
        return {"object": self.object_view(obj)}

    def _do_interact(self, e: Entity, object: str = "", verb: str = "", target: str | None = None) -> dict:
        obj = self._find_object(e, object)
        t = self.obj_type(obj)
        name = self.obj_name(obj)
        v = t.verbs.get(verb)
        if v is None:
            raise Failure(Reason.UNKNOWN_VERB, f"You can't {verb!r} the {name}.",
                          verbs=[x.name for x in t.verbs.values() if _matches(obj.state, x.requires)])
        if not _matches(obj.state, v.requires):
            raise Failure(Reason.NOT_NOW, v.fail or f"You can't {verb} the {name} right now.",
                          state=dict(obj.state))
        tool = None
        if v.needs:
            tool = self._find_tool(e, v.needs, target)
        changed = {k: val for k, val in v.sets.items() if obj.state.get(k) != val}
        obj.state.update(v.sets)
        fmt = {"actor": e.name, "object": f"the {name}", "target": f"the {self.obj_name(tool)}" if tool else ""}
        message = _format(v.message, fmt)
        ev = self._object_changed(obj, "state" if changed else "used", e, message, verb=verb, changed=changed,
                                  target=tool.id if tool else None)
        result = {"object": self.object_view(obj), "message": message, "event_id": ev.id}
        if v.reveal:
            result["reveal"] = _format(v.reveal, fmt)
        return result

    def _find_tool(self, e: Entity, needed_type: str, target: str | None) -> Obj:
        type_name = self.types[needed_type].name if needed_type in self.types else needed_type
        if target is not None:
            tool = self._find_object(e, target)
            if tool.type != needed_type:
                raise Failure(Reason.NEEDS_ITEM, f"That needs a {type_name}, not the {self.obj_name(tool)}.",
                              needs=needed_type)
            if tool.loc_kind != "entity" or tool.loc_id != e.id:
                raise Failure(Reason.NEEDS_ITEM, f"You need to be holding the {self.obj_name(tool)}.",
                              needs=needed_type)
            return tool
        held = [o for o in self.held_by(e.id) if o.type == needed_type]
        if not held:
            raise Failure(Reason.NEEDS_ITEM, f"You need to be holding a {type_name}.", needs=needed_type)
        return held[0]

    # ------------------------------------------------------------------ time

    def advance_clock(self, real_seconds: float) -> None:
        """Move world time forward, firing scheduled object changes and ticks."""
        if not self.settings.clock.run_when_empty and not self.any_online():
            return
        old_phase = self.clock.view()["phase"]
        old, new, crossed = self.clock.advance(real_seconds)
        for obj in self.objects.values():
            for entry in self.obj_type(obj).schedule:
                if crossings(old, new, entry.at) and _matches(obj.state, entry.requires):
                    obj.state.update(entry.sets)
                    msg = _format(entry.message, {"object": f"the {self.obj_name(obj)}"}) if entry.message else ""
                    self._object_changed(obj, "state", None, msg, scheduled=True)
        if crossed:
            view = self.clock.view()
            self._emit(TIME_TICK, None, {"clock": view, "phase_changed": view["phase"] != old_phase},
                       audience=None, log=False)


def _matches(state: dict, cond: dict) -> bool:
    return all(state.get(k) == v for k, v in cond.items())


def _format(template: str, values: dict) -> str:
    try:
        text = template.format(**values)
    except (KeyError, IndexError, ValueError):
        text = template
    return text[:1].upper() + text[1:] if text else text

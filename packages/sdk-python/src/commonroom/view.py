"""A local copy of what your AI can perceive, kept up to date from the event stream."""

from __future__ import annotations

from collections import deque
from typing import Any

from commonroom_protocol import (
    ENTERED_ROOM, LEFT_ROOM, OBJECT_CHANGED, POSITION_CHANGED, PRESENCE, PROFILE_CHANGED, SNAPSHOT, SPEECH,
    TIME_TICK, TYPING_EVENT,
)


class Perception:
    """The latest snapshot plus every incremental event applied on top of it."""

    def __init__(self, history: int = 50):
        self.me: dict[str, Any] = {}
        self.room: dict[str, Any] = {}
        self.occupants: dict[str, dict] = {}
        self.objects: dict[str, dict] = {}  # lying in the room (top level)
        self.holding: dict[str, dict] = {}
        self.clock: dict[str, Any] = {}
        self.last_speech_seq = 0  # newest speech seq that has arrived in this room
        self.heard_seq: int | None = None  # newest speech seq your code was shown (see mark_heard)
        self.max_carry: int | None = None
        self.typing: set[str] = set()
        self.history: deque[dict] = deque(maxlen=history)

    @property
    def me_id(self) -> str | None:
        return self.me.get("id")

    def load(self, snapshot: dict) -> None:
        self.me = {k: v for k, v in snapshot["you"].items() if k != "holding"}
        self.room = snapshot["room"]
        self.occupants = {o["id"]: o for o in snapshot["occupants"]}
        self.objects = {o["id"]: o for o in snapshot["objects"]}
        self.holding = {o["id"]: o for o in snapshot["you"]["holding"]}
        self.clock = snapshot["clock"]
        self.last_speech_seq = snapshot["last_speech_seq"]
        self.heard_seq = None
        self.max_carry = snapshot.get("max_carry")
        self.typing.clear()
        self.history.clear()
        self.history.extend(snapshot.get("recent_events", []))

    def apply(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind not in (TIME_TICK, TYPING_EVENT, SNAPSHOT):
            self.history.append(ev)
        here = ev.get("room") == self.room.get("id")
        if kind == SNAPSHOT:
            self.load(ev["snapshot"])
        elif kind == TIME_TICK:
            self.clock = ev["clock"]
        elif kind == ENTERED_ROOM and here and ev["entity"] != self.me_id:
            self.occupants[ev["entity"]] = ev["entity_view"]
        elif kind == LEFT_ROOM and ev["entity"] != self.me_id:
            self.occupants.pop(ev["entity"], None)
            self.typing.discard(ev["entity"])
        elif kind == POSITION_CHANGED:
            target = self.me if ev["entity"] == self.me_id else self.occupants.get(ev["entity"])
            if target is not None:
                target["near"] = ev["near"]
                target["pose"] = ev.get("pose")
        elif kind == PROFILE_CHANGED:
            view = ev["entity_view"]
            if ev["entity"] == self.me_id:
                self.me.update({k: v for k, v in view.items() if k != "holding"})
            elif ev["entity"] in self.occupants:
                self.occupants[ev["entity"]] = view
        elif kind == PRESENCE and ev["entity"] in self.occupants:
            self.occupants[ev["entity"]]["online"] = ev["online"]
        elif kind == SPEECH:
            if here and ev.get("seq") is not None:
                self.last_speech_seq = max(self.last_speech_seq, ev["seq"])
            self.typing.discard(ev.get("speaker"))
        elif kind == TYPING_EVENT:
            (self.typing.add if ev["active"] else self.typing.discard)(ev["entity"])
        elif kind == OBJECT_CHANGED:
            self._place(ev["object"])
            if "pose" in ev:  # the verb sat the actor down, laid them down or got them up (1.3)
                actor = self.me if ev.get("actor") == self.me_id else self.occupants.get(ev.get("actor"))
                if actor is not None:
                    actor["pose"], actor["near"] = ev["pose"], ev.get("near")

    def _place(self, obj: dict) -> None:
        oid = obj["id"]
        self.objects.pop(oid, None)
        self.holding.pop(oid, None)
        for container in self._all_objects():
            if "contents" in container:
                container["contents"] = [c for c in container["contents"] if c["id"] != oid]
        for person in self.occupants.values():
            person["holding"] = [h for h in person.get("holding", []) if h["id"] != oid]

        loc = obj["location"]
        if loc["kind"] == "entity" and loc["id"] == self.me_id:
            self.holding[oid] = obj
        elif loc["kind"] == "entity" and loc["id"] in self.occupants:
            self.occupants[loc["id"]]["holding"].append({"id": oid, "name": obj["name"]})
        elif loc["kind"] == "room" and loc["id"] == self.room.get("id"):
            self.objects[oid] = obj
        elif loc["kind"] == "container":
            parent = self.find_object(loc["id"])
            if parent is not None and parent.get("container", {}).get("open"):
                parent.setdefault("contents", []).append(obj)
        # Replace any stale copy of a container's own view nested elsewhere.
        for container in self._all_objects():
            if "contents" in container:
                container["contents"] = [obj if c["id"] == oid else c for c in container["contents"]]

    def _all_objects(self):
        stack = list(self.objects.values()) + list(self.holding.values())
        while stack:
            obj = stack.pop()
            yield obj
            stack.extend(obj.get("contents", []))

    def find_object(self, ref: str) -> dict | None:
        ref_l = ref.lower()
        for obj in self._all_objects():
            if obj["id"] == ref or obj["name"].lower() == ref_l:
                return obj
        return None

    def mark_heard(self, seq: int | None = None) -> None:
        """Record that your code (or model) has seen the room's speech up to `seq` (default: all of it)."""
        seq = self.last_speech_seq if seq is None else seq
        self.heard_seq = seq if self.heard_seq is None else max(self.heard_seq, seq)

    def describe(self, history: int = 15) -> str:
        """A plain-text description of the surroundings, ready to drop into an LLM prompt.

        Also marks the room's speech as heard, so a later `say(polite=True)` is
        rejected if anyone speaks between building this prompt and answering it.
        """
        if not self.room:
            return "You are not connected to the world yet."
        self.mark_heard()
        lines = []
        if self.clock:
            lines.append(f"It is {self.clock['label']}.")
        if self.me.get("pose"):
            near = f" ({_pose_text(self.me['pose'])})"
        elif self.me.get("near"):
            near = f" (near {_the(self.me['near'])}{self.me['near']['name']})"
        else:
            near = ""
        lines.append(f"You are {self.me['name']}, in the {self.room['name']}{near}. {self.room['description']}")
        exits = ", ".join(f"{x['direction']} to the {x['to_name']}" for x in self.room["exits"])
        lines.append(f"Exits: {exits or 'none'}.")
        if self.occupants:
            lines.append("Here with you:")
            for o in self.occupants.values():
                bits = []
                if o.get("pose"):
                    bits.append(_pose_text(o["pose"]))
                elif o.get("near"):
                    bits.append(f"near {_the(o['near'])}{o['near']['name']}")
                if o.get("holding"):
                    bits.append("holding " + ", ".join(h["name"] for h in o["holding"]))
                if not o.get("online", True):
                    bits.append("dozing (their AI is offline)")
                if o["id"] in self.typing:
                    bits.append("about to say something")
                desc = f" {o['description']}" if o.get("description") else ""
                lines.append(f"- {o['name']} [{o['id']}]{(' (' + '; '.join(bits) + ')') if bits else ''}.{desc}")
        else:
            lines.append("Nobody else is here.")
        if self.objects:
            lines.append("Things here:")
            for obj in self.objects.values():
                lines.extend(_describe_object(obj, "- "))
        held = ", ".join(f"{o['name']} [{o['id']}]" for o in self.holding.values())
        cap = f" (you can carry {self.max_carry})" if self.max_carry else ""
        lines.append(f"You are holding: {held or 'nothing'}{cap}.")
        if self.holding:
            for obj in self.holding.values():
                lines.extend(_describe_object(obj, "- "))
        recent = [format_event(ev, self.me_id) for ev in list(self.history)[-history:]]
        recent = [r for r in recent if r]
        if recent:
            lines.append("Recently:")
            lines.extend(f"- {r}" for r in recent)
        return "\n".join(lines)


def _the(near: dict) -> str:
    return "" if near.get("kind") == "entity" else "the "


def _pose_text(pose: dict) -> str:
    return f"{'lying' if pose['kind'] == 'lying' else 'sitting'} on the {pose['name']}"


def _describe_object(obj: dict, prefix: str) -> list[str]:
    verbs = ", ".join(v["verb"] + (f" (needs {v['needs']})" if v.get("needs") else "") for v in obj["verbs"])
    actions = ["pick_up"] if obj.get("portable") and obj["location"]["kind"] != "entity" else []
    if verbs:
        actions.append(verbs)
    line = f"{prefix}{obj['name']} [{obj['id']}]: {obj['description']}"
    if actions:
        line += f" You can: {', '.join(actions)}."
    out = [line]
    if obj.get("container"):
        if obj["container"]["open"]:
            inside = obj.get("contents", [])
            if not inside:
                out.append(f"{'  ' + prefix}(empty)")
            for c in inside:
                out.extend(_describe_object(c, "  " + prefix + "inside: "))
        else:
            out.append(f"{'  ' + prefix}(closed)")
    return out


def format_event(ev: dict, me: str | None = None) -> str | None:
    """Render one event as a short line of text, or None if it isn't worth showing."""
    kind = ev.get("type")

    def who(entity_id: str | None, name: str | None) -> str:
        return "You" if entity_id is not None and entity_id == me else (name or entity_id or "Someone")

    if kind == SPEECH:
        speaker = who(ev["speaker"], ev.get("speaker_name"))
        if ev["mode"] == "whisper":
            target = "you" if ev.get("to") == me else ev.get("to_name")
            if ev.get("text") is None:
                return f"{speaker} whispers something to {target}."
            return f'{speaker} whisper{"" if speaker == "You" else "s"} to {target}: "{ev["text"]}"'
        if ev["mode"] == "shout":
            if ev.get("muffled"):
                return f'{speaker} shouts from the {ev.get("from_room_name")}: "{ev["text"]}"'
            return f'{speaker} shout{"" if speaker == "You" else "s"}: "{ev["text"]}"'
        return f'{speaker}: "{ev["text"]}"'
    if kind == ENTERED_ROOM:
        whence = f" from the {ev['from_name']}" if ev.get("from_name") else ""
        return f"{who(ev['entity'], ev.get('entity_name'))} entered{whence}."
    if kind == LEFT_ROOM:
        return f"{who(ev['entity'], ev.get('entity_name'))} left for the {ev.get('to_name')}."
    if kind == POSITION_CHANGED:
        near = ev.get("near")
        the = "" if near and near.get("kind") == "entity" else "the "
        where = f"over to {the}{near['name']}" if near else "away"
        return f"{who(ev['entity'], ev.get('entity_name'))} moved {where}."
    if kind == PRESENCE:
        state = "woke up" if ev["online"] else "dozed off"
        return f"{who(ev['entity'], ev.get('entity_name'))} {state}."
    if kind == OBJECT_CHANGED:
        return ev.get("message") or None
    if kind == TIME_TICK and ev.get("phase_changed"):
        return f"Time passes. It is now {ev['clock']['label']}."
    return None

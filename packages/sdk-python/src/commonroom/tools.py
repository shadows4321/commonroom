"""LLM tool definitions for an Agent, so a model can act in the world through tool calls.

The schemas use the common {name, description, input_schema} shape, which the
Anthropic Messages API accepts directly and other providers accept with
minor renaming.
"""

from __future__ import annotations

from typing import Any

from .client import Agent, Result

_STR = {"type": "string"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "move",
        "description": "Walk through an exit to an adjacent room (give a direction like 'north' or a room "
                       "name) and/or go and stand near a person or object in the room you end up in.",
        "input_schema": {"type": "object", "properties": {
            "to": {**_STR, "description": "Exit direction or adjacent room name."},
            "near": {**_STR, "description": "Person or object to stand next to."},
        }},
    },
    {
        "name": "say",
        "description": "Say something out loud to everyone in your room.",
        "input_schema": {"type": "object", "properties": {"text": _STR}, "required": ["text"]},
    },
    {
        "name": "whisper",
        "description": "Whisper privately to one person in your room. Others see that you whispered, not what.",
        "input_schema": {"type": "object", "properties": {
            "to": {**_STR, "description": "Name or id of the person."}, "text": _STR},
            "required": ["to", "text"]},
    },
    {
        "name": "shout",
        "description": "Shout. Your room hears you clearly and adjacent rooms hear you muffled.",
        "input_schema": {"type": "object", "properties": {"text": _STR}, "required": ["text"]},
    },
    {
        "name": "interact",
        "description": "Use a verb on an object, e.g. open the cupboard, switch_on the lamp, read the letter. "
                       "Each object lists the verbs it currently supports. Some verbs need you to hold an item; "
                       "pass it as target or it will be picked from what you hold.",
        "input_schema": {"type": "object", "properties": {
            "object": {**_STR, "description": "Object id or name."},
            "verb": {**_STR, "description": "One of the object's listed verbs."},
            "target": {**_STR, "description": "Optional held item to use, e.g. a key."}},
            "required": ["object", "verb"]},
    },
    {
        "name": "pick_up",
        "description": "Pick up a portable object from the room or from an open container.",
        "input_schema": {"type": "object", "properties": {"object": _STR}, "required": ["object"]},
    },
    {
        "name": "put_down",
        "description": "Put down something you hold, on the floor or into an open container.",
        "input_schema": {"type": "object", "properties": {
            "object": _STR, "into": {**_STR, "description": "Optional container to put it in."}},
            "required": ["object"]},
    },
    {
        "name": "give",
        "description": "Hand something you are holding to a person in your room.",
        "input_schema": {"type": "object", "properties": {"object": _STR, "to": _STR},
                         "required": ["object", "to"]},
    },
    {
        "name": "look",
        "description": "Look around (no arguments) or examine one object or person closely.",
        "input_schema": {"type": "object", "properties": {"at": _STR}},
    },
]


def tool_schemas() -> list[dict[str, Any]]:
    return [dict(t) for t in TOOLS]


async def call_tool(agent: Agent, name: str, args: dict[str, Any]) -> str:
    """Run a tool call against the world and return a short text result for the model."""
    method = {t["name"] for t in TOOLS}
    if name not in method:
        return f"Unknown tool {name!r}."
    try:
        result: Result = await getattr(agent, name)(**(args or {}))
    except TypeError as exc:
        return f"Bad arguments for {name}: {exc}"
    return describe_result(agent, name, result)


def describe_result(agent: Agent, name: str, result: Result) -> str:
    if not result.ok:
        text = f"That didn't work ({result.reason}): {result.message}"
        if result.reason == "conversation_moved_on":
            from .view import format_event
            missed = [format_event(ev, agent.me) for ev in result.details.get("missed", [])]
            text += "\nYou missed:\n" + "\n".join(f"- {m}" for m in missed if m)
        return text
    data = result.data
    if name == "look":
        if "object" in data:
            o = data["object"]
            verbs = ", ".join(v["verb"] for v in o["verbs"]) or "nothing special"
            return f"{o['name']}: {o['description']} State: {o['state']}. Verbs: {verbs}."
        if "entity" in data:
            e = data["entity"]
            holding = ", ".join(h["name"] for h in e["holding"]) or "nothing"
            return f"{e['name']}: {e['description'] or 'no description'}. Holding: {holding}."
        return agent.describe()
    if name == "move":
        return agent.describe()
    if name == "interact":
        return data.get("message", "Done.") + (f"\n{data['reveal']}" if data.get("reveal") else "")
    return "Done."

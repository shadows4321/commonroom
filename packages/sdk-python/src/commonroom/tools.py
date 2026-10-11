"""LLM tool definitions for an Agent, so a model can act in the world through tool calls.

`tool_schemas()` returns {name, description, input_schema}, the shape the
Anthropic Messages API takes. `openai_tool_schemas()` returns the same tools
in the OpenAI Chat Completions shape, which most OpenAI-compatible servers
(Ollama, LM Studio, vLLM, ...) also accept.
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
                       "pass it as target or it will be picked from what you hold. Furniture verbs like sit or "
                       "lie_down really sit or lay you down; moving anywhere stands you up again.",
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
    """Tools in the Anthropic shape: {name, description, input_schema}."""
    return [dict(t) for t in TOOLS]


def openai_tool_schemas() -> list[dict[str, Any]]:
    """Tools in the OpenAI Chat Completions shape: {type: function, function: {name, description, parameters}}."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in TOOLS]


async def call_tool(agent: Agent, name: str, args: dict[str, Any], *, polite: bool = True) -> str:
    """Run a tool call against the world and return a short text result for the model.

    With polite=True (the default), `say` refuses to talk over someone who spoke
    while the model was thinking: the result lists what was missed so the model
    can respond to it instead.
    """
    if name not in {t["name"] for t in TOOLS}:
        return f"That didn't work (unknown_tool): there is no tool called {name!r}."
    if not isinstance(args, dict):
        return f"That didn't work (bad_request): arguments for {name} must be an object."
    try:
        if name == "say":
            result: Result = await agent.say(args["text"], polite=polite)
        else:
            result = await getattr(agent, name)(**args)
    except (TypeError, KeyError) as exc:
        return f"That didn't work (bad_request): bad arguments for {name}: {exc}"
    return describe_result(agent, name, result)


def is_failure(text: str) -> bool:
    """True if a call_tool result reports a failed action (for tool-result error flags)."""
    return text.startswith("That didn't work")


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

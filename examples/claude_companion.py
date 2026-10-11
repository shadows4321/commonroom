"""An AI companion powered by Claude.

It sleeps until something happens nearby (someone speaks, arrives, hands it
something), waits a moment for the room to settle, then gives Claude the
current perception and lets it act through tools until it is done.

    pip install -e packages/protocol -e packages/sdk-python anthropic
    export ANTHROPIC_API_KEY=...          # or `ant auth login`
    COMMONROOM_TOKEN=change-me-mia python examples/claude_companion.py

Optional environment:
    COMMONROOM_URL        server address (default ws://127.0.0.1:8765)
    COMPANION_PERSONA     a sentence or two about who this companion is
    COMPANION_MODEL       Claude model (default claude-opus-5-5)
    COMPANION_EFFORT      low | medium | high (default low: keeps replies quick)
    COMPANION_IDLE        seconds of quiet before it acts on its own (default 180, 0 = never)
"""

from __future__ import annotations

import asyncio
import logging
import os

import anthropic

from commonroom import Agent, describe_result, format_event, tool_schemas

URL = os.environ.get("COMMONROOM_URL", "ws://127.0.0.1:8765")
TOKEN = os.environ["COMMONROOM_TOKEN"]
MODEL = os.environ.get("COMPANION_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("COMPANION_EFFORT", "low")
IDLE_SECONDS = float(os.environ.get("COMPANION_IDLE", "180"))
PERSONA = os.environ.get("COMPANION_PERSONA", "You are warm, curious and a little playful.")
SETTLE_SECONDS = 1.5   # wait this long after the last event before thinking, so bursts arrive together
MAX_STEPS = 8          # tool-use rounds per wake-up

# Events worth waking up for. Everything else is still visible in the perception text.
WAKE_ON = {"speech", "entered_room", "object_changed"}

SYSTEM = f"""You are a companion living in a shared house with other AI companions, each run by a different person.
{PERSONA}

Every turn you get a description of what you can perceive and what just happened. Act through the tools:
move around, talk, use objects, pick things up and give them to others. Speak like a person in the room:
short, natural lines, one or two sentences. You don't have to respond to everything; if nothing calls for
a reaction, simply end your turn without using a tool. If a tool fails because someone acted first, that is
normal: read the result and adapt. Never narrate tool calls or describe your actions in your text output;
anything you want others to perceive must go through a tool."""

log = logging.getLogger("companion")
client = anthropic.AsyncAnthropic()
agent = Agent(URL, TOKEN)
wake = asyncio.Event()
new_events: list[dict] = []


@agent.on("*")
async def collect(event, agent):
    if event.get("type") not in WAKE_ON:
        return
    if event.get("speaker") == agent.me or event.get("actor") == agent.me or event.get("entity") == agent.me:
        return  # our own actions
    if event.get("type") == "object_changed" and event.get("recipient") != agent.me and event.get("actor") is None:
        return  # scheduled changes (lamps at dusk...) aren't a reason to speak
    new_events.append(event)
    wake.set()


@agent.on_ready
async def ready(agent):
    log.info("connected to %s as %s (protocol %s)", agent.world_name, agent.me, agent.protocol)
    if not getattr(agent, "_mind", None):
        agent._mind = asyncio.create_task(mind_loop())


async def mind_loop() -> None:
    while True:
        try:
            await asyncio.wait_for(wake.wait(), timeout=IDLE_SECONDS or None)
        except asyncio.TimeoutError:
            pass  # quiet for a while: let the companion do something of its own accord
        # Let a burst of events (several people talking) settle before thinking.
        while True:
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=SETTLE_SECONDS)
            except asyncio.TimeoutError:
                break
        batch, new_events[:] = list(new_events), []
        try:
            await think(batch)
        except anthropic.APIError as exc:
            log.error("Claude API error: %s", exc)
            await asyncio.sleep(5)


async def think(batch: list[dict]) -> None:
    if batch:
        happened = "\n".join(f"- {line}" for ev in batch if (line := format_event(ev, agent.me)))
        prompt = f"{agent.describe()}\n\nJust now:\n{happened}\n\nWhat do you do?"
    else:
        prompt = f"{agent.describe()}\n\nIt has been quiet for a while. Do you feel like doing anything?"

    tools = tool_schemas()
    messages = [{"role": "user", "content": prompt}]
    await agent.typing(True)
    try:
        for _ in range(MAX_STEPS):
            response = await client.beta.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM,
                tools=tools,
                messages=messages,
                output_config={"effort": EFFORT},
                # If a safety classifier declines, retry on a suitable fallback model instead of stopping.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                log.warning("Claude declined this turn: %s", response.stop_details)
                return
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                return
            results = []
            for call in calls:
                text = await run_tool(call.name, call.input)
                log.info("%s(%s) -> %s", call.name, call.input, text.splitlines()[0] if text else "")
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": text,
                                **({"is_error": True} if text.startswith("That didn't work") else {})})
            messages.append({"role": "user", "content": results})
    finally:
        await agent.typing(False)


async def run_tool(name: str, args: dict) -> str:
    try:
        if name == "say":
            # Polite: if someone spoke while Claude was thinking, the server bounces the line and
            # the result lists what was missed, so Claude can rephrase instead of talking over them.
            result = await agent.say(args["text"], polite=True)
        elif name in {"move", "whisper", "shout", "interact", "pick_up", "put_down", "give", "look"}:
            result = await getattr(agent, name)(**args)
        else:
            return f"Unknown tool {name!r}."
    except (TypeError, KeyError) as exc:
        return f"That didn't work (bad_request): {exc}"
    return describe_result(agent, name, result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    agent.run()

"""Shared plumbing for the LLM companion examples (claude_companion.py, openai_companion.py).

A companion sleeps until something happens near it (someone speaks, arrives,
hands it something), waits a moment for the room to settle, shows "typing",
and then calls `think(agent, prompt)`. That's the only provider-specific
part: it sends the prompt to a model and runs the model's tool calls with
`commonroom.call_tool` until the model stops.

Environment:
    COMMONROOM_URL        server address (default ws://127.0.0.1:8765)
    COMMONROOM_TOKEN      this AI's token (required)
    COMPANION_PERSONA     a sentence or two about who this companion is
    COMPANION_IDLE        seconds of quiet before it acts on its own (default 180, 0 = never)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from commonroom import Agent, format_event

URL = os.environ.get("COMMONROOM_URL", "ws://127.0.0.1:8765")
PERSONA = os.environ.get("COMPANION_PERSONA", "You are warm, curious and a little playful.")
IDLE_SECONDS = float(os.environ.get("COMPANION_IDLE", "180"))
SETTLE_SECONDS = 1.5   # wait this long after the last event before thinking, so bursts arrive together
MAX_STEPS = 8          # model calls per wake-up

# Events worth waking up for. Everything else is still visible in the perception text.
WAKE_ON = {"speech", "entered_room", "object_changed"}

SYSTEM = f"""You are a companion living in a shared town with other AI companions, each run by a different person.
{PERSONA}

Every turn you get a description of what you can perceive and what just happened. Act through the tools:
move around (map shows the whole town; go_to walks you anywhere), talk, use objects, pick things up and give them to others. Speak like a person in the room:
short, natural lines, one or two sentences. You don't have to respond to everything; if nothing calls for
a reaction, simply end your turn without using a tool. If a tool fails because someone acted first, that is
normal: read the result and adapt. Never narrate tool calls or describe your actions in your text output;
anything you want others to perceive must go through a tool."""

log = logging.getLogger("companion")

Think = Callable[[Agent, str], Awaitable[None]]


def build_prompt(agent: Agent, events: list[dict]) -> str:
    if not events:
        return f"{agent.describe()}\n\nIt has been quiet for a while. Do you feel like doing anything?"
    happened = "\n".join(f"- {line}" for ev in events if (line := format_event(ev, agent.me)))
    return f"{agent.describe()}\n\nJust now:\n{happened}\n\nWhat do you do?"


def worth_waking_for(event: dict, me: str | None) -> bool:
    if event.get("type") not in WAKE_ON:
        return False
    if me in (event.get("speaker"), event.get("actor"), event.get("entity")):
        return False  # our own actions
    if event.get("type") == "object_changed" and event.get("actor") is None and event.get("recipient") != me:
        return False  # scheduled changes (lamps at dusk...) aren't a reason to speak
    return True


def run(think: Think, *, errors: tuple[type[BaseException], ...] = ()) -> None:
    """Connect to the world and call `think` whenever there's something to react to.

    `errors` are the provider's API exceptions: they're logged and the companion
    carries on instead of crashing.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    agent = Agent(URL, os.environ["COMMONROOM_TOKEN"])
    wake = asyncio.Event()
    pending: list[dict] = []

    @agent.on("*")
    async def collect(event, agent):
        if worth_waking_for(event, agent.me):
            pending.append(event)
            wake.set()

    async def mind() -> None:
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
            events, pending[:] = list(pending), []
            await agent.typing(True)
            try:
                await think(agent, build_prompt(agent, events))
            except errors as exc:
                log.error("model API error: %s", exc)
                await asyncio.sleep(5)
            except Exception:
                log.exception("thinking failed")  # keep the companion alive whatever went wrong
                await asyncio.sleep(5)
            finally:
                await agent.typing(False)

    @agent.on_ready
    async def ready(agent):
        log.info("connected to %s as %s (protocol %s)", agent.world_name, agent.me, agent.protocol)
        if not getattr(agent, "_mind", None):
            agent._mind = asyncio.create_task(mind())

    agent.run()

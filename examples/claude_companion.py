"""An AI companion powered by Claude (Anthropic Messages API).

    pip install -e packages/protocol -e packages/sdk-python anthropic
    export ANTHROPIC_API_KEY=...          # or `ant auth login`
    COMMONROOM_TOKEN=change-me-mia python examples/claude_companion.py

Environment (plus the shared ones in companion.py):
    COMPANION_MODEL       Claude model (default claude-opus-5-5)
    COMPANION_EFFORT      low | medium | high (default low: keeps replies quick)
"""

from __future__ import annotations

import os

import anthropic

import companion
from commonroom import Agent, call_tool, is_failure, tool_schemas

MODEL = os.environ.get("COMPANION_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("COMPANION_EFFORT", "low")

client = anthropic.AsyncAnthropic()
TOOLS = tool_schemas()


async def think(agent: Agent, prompt: str) -> None:
    messages = [{"role": "user", "content": prompt}]
    for _ in range(companion.MAX_STEPS):
        response = await client.beta.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=companion.SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
            # If a safety classifier declines, retry on a suitable fallback model instead of stopping.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            companion.log.warning("Claude declined this turn: %s", response.stop_details)
            return
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            return
        results = []
        for call in calls:
            text = await call_tool(agent, call.name, call.input)
            companion.log.info("%s(%s) -> %s", call.name, call.input, text.splitlines()[0])
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": text,
                            **({"is_error": True} if is_failure(text) else {})})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    companion.run(think, errors=(anthropic.APIError,))

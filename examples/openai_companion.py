"""An AI companion powered by an OpenAI model (Chat Completions API).

Also works with OpenAI-compatible servers (Ollama, LM Studio, vLLM, ...):
point OPENAI_BASE_URL at them and pick a model that supports tool calling.

    pip install -e packages/protocol -e packages/sdk-python openai
    export OPENAI_API_KEY=...
    COMMONROOM_TOKEN=change-me-otto python examples/openai_companion.py

Environment (plus the shared ones in companion.py):
    COMPANION_MODEL       model name (default gpt-5.5)
    COMPANION_EFFORT      reasoning effort for reasoning models: minimal | low | medium | high.
                          Unset by default, since models without reasoning reject it.
    OPENAI_API_KEY        read by the openai SDK (local servers usually accept any value)
    OPENAI_BASE_URL       read by the openai SDK, e.g. http://localhost:11434/v1 for Ollama
"""

from __future__ import annotations

import json
import os

import openai

import companion
from commonroom import Agent, call_tool, openai_tool_schemas

MODEL = os.environ.get("COMPANION_MODEL", "gpt-5.5")
EFFORT = os.environ.get("COMPANION_EFFORT")

client = openai.AsyncOpenAI()
TOOLS = openai_tool_schemas()


async def think(agent: Agent, prompt: str) -> None:
    messages: list[dict] = [{"role": "system", "content": companion.SYSTEM}, {"role": "user", "content": prompt}]
    extra = {"reasoning_effort": EFFORT} if EFFORT else {}
    for _ in range(companion.MAX_STEPS):
        response = await client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, **extra)
        choice = response.choices[0]
        message = choice.message
        if message.refusal or choice.finish_reason == "content_filter":
            companion.log.warning("the model declined this turn: %s", message.refusal or choice.finish_reason)
            return
        calls = message.tool_calls or []
        messages.append({
            "role": "assistant",
            "content": message.content,
            **({"tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in calls]} if calls else {}),
        })
        if not calls:
            return
        # Every tool call needs a matching tool message, even ones we can't run.
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                text = "That didn't work (bad_request): the arguments were not valid JSON."
            else:
                text = await call_tool(agent, call.function.name, args)
            companion.log.info("%s(%s) -> %s", call.function.name, call.function.arguments, text.splitlines()[0])
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})


if __name__ == "__main__":
    companion.run(think, errors=(openai.APIError,))

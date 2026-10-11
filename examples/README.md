# Examples

Start a server first (from the repo root; see [docs/running.md](../docs/running.md) for details):

```bash
cp worlds/town/agents.example.yaml worlds/town/agents.yaml   # dev tokens: change-me-mia / change-me-otto
commonroom-server run --world worlds/town
```

Open http://127.0.0.1:8765/ to watch.

| Script | What it does |
|--------|--------------|
| `greeter_bot.py` | Scripted. Greets arrivals, answers when its name is mentioned, thanks people for gifts. |
| `wanderer_bot.py` | Scripted. Walks between rooms, stands near things, fiddles with objects. |
| `claude_companion.py` | An LLM companion on Claude (Anthropic Messages API). |
| `openai_companion.py` | The same companion on an OpenAI model (Chat Completions API), or on a local model through any OpenAI-compatible server (Ollama, LM Studio, vLLM). |
| `companion.py` | Shared code for the two LLM companions (not run directly). |

```bash
COMMONROOM_TOKEN=change-me-otto python examples/greeter_bot.py
COMMONROOM_TOKEN=change-me-mia  python examples/wanderer_bot.py
```

## LLM companions

Both companions behave the same way; only the model call differs.
`companion.py` connects to the world and waits until something happens
nearby (someone speaks, arrives or hands it something), or until it has been quiet
for `COMPANION_IDLE` seconds. It then waits for the room to settle, shows
"typing", and calls the example's `think()`. `think()` sends the perception
text (`agent.describe()`) to the model with the SDK's tools and runs the
tool calls with `commonroom.call_tool` until the model stops.
`call_tool` makes `say` polite: if someone spoke while the model was
thinking, the line is bounced and the model is shown what it missed.

```bash
# Claude
pip install anthropic
export ANTHROPIC_API_KEY=...        # or `ant auth login`
COMMONROOM_TOKEN=change-me-mia COMPANION_PERSONA="You love the garden and talk about birds." \
  python examples/claude_companion.py

# OpenAI
pip install openai
export OPENAI_API_KEY=...
COMMONROOM_TOKEN=change-me-otto COMPANION_PERSONA="You are always making tea." \
  python examples/openai_companion.py

# A local model through Ollama (no key, no cost)
OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama COMPANION_MODEL=qwen3 \
  COMMONROOM_TOKEN=change-me-otto python examples/openai_companion.py
```

| Variable | Claude | OpenAI |
|----------|--------|--------|
| `COMPANION_MODEL` | `claude-opus-5-5` | `gpt-5.5` (any model with tool calling) |
| `COMPANION_EFFORT` | `low` (`low`/`medium`/`high`) | unset; `reasoning_effort` for reasoning models only |
| `COMPANION_PERSONA` | who the companion is | same |
| `COMPANION_IDLE` | seconds of quiet before acting alone (180; 0 = never) | same |
| `COMMONROOM_URL`, `COMMONROOM_TOKEN` | server address and token | same |
| `OPENAI_BASE_URL` | | an OpenAI-compatible server for local models |

`claude_companion.py` turns on the API's server-side refusal fallback
(`fallbacks: "default"`), so a turn a safety classifier declines is retried
on a fallback model instead of the companion going silent. Remove those two
lines from `think()` if you don't want that.

To support another provider, copy `openai_companion.py` and replace
`think()`: send the prompt with tools, run each tool call through
`call_tool`, send the results back, and stop when the model makes no more
calls.

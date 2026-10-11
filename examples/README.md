# Examples

Start a server first (from the repo root):

```bash
cp worlds/house/agents.example.yaml worlds/house/agents.yaml   # dev tokens: change-me-mia / change-me-otto
commonroom-server run --world worlds/house
```

Open http://127.0.0.1:8765/ to watch.

| Script | What it does |
|--------|--------------|
| `greeter_bot.py` | Scripted. Greets arrivals, answers when its name is mentioned, thanks people for gifts. |
| `wanderer_bot.py` | Scripted. Walks between rooms, stands near things, fiddles with objects. |
| `claude_companion.py` | Claude decides what to do, using the SDK's tools. Wakes when something happens nearby (or after a quiet spell), waits for the room to settle, shows "typing", then acts. Uses polite `say` so it won't talk over anyone. |

```bash
COMMONROOM_TOKEN=change-me-otto python examples/greeter_bot.py
COMMONROOM_TOKEN=change-me-mia  python examples/wanderer_bot.py

pip install anthropic
export ANTHROPIC_API_KEY=...        # or `ant auth login`
COMMONROOM_TOKEN=change-me-mia COMPANION_PERSONA="You love the garden and talk about birds." \
  python examples/claude_companion.py
```

`claude_companion.py` uses `claude-opus-5-5` at `low` effort by default
(change with `COMPANION_MODEL` / `COMPANION_EFFORT`). It turns on the API's
server-side refusal fallback (`fallbacks: "default"`), so a turn a safety
classifier declines is retried on a fallback model instead of the
companion going silent. Remove those two lines from `think()` if you don't
want that.

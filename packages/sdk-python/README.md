# commonroom (Python SDK)

Connect an AI companion to a commonroom world.

```bash
pip install -e packages/protocol -e packages/sdk-python
```

```python
from commonroom import Agent

agent = Agent("ws://127.0.0.1:8765", token="your-secret-token")

@agent.on_ready
async def ready(agent):
    print(agent.describe())          # plain-text perception, ready for an LLM prompt

@agent.on("speech")
async def heard(event, agent):
    if event["speaker"] != agent.me:
        await agent.say(f"Hi {event['speaker_name']}!", polite=True)

agent.run()
```

Pass the server address and the SDK adds the protocol path (`/v1/ws`). It
offers every protocol version it supports during `register`; the
negotiated one is in `agent.protocol`.

## Acting

All intents are `async` and return a `Result` (truthy when it worked).
Failures are normal, because another AI may have acted first, so they are returned,
not raised: check `result.reason` and `result.message`.

| Method | |
|--------|--|
| `move(to=None, near=None)` | exit direction / adjacent room, and/or stand near a person or object |
| `say(text, polite=False)` | `polite=True` refuses to talk over someone who spoke since you last listened |
| `whisper(to, text)`, `shout(text)` | |
| `interact(object, verb, target=None)` | e.g. `interact("chest", "unlock")` |
| `pick_up(object)`, `put_down(object, into=None)`, `give(object, to)` | |
| `look(at=None)` | |
| `typing(active=True)` | show the room you're composing a reply |
| `request(intent, **fields)` | any intent, including ones added in newer protocol minors |

## Perceiving

- `agent.view` is a `Perception` kept up to date from the event stream:
  `room`, `me`, `occupants`, `objects`, `holding`, `clock`,
  `last_speech_seq`, `typing`, `history`.
- `agent.describe()` renders it as text.
- Handlers: `@agent.on("speech")`, `@agent.on("*")`, `@agent.on_ready`.
  They run one at a time in arrival order. Long-running work (an LLM loop)
  belongs in its own task; see `examples/claude_companion.py`.

## LLM tools

`tool_schemas()` returns tool definitions (`name`, `description`,
`input_schema`) for every action; `await call_tool(agent, name, input)` runs
one and returns a short text result for the model.

The SDK reconnects with backoff when the connection drops. A bad token or
protocol mismatch raises `FatalError`.

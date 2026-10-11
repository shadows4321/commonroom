"""A scripted bot that wanders from room to room and fiddles with things.

    COMMONROOM_TOKEN=change-me-mia python examples/wanderer_bot.py
"""

import asyncio
import os
import random

from commonroom import Agent

agent = Agent(os.environ.get("COMMONROOM_URL", "ws://127.0.0.1:8765"), os.environ["COMMONROOM_TOKEN"])


async def wander(agent: Agent) -> None:
    while True:
        await asyncio.sleep(random.uniform(8, 15))
        view = agent.view
        things = list(view.objects.values())
        choice = random.random()
        if choice < 0.4 and view.room.get("exits"):
            exit_ = random.choice(view.room["exits"])
            result = await agent.move(exit_["direction"])
            print(f"walk {exit_['direction']}: {'ok' if result else result.message}")
        elif choice < 0.7 and things:
            obj = random.choice(things)
            if obj["verbs"]:
                verb = random.choice(obj["verbs"])["verb"]
                result = await agent.interact(obj["id"], verb)
                print(f"{verb} {obj['name']}: {result.data.get('message') if result else result.message}")
        elif choice < 0.85 and things:
            obj = random.choice(things)
            await agent.move(near=obj["id"])
        else:
            await agent.say(random.choice(["Lovely day.", "Hmm.", "What's that noise?", "I like it here."]))


@agent.on_ready
async def ready(agent):
    print(f"Connected as {agent.me}")
    # Handlers run one at a time, so long-running loops go in their own task.
    if not getattr(agent, "_wandering", None):
        agent._wandering = asyncio.create_task(wander(agent))


agent.run()

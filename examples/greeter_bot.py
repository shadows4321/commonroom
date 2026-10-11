"""A scripted bot: greets people who arrive and answers when spoken to by name.

    COMMONROOM_TOKEN=change-me-otto python examples/greeter_bot.py
"""

import os

from commonroom import Agent

agent = Agent(os.environ.get("COMMONROOM_URL", "ws://127.0.0.1:8765"), os.environ["COMMONROOM_TOKEN"])


@agent.on_ready
async def ready(agent):
    print(f"Connected to {agent.world_name} as {agent.me} (protocol {agent.protocol})")
    print(agent.describe())


@agent.on("entered_room")
async def welcome(event, agent):
    if event["entity"] != agent.me and event["room"] == agent.view.room["id"]:
        await agent.say(f"Hello {event['entity_name']}! Welcome to the {agent.view.room['name']}.")


@agent.on("speech")
async def reply(event, agent):
    if event["speaker"] == agent.me or event.get("text") is None:
        return
    if event["mode"] == "whisper":
        await agent.whisper(event["speaker"], "Your secret is safe with me.")
    elif agent.view.me["name"].lower() in event["text"].lower():
        # Only answer if nobody else has spoken since that line.
        await agent.say(f"You called, {event['speaker_name']}?", after_seq=event.get("seq"))


@agent.on("object_changed")
async def thanks(event, agent):
    if event["change"] == "given" and event.get("recipient") == agent.me:
        await agent.say(f"Thank you for the {event['object']['name']}, {event['actor_name']}!")


agent.run()

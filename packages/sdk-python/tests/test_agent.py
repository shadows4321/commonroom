import asyncio

import pytest

from commonroom import Agent, FatalError, call_tool, tool_schemas
from commonroom_protocol import PROTOCOL_VERSION


async def eventually(check, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@pytest.fixture
async def pair(server):
    mia, otto = Agent(server, "tok-mia"), Agent(server, "tok-otto")
    await mia.start()
    await otto.start()
    yield mia, otto
    await mia.close()
    await otto.close()


async def test_connects_and_negotiates(pair):
    mia, _ = pair
    assert mia.me == "mia"
    assert mia.protocol == PROTOCOL_VERSION
    assert mia.view.room["id"] == "hall"


async def test_bad_token_is_fatal(server):
    agent = Agent(server, "wrong")
    with pytest.raises(FatalError):
        await agent.start()


async def test_handlers_and_speech(pair):
    mia, otto = pair
    heard = []

    @otto.on("speech")
    async def on_speech(event, agent):
        heard.append(event["text"])
        if event["speaker"] != agent.me:
            await agent.say(f"hi {event['speaker_name']}")

    assert await mia.say("hello")
    await eventually(lambda: "hi Mia" in heard)
    await eventually(lambda: mia.view.last_speech_seq == 2)


async def test_polite_say_reports_what_was_missed(pair):
    mia, otto = pair
    mia.view.last_speech_seq = 0
    assert await otto.say("me first")
    await asyncio.sleep(0.05)
    mia.view.last_speech_seq = 0  # pretend mia's LLM started composing before otto spoke
    result = await mia.say("hello", polite=True)
    assert not result and result.reason == "conversation_moved_on"
    assert result.details["missed"][0]["text"] == "me first"


async def test_view_tracks_objects_and_people(pair):
    mia, otto = pair
    assert await mia.interact("box", "open")
    assert await mia.pick_up("key")
    assert "key" in mia.view.holding
    await eventually(lambda: any(h["id"] == "key" for h in otto.view.occupants["mia"]["holding"]))
    assert await mia.give("key", "Otto")
    await eventually(lambda: "key" in otto.view.holding)
    assert "key" not in mia.view.holding

    assert await mia.move("north")
    assert mia.view.room["id"] == "kitchen"
    await eventually(lambda: "mia" not in otto.view.occupants)
    failed = await mia.pick_up("statue")
    assert not failed and failed.reason == "not_here"


async def test_describe_and_tools(pair):
    mia, _ = pair
    text = mia.describe()
    assert "You are Mia, in the Hall" in text and "Otto [otto]" in text and "box [box]" in text
    names = {t["name"] for t in tool_schemas()}
    assert {"move", "say", "interact", "pick_up", "give", "look"} <= names
    out = await call_tool(mia, "interact", {"object": "box", "verb": "open"})
    assert "opens the box" in out
    out = await call_tool(mia, "pick_up", {"object": "statue"})
    assert out.startswith("That didn't work (not_portable)")
    assert "You are Mia, in the Kitchen" in await call_tool(mia, "move", {"to": "north"})


async def test_on_ready_can_await_intents(server):
    agent = Agent(server, "tok-mia")
    results = []

    @agent.on_ready
    async def ready(agent):
        results.append(await agent.say("hello from on_ready"))

    await agent.start()
    await eventually(lambda: results)
    assert results[0].ok, results[0]
    await agent.close()

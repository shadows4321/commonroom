import asyncio

import pytest

from commonroom import (
    Agent, AvatarUploadError, FatalError, call_tool, is_failure, openai_tool_schemas, tool_schemas,
)
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
    assert mia.connected


async def test_connected_follows_the_connection(server):
    agent = Agent(server, "tok-mia", reconnect=False)
    assert not agent.connected
    await agent.start()
    assert agent.connected
    await agent.close()
    assert not agent.connected


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
    mia.describe()  # mia's model reads the room...
    assert await otto.say("me first")  # ...otto speaks while it is still thinking...
    await eventually(lambda: mia.view.last_speech_seq == 1)
    result = await mia.say("hello", polite=True)  # ...so its answer is bounced
    assert not result and result.reason == "conversation_moved_on"
    assert result.details["missed"][0]["text"] == "me first"
    assert await mia.say("oh, hi otto", polite=True)  # the missed line now counts as heard


async def test_describe_without_hearing_keeps_say_polite(pair):
    mia, otto = pair
    mia.describe()  # the town model reads the room...
    assert await otto.say("me first")
    await eventually(lambda: mia.view.last_speech_seq == 1)
    assert "me first" in mia.describe(hear=False)  # ...a chat channel is shown it too...
    result = await mia.say("hello", polite=True)  # ...but the town model still hasn't heard it
    assert not result and result.reason == "conversation_moved_on"


async def test_after_seq_answers_a_specific_line(pair):
    mia, otto = pair
    assert await otto.say("anyone there?")
    assert await otto.say("hello??")
    assert not await mia.say("yes!", after_seq=1)  # a reply to line 1, but line 2 came since
    assert await mia.say("yes, hi!", after_seq=2)


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


async def test_view_tracks_poses(pair):
    mia, otto = pair
    assert await mia.move("east")
    assert await otto.move("east")
    assert await mia.interact("bench", "lie")
    await eventually(lambda: (mia.view.me.get("pose") or {}).get("kind") == "lying")
    await eventually(lambda: otto.view.occupants["mia"].get("pose") is not None)
    assert "(lying on the bench)" in mia.describe()
    assert "Mia [mia] (sitting" not in otto.describe() and "(lying on the bench" in otto.describe()
    assert await mia.move(near="lamp")
    await eventually(lambda: otto.view.occupants["mia"].get("pose") is None)
    assert mia.view.me["pose"] is None and "(near the lamp)" in mia.describe()


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


async def test_map_and_go_to(pair):
    mia, otto = pair
    assert await mia.move(to="north")
    result = await mia.map(to="Study")
    assert [s["to"] for s in result.data["route"]] == ["hall", "study"]
    text = await call_tool(mia, "map", {"to": "study"})
    assert "Kitchen (you are here): south to Hall" in text
    assert "Way to Study, 2 steps: south (Hall) -> east (Study)." in text
    passed = []
    otto.on("entered_room")(lambda ev, agent: passed.append(ev["entity"]))
    out = await call_tool(mia, "go_to", {"place": "study", "near": "lamp"})
    assert "You are Mia, in the Study" in out and "(near the lamp)" in out
    await eventually(lambda: "mia" in passed)  # otto, in the hall, saw her walk through
    assert (await mia.go_to("study")).data["already_there"]
    failed = await mia.go_to("the moon")
    assert not failed.ok and failed.reason == "no_such_place"


def test_world_changed_refreshes_the_view_but_keeps_what_was_heard():
    from commonroom import Perception, format_event

    view = Perception()
    snap = {"you": {"id": "mia", "name": "Mia", "holding": []}, "occupants": [], "objects": [],
            "room": {"id": "street", "name": "Street", "exits": []}, "clock": {"label": "08:00"},
            "last_speech_seq": 3, "recent_events": [{"type": "speech", "speaker": "otto", "text": "hi"}]}
    view.load(snap)
    view.mark_heard(3)
    new_room = {**snap["room"], "exits": [{"direction": "Kai's house", "to": "kai.garden", "to_name": "Kai's Garden"}]}
    ev = {"type": "world_changed", "room": None, "message": "Kai's house has been built.",
          "snapshot": {**snap, "room": new_room, "recent_events": []}}
    view.apply(ev)
    assert view.room["exits"][0]["to"] == "kai.garden"
    assert [e["type"] for e in view.history] == ["speech", "world_changed"] and view.heard_seq == 3
    assert format_event(ev) == "Kai's house has been built."


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


def test_openai_tool_schemas_mirror_anthropic_ones():
    anthropic_tools, openai_tools = tool_schemas(), openai_tool_schemas()
    assert len(openai_tools) == len(anthropic_tools)
    for a, o in zip(anthropic_tools, openai_tools):
        assert o["type"] == "function"
        assert o["function"] == {"name": a["name"], "description": a["description"], "parameters": a["input_schema"]}


async def test_tool_prefix(pair):
    mia, _ = pair
    names = {t["name"] for t in tool_schemas()}
    assert {t["name"] for t in tool_schemas("town_")} == {"town_" + n for n in names}
    assert {t["function"]["name"] for t in openai_tool_schemas("town_")} == {"town_" + n for n in names}
    assert "You are Mia" in await call_tool(mia, "town_look", {}, prefix="town_")
    assert "unknown_tool" in await call_tool(mia, "look", {}, prefix="town_")  # the bare name isn't a tool
    assert "'town_dance'" in await call_tool(mia, "town_dance", {}, prefix="town_")


async def test_call_tool_is_polite_and_reports_bad_calls(pair):
    mia, otto = pair
    mia.describe()
    assert await otto.say("me first")
    await eventually(lambda: mia.view.last_speech_seq == 1)
    out = await call_tool(mia, "say", {"text": "hello"})
    assert is_failure(out) and "conversation_moved_on" in out and 'Otto: "me first"' in out
    assert not is_failure(await call_tool(mia, "say", {"text": "hello"}))
    assert "unknown_tool" in await call_tool(mia, "dance", {})
    assert "bad_request" in await call_tool(mia, "say", {})
    assert "bad_request" in await call_tool(mia, "give", {"object": "box", "bogus": 1})
    assert "bad_request" in await call_tool(mia, "look", ["not", "a", "dict"])


async def test_set_avatar_updates_everyone(pair, tmp_path):
    from avatar_files import model, png

    mia, otto = pair
    (tmp_path / "mia.vrm").write_bytes(model(title="Mia v1"))
    avatar = await mia.set_avatar(str(tmp_path / "mia.vrm"))
    assert avatar["kind"] == "vrm" and avatar["meta"]["title"] == "Mia v1"
    await eventually(lambda: (otto.view.occupants["mia"].get("avatar") or {}).get("kind") == "vrm")
    await eventually(lambda: (mia.view.me.get("avatar") or {}).get("kind") == "vrm")

    (tmp_path / "bad.png").write_bytes(png(5000, 10))
    with pytest.raises(AvatarUploadError) as exc:
        await mia.set_avatar(str(tmp_path / "bad.png"))
    assert exc.value.reason == "texture_too_large" and exc.value.status == 422

    await mia.remove_avatar()
    await eventually(lambda: otto.view.occupants["mia"].get("avatar") is None)


async def test_avatar_command_line(server, tmp_path, capsys):
    from avatar_files import png
    from commonroom.__main__ import main

    (tmp_path / "me.png").write_bytes(png())
    # The CLI blocks, and the test server runs on this event loop, so run the CLI in a thread.
    await asyncio.to_thread(main, ["avatar", str(tmp_path / "me.png"), "--server", server, "--token", "tok-otto"])
    assert "uploaded png avatar" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="upload failed"):
        await asyncio.to_thread(main, ["avatar", str(tmp_path / "me.png"), "--server", server, "--token", "wrong"])


def test_http_base():
    from commonroom.avatar import http_base
    assert http_base("ws://h:8765") == "http://h:8765"
    assert http_base("wss://world.example.com/v1/ws") == "https://world.example.com"
    assert http_base("wss://example.com/world/v1/ws") == "https://example.com/world"


def test_guests_are_described_as_people():
    from commonroom import Perception, format_event

    view = Perception()
    view.load({"you": {"id": "mia", "name": "Mia", "holding": []}, "objects": [], "recent_events": [],
               "room": {"id": "hall", "name": "Hall", "description": "", "exits": []}, "clock": {"label": "08:00"},
               "last_speech_seq": 0,
               "occupants": [{"id": "guest-bob", "name": "Bob", "kind": "guest", "online": True, "holding": []}]})
    assert "Bob [guest-bob] (a human guest, not an AI)" in view.describe()
    assert format_event({"type": "left_room", "entity": "guest-bob", "entity_name": "Bob", "to": None,
                         "to_name": None, "gone": True}) == "Bob went home."

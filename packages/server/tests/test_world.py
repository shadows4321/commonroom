import pytest

from commonroom_protocol import Reason
from commonroom_server.world import Failure


def act(world, who, type_, **args):
    result = world.handle(who, {"type": type_, **args})
    return result, world.commit()


def fails(world, who, type_, reason, **args):
    with pytest.raises(Failure) as exc:
        world.handle(who, {"type": type_, **args})
    assert exc.value.reason == reason, exc.value.message
    assert not world.commit(), "a failed intent must not change anything"
    return exc.value


def test_move_by_direction_and_room_name(world):
    result, batch = act(world, "mia", "move", to="north")
    assert result["room"] == "kitchen"
    assert [e.type for e in batch.events] == ["left_room", "entered_room", "object_changed"]  # the floor grid
    left, entered, _ = batch.events
    assert left.audience == {"mia", "otto"}  # everyone who was in the hall, including the mover
    assert entered.audience == {"mia"}
    act(world, "mia", "move", to="Hall")
    assert world.entities["mia"].room == "hall"


def test_move_rejects_non_adjacent(world):
    f = fails(world, "mia", "move", Reason.NO_SUCH_EXIT, to="nowhere")
    assert {"direction": "north", "to": "kitchen"} in f.details["exits"]


def test_move_near(world):
    result, batch = act(world, "mia", "move", near="statue")
    assert result["near"]["id"] == "statue"
    assert batch.events[0].type == "position_changed"
    act(world, "otto", "move", near="Mia")
    assert world.entity_view(world.entities["otto"])["near"]["name"] == "Mia"
    fails(world, "mia", "move", Reason.NOT_HERE, near="lamp")  # lamp is in the study


def test_say_reaches_room_only(world):
    _, batch = act(world, "mia", "say", text="hello")
    ev = batch.events[0]
    assert ev.audience == {"mia", "otto"}
    assert ev.payload["seq"] == 1


def test_say_after_seq_detects_interruption(world):
    act(world, "otto", "say", text="first")
    f = fails(world, "mia", "say", Reason.CONVERSATION_MOVED_ON, text="me too", after_seq=0)
    assert [m["text"] for m in f.details["missed"]] == ["first"]
    result, _ = act(world, "mia", "say", text="me too", after_seq=1)
    assert result["seq"] == 2


def test_whisper_is_redacted_for_others(world):
    fails(world, "mia", "whisper", Reason.NOT_HERE, to="kai", text="hi")  # kai is in the study
    act(world, "kai", "move", to="west")
    _, batch = act(world, "mia", "whisper", to="Otto", text="secret")
    ev = batch.events[0]
    assert ev.audience == {"mia", "otto", "kai"}
    assert ev.to_wire("otto")["text"] == "secret"
    assert ev.to_wire("mia")["text"] == "secret"
    assert ev.to_wire("kai")["text"] is None and ev.to_wire("kai")["redacted"]


def test_shout_reaches_adjacent_rooms_muffled(world):
    _, batch = act(world, "kai", "shout", text="dinner!")
    rooms = {e.room: e.payload for e in batch.events}
    assert set(rooms) == {"study", "hall"}
    assert rooms["hall"]["muffled"] is True
    assert batch.events[1].audience == {"mia", "otto"}


def test_pick_up_first_request_wins(world):
    act(world, "mia", "move", to="north")
    act(world, "otto", "move", to="north")
    act(world, "mia", "pick_up", object="apple")
    f = fails(world, "otto", "pick_up", Reason.NOT_HERE, object="apple")
    assert f.details["holder"] == "mia"
    assert world.objects["apple"].loc_kind == "entity"


def test_carry_limit(world):
    act(world, "mia", "move", to="north")
    act(world, "mia", "pick_up", object="apple")
    act(world, "mia", "pick_up", object="pear")
    fails(world, "mia", "pick_up", Reason.HANDS_FULL, object="plum")


def test_not_portable(world):
    fails(world, "mia", "pick_up", Reason.NOT_PORTABLE, object="statue")


def test_container_closed_then_open(world):
    fails(world, "mia", "pick_up", Reason.CONTAINER_CLOSED, object="key")
    act(world, "mia", "interact", object="box", verb="open")
    act(world, "mia", "pick_up", object="key")
    assert world.objects["key"].loc_id == "mia"


def test_put_into_container_and_no_nesting_in_itself(world):
    act(world, "mia", "interact", object="box", verb="open")
    act(world, "mia", "pick_up", object="box")
    fails(world, "mia", "put_down", Reason.WOULD_NEST, object="box", into="box")
    act(world, "mia", "put_down", object="box")
    act(world, "mia", "pick_up", object="key")
    act(world, "mia", "put_down", object="key", into="box")
    assert (world.objects["key"].loc_kind, world.objects["key"].loc_id) == ("container", "box")


def test_give(world):
    act(world, "mia", "interact", object="box", verb="open")
    act(world, "mia", "pick_up", object="key")
    _, batch = act(world, "mia", "give", object="key", to="otto")
    assert world.objects["key"].loc_id == "otto"
    assert batch.events[0].payload["change"] == "given"
    fails(world, "mia", "give", Reason.NOT_HERE, object="key", to="otto")  # Otto has it now
    fails(world, "otto", "give", Reason.NOT_HERE, object="key", to="kai")


def test_interact_requires_and_needs(world):
    act(world, "kai", "interact", object="lamp", verb="switch_on")
    f = fails(world, "kai", "interact", Reason.NOT_NOW, object="lamp", verb="switch_on")
    assert f.message == "Already on."
    assert "It glows." in world.describe_object(world.objects["lamp"])
    fails(world, "kai", "interact", Reason.UNKNOWN_VERB, object="lamp", verb="eat")
    fails(world, "kai", "interact", Reason.NEEDS_ITEM, object="safe", verb="unlock")

    act(world, "mia", "interact", object="box", verb="open")
    act(world, "mia", "pick_up", object="key")
    act(world, "mia", "move", to="east")
    result, _ = act(world, "mia", "interact", object="safe", verb="unlock")
    assert result["message"] == "Mia unlocks the safe with the key."
    assert result["reveal"] == "Inside is a note."


def test_objects_never_duplicate(world):
    act(world, "mia", "interact", object="box", verb="open")
    act(world, "mia", "pick_up", object="key")
    act(world, "mia", "give", object="key", to="otto")
    act(world, "otto", "put_down", object="key", into="box")
    locations = [(o.loc_kind, o.loc_id) for o in world.objects.values() if o.id == "key"]
    assert locations == [("container", "box")]
    assert len(world.objects) == 11


def test_sit_and_lie_on_furniture(world):
    for who in ("mia", "otto"):
        act(world, who, "move", to="study")
    result, batch = act(world, "kai", "interact", object="bench", verb="sit")
    kai = world.entity_view(world.entities["kai"])
    assert kai["pose"] == {"kind": "sitting", "object": "bench", "name": "bench", "seat": 0}
    assert kai["near"]["id"] == "bench"
    ev = batch.events[0]
    assert ev.payload["pose"] == kai["pose"] and ev.payload["message"] == "Kai sits on the bench."
    assert "kai" in batch.entities
    act(world, "mia", "interact", object="bench", verb="lie")
    assert world.entities["mia"].pose == {"kind": "lying", "object": "bench", "seat": 1}
    f = fails(world, "otto", "interact", Reason.OCCUPIED, object="bench", verb="sit")
    assert f.message == "There's no room on the bench: Kai and Mia are already there."
    act(world, "mia", "interact", object="bench", verb="sit")  # changing pose keeps the seat
    assert world.entities["mia"].pose["seat"] == 1
    fails(world, "otto", "interact", Reason.NOT_NOW, object="bench", verb="stand")
    act(world, "kai", "interact", object="bench", verb="stand")
    assert world.entities["kai"].pose is None and world.entities["kai"].near == "bench"
    act(world, "otto", "interact", object="bench", verb="sit")
    assert world.entities["otto"].pose["seat"] == 0


def test_moving_stands_you_up(world):
    act(world, "kai", "interact", object="bench", verb="sit")
    _, batch = act(world, "kai", "move", near="lamp")
    assert world.entities["kai"].pose is None
    assert batch.events[0].payload["pose"] is None
    act(world, "kai", "interact", object="bench", verb="sit")
    act(world, "kai", "move", to="hall")
    assert world.entity_view(world.entities["kai"])["pose"] is None


def test_cant_pick_up_what_someone_sits_on(world):
    act(world, "mia", "move", to="study")
    act(world, "kai", "interact", object="stool", verb="sit")
    f = fails(world, "mia", "pick_up", Reason.OCCUPIED, object="stool")
    assert f.message == "Kai is on the stool."
    act(world, "kai", "move", near="lamp")
    act(world, "mia", "pick_up", object="stool")
    fails(world, "mia", "interact", Reason.NOT_NOW, object="stool", verb="sit")  # put it down first


def test_map_lists_every_room_and_finds_the_way(world):
    result, batch = act(world, "mia", "map")
    assert not batch, "looking at the map changes nothing"
    m = result["map"]
    assert m["here"] == "hall" and {r["id"] for r in m["rooms"]} == {"hall", "kitchen", "study"}
    assert m["areas"][0]["id"] == "town" and set(m["areas"][0]["rooms"]) == {"hall", "kitchen", "study"}
    assert "route" not in result
    act(world, "mia", "move", to="north")
    result, _ = act(world, "mia", "map", to="Study")
    assert result["destination"] == {"id": "study", "name": "Study"}
    assert [(s["direction"], s["to"]) for s in result["route"]] == [("south", "hall"), ("east", "study")]
    assert act(world, "mia", "map", to="kitchen")[0]["route"] == []
    f = fails(world, "mia", "map", Reason.NO_SUCH_PLACE, to="the moon")
    assert "Study" in f.details["places"]
    fails(world, "mia", "map", Reason.BAD_REQUEST, to=5)


def test_route_is_none_when_unreachable(world):
    world.rooms["study"].exits.clear()
    world.rooms["hall"].exits.pop("east")
    assert world.route("hall", "study") is None


def test_bad_arguments(world):
    fails(world, "mia", "move", Reason.BAD_REQUEST, bogus=1)
    fails(world, "mia", "dance", Reason.UNKNOWN_INTENT)
    fails(world, "mia", "say", Reason.BAD_REQUEST, text="  ")
    fails(world, "mia", "say", Reason.TEXT_TOO_LONG, text="x" * 5000)


def test_snapshot_contents(world):
    act(world, "otto", "say", text="hi")
    snap = world.snapshot("mia")
    assert snap["room"]["id"] == "hall"
    assert {o["id"] for o in snap["occupants"]} == {"otto"}
    assert {o["id"] for o in snap["objects"]} == {"box", "statue"}
    box = next(o for o in snap["objects"] if o["id"] == "box")
    assert box["container"]["open"] is False and "contents" not in box
    assert [v["verb"] for v in box["verbs"]] == ["open"]
    assert snap["last_speech_seq"] == 1
    assert any(e["type"] == "speech" for e in snap["recent_events"])
    assert snap["clock"]["time"] == "06:00"


def test_clock_ticks_schedules_and_phase(world):
    act(world, "kai", "interact", object="lamp", verb="switch_on")
    world.advance_clock(30)  # 06:30
    batch = world.commit()
    assert [e.type for e in batch.events] == ["time_tick"]
    world.advance_clock(31)  # 07:01, crosses 07:00
    batch = world.commit()
    types = [e.type for e in batch.events]
    assert "object_changed" in types
    assert world.objects["lamp"].state["on"] is False
    tick = next(e for e in batch.events if e.type == "time_tick")
    assert tick.payload["clock"]["phase"] == "morning" and tick.payload["phase_changed"]
    assert "Sunny." in world.room_view("kitchen")["description"]


def test_on_enter_and_on_empty(world):
    _, batch = act(world, "mia", "move", to="north")
    assert [e.type for e in batch.events] == ["left_room", "entered_room", "object_changed"]
    lit = batch.events[-1]
    assert world.objects["grid"].state["lit"] is True
    assert lit.payload["message"] == "The floor grid lights up under Mia's feet."
    assert lit.payload["actor"] is None and lit.payload["changed"] == {"lit": True}
    assert lit.audience == {"mia"}

    _, batch = act(world, "otto", "move", to="north")  # requires lit: false, so nothing again
    assert [e.type for e in batch.events] == ["left_room", "entered_room"]

    act(world, "otto", "move", to="south")  # Mia is still there
    assert world.objects["grid"].state["lit"] is True
    _, batch = act(world, "mia", "move", to="south")
    dark = batch.events[-1]
    assert dark.type == "object_changed" and dark.room == "kitchen" and dark.audience == set()
    assert world.objects["grid"].state["lit"] is False


def test_on_enter_by_teleport_and_on_empty_when_a_guest_leaves(world):
    world.teleport("kai", "kitchen")
    world.commit()
    assert world.objects["grid"].state["lit"] is True
    world.remove_entity("kai")
    assert world.commit().events[-1].type == "object_changed"
    assert world.objects["grid"].state["lit"] is False


def test_clock_pauses_when_empty(world):
    for eid in list(world.entities):
        world.disconnect(eid)
    before = world.clock.minutes
    world.advance_clock(100)
    assert world.clock.minutes == before

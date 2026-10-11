"""Towns (world format 2): houses in their own files, the .glb importer, and serving 3D files."""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from commonroom_protocol import API_PREFIX
from commonroom_server.app import create_app
from commonroom_server.cli import main as cli
from commonroom_server.config import AgentsConfig, AgentSpec, ConfigError, load_world
from commonroom_server.scene_import import read_scene
from commonroom_server.world import World

REPO = Path(__file__).resolve().parents[3]
TOWN = REPO / "worlds" / "town"


def _blockout():
    spec = importlib.util.spec_from_file_location("make_blockout", REPO / "tools" / "make_blockout.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_blockout"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def town_dir(tmp_path):
    """A fresh copy of worlds/town to modify."""
    target = tmp_path / "town"
    shutil.copytree(TOWN, target)
    return target


# --------------------------------------------------------------- loading

def test_town_loads_and_links_houses_to_streets():
    cfg = load_world(TOWN)
    assert cfg.rooms["mia.garden"].exits["out"] == "street_west"
    assert cfg.rooms["street_west"].exits["Mia's house"] == "mia.garden"
    assert cfg.rooms["mia.kitchen"].name == "Mia's Kitchen" and cfg.rooms["mia.kitchen"].area == "mia"
    assert cfg.rooms["square"].area == "town"
    assert cfg.homes == {"mia": "mia.garden", "otto": "otto.garden"}
    assert {o.id for o in cfg.objects} >= {"mia.kettle", "otto.kettle", "fountain"}


def test_agents_start_at_home():
    w = World(load_world(TOWN))
    assert w.connect(AgentSpec("mia", "Mia", "t")).room == "mia.garden"
    assert w.connect(AgentSpec("kai", "Kai", "t2")).room == "square"  # no house: the town's start room
    assert w.connect(AgentSpec("lea", "Lea", "t3", start_room="cafe")).room == "cafe"


def test_walk_from_home_to_a_neighbour():
    w = World(load_world(TOWN))
    w.connect(AgentSpec("mia", "Mia", "t"))
    for step in ("out", "Otto's house", "house", "kitchen"):
        w.handle("mia", {"type": "move", "to": step})
    assert w.entities["mia"].room == "otto.kitchen"
    w.handle("mia", {"type": "interact", "object": "kettle", "verb": "fill"})
    assert w.objects["otto.kettle"].state["filled"] and not w.objects["mia.kettle"].state["filled"]


def test_map_finds_houses_by_owner_and_home():
    w = World(load_world(TOWN))
    w.connect(AgentSpec("mia", "Mia", "t"))
    w.connect(AgentSpec("otto", "Otto", "t2"))
    m = w.handle("mia", {"type": "map"})["map"]
    assert m["home"] == "mia.garden"
    assert {a["id"]: a["entrance"] for a in m["areas"]} == {"town": None, "mia": "mia.garden", "otto": "otto.garden"}
    for place in ("Otto", "otto", "Otto's house", "Otto's Garden"):
        result = w.handle("mia", {"type": "map", "to": place})
        assert result["destination"]["id"] == "otto.garden", place
        assert [s["to"] for s in result["route"]] == ["street_west", "otto.garden"]
    for step in w.handle("mia", {"type": "map", "to": "Otto's Kitchen"})["route"]:
        w.handle("mia", {"type": "move", "to": step["to"]})
    assert w.entities["mia"].room == "otto.kitchen"
    assert [s["to"] for s in w.handle("mia", {"type": "map", "to": "home"})["route"]][-1] == "mia.garden"


def test_house_validation(town_dir):
    house = yaml.safe_load((town_dir / "houses" / "mia.yaml").read_text())
    house["rooms"]["kitchen"]["exits"]["back"] = "@street"
    (town_dir / "houses" / "mia.yaml").write_text(yaml.safe_dump(house))
    with pytest.raises(ConfigError, match="exactly one exit"):
        load_world(town_dir)


def test_two_houses_on_one_plot(town_dir):
    world = yaml.safe_load((town_dir / "world.yaml").read_text())
    world["houses"][1]["plot"] = world["houses"][0]["plot"]
    (town_dir / "world.yaml").write_text(yaml.safe_dump(world))
    with pytest.raises(ConfigError, match="more than one house"):
        load_world(town_dir)


def test_format_1_worlds_still_load():
    assert load_world(REPO / "worlds" / "house").areas["town"].model is None


# ---------------------------------------------------------------- import

def test_read_scene_world_positions():
    nodes = {n.name: n for n in read_scene(TOWN / "town.glb").nodes}
    # plot:3 is at Blender (-48, -13) -> glTF (x, z) = (-48, 13)
    x, y, z = nodes["plot:3"].position
    assert (round(x), round(y), round(z)) == (-48, 0, 13)
    assert nodes["gate:3"].room == "street_west"
    assert nodes["exit:square:cafe"].room == "square"


def test_import_round_trip_keeps_hand_edits(town_dir):
    house_yaml = town_dir / "houses" / "mia.yaml"
    house = yaml.safe_load(house_yaml.read_text())
    house["rooms"]["kitchen"]["name"] = "Scullery"
    house["objects"] = [o for o in house["objects"] if o["id"] != "kettle"]  # pretend it's new
    house_yaml.write_text(yaml.safe_dump(house))

    cli(["import-glb", str(town_dir / "houses" / "mia.glb"), "--world", str(town_dir), "--owner", "mia"])
    house = yaml.safe_load(house_yaml.read_text())
    assert house["rooms"]["kitchen"]["name"] == "Scullery"
    assert next(o for o in house["objects"] if o["id"] == "kettle")["type"] == "kettle"
    assert next(o for o in house["objects"] if o["id"] == "rose_bush")["type"] == "plant"
    assert house["rooms"]["garden"]["exits"] == {"out": "@street", "house": "living_room"}
    load_world(town_dir)


def test_import_a_new_house_takes_a_free_plot(town_dir, capsys):
    shutil.copyfile(town_dir / "houses" / "mia.glb", town_dir / "kai.glb")
    cli(["import-glb", str(town_dir / "kai.glb"), "--world", str(town_dir), "--owner", "kai"])
    assert "copied" in capsys.readouterr().out
    world = yaml.safe_load((town_dir / "world.yaml").read_text())
    kai = next(h for h in world["houses"] if h["owner"] == "kai")
    assert kai["plot"] == "3" and kai["model"] == "houses/kai.glb"
    cfg = load_world(town_dir)
    assert cfg.rooms["street_west"].exits["Kai's house"] == "kai.garden"


def test_a_shared_house_belongs_to_no_one(town_dir, capsys):
    shutil.copyfile(town_dir / "houses" / "mia.glb", town_dir / "hall.glb")
    cli(["import-glb", str(town_dir / "hall.glb"), "--world", str(town_dir), "--shared", "hall", "--name", "Town Hall"])
    assert "the shared house 'hall'" in capsys.readouterr().out
    world = yaml.safe_load((town_dir / "world.yaml").read_text())
    hall = next(h for h in world["houses"] if h.get("id") == "hall")
    assert "owner" not in hall and hall["name"] == "Town Hall" and hall["plot"] == "3"
    cfg = load_world(town_dir)
    area = cfg.areas["hall"]
    assert (area.kind, area.owner, area.name, area.entrance) == ("house", None, "Town Hall", "hall.garden")
    assert cfg.rooms["street_west"].exits["Town Hall"] == "hall.garden"
    assert cfg.rooms["hall.kitchen"].name == "Kitchen" and cfg.rooms["hall.kitchen"].area == "hall"
    assert "hall" not in cfg.homes and "hall.kettle" in {o.id for o in cfg.objects}

    w = World(cfg)
    assert w.connect(AgentSpec("hall", "Hall", "t")).room == "square"  # nobody lives there
    w.connect(AgentSpec("mia", "Mia", "t2"))
    assert w.handle("mia", {"type": "map", "to": "Town Hall"})["destination"]["id"] == "hall.garden"
    assert next(a for a in w.world_state()["areas"] if a["id"] == "hall")["owner"] is None


def test_a_house_has_an_owner_or_an_id(town_dir):
    world = yaml.safe_load((town_dir / "world.yaml").read_text())
    world["houses"][0]["id"] = "club"
    (town_dir / "world.yaml").write_text(yaml.safe_dump(world))
    with pytest.raises(ConfigError, match="either an owner"):
        load_world(town_dir)
    with pytest.raises(SystemExit):
        cli(["import-glb", str(town_dir / "houses" / "mia.glb"), "--world", str(town_dir), "--owner", "mia",
             "--shared", "mia"])


def test_import_rejects_house_without_way_out(tmp_path):
    mb = _blockout()
    rooms = [n for n in mb.house((1, 1, 1)) if n.name != "room:garden"]
    mb.write_glb(tmp_path / "bad.glb", rooms)
    with pytest.raises(SystemExit, match="exactly one exit labelled 'out'"):
        cli(["import-glb", str(tmp_path / "bad.glb"), "--world", str(tmp_path), "--owner", "x"])


def test_import_warns_about_unpaired_exits(tmp_path, capsys):
    mb = _blockout()
    nodes = mb.house((1, 1, 1))
    nodes.append(mb.N("room:attic").add(mb.N("floor:attic", (0, 0, 5)), mb.N("exit:attic:ladder", (5, 5, 5))))
    mb.write_glb(tmp_path / "h.glb", nodes)
    (tmp_path / "world.yaml").write_text(yaml.safe_dump({"format_version": 2, "plots": {"1": {"street": "s"}}}))
    cli(["import-glb", str(tmp_path / "h.glb"), "--world", str(tmp_path), "--owner", "x"])
    assert "exit:attic:ladder has no exit marker" in capsys.readouterr().out


def test_check_reports_mistakes_and_writes_nothing(town_dir, capsys):
    mb = _blockout()
    nodes = mb.house((1, 1, 1))
    kitchen = next(n for n in nodes if n.name == "room:kitchen")
    kitchen.add(
        mb.N("obj:kettle_2").add(mb.N("when:on=true")),  # kettles are hot, not on
        mb.N("obj:street_lantern_1").add(mb.N("glow", light=(1, 1, 1, 544))),  # 10 W in Blender
        mb.N("spot:teapot"), mb.N("seat:kettle_2"), mb.N("flor:kitchen"), mb.N("time:noon"),
    )
    mb.write_glb(town_dir / "kai.glb", nodes)
    before = {p: p.read_bytes() for p in town_dir.rglob("*.yaml")}
    with pytest.raises(SystemExit, match="6 warnings"):
        cli(["import-glb", str(town_dir / "kai.glb"), "--world", str(town_dir), "--owner", "kai", "--check"])
    out = capsys.readouterr().out
    assert "when:on=true under obj:kettle_2: type 'kettle' has no state on (it has: filled, hot)" in out
    assert "glow is 544 cd" in out
    assert "spot:teapot has no matching obj:teapot" in out
    assert "obj:kettle_2 has seat: markers, but type 'kettle' has no verb with a pose" in out
    assert "flor:kitchen: unknown prefix 'flor:'" in out
    assert "time:noon: unknown time of day noon" in out
    assert {p: p.read_bytes() for p in town_dir.rglob("*.yaml")} == before
    assert not (town_dir / "houses" / "kai.glb").exists()

    cli(["import-glb", str(TOWN / "houses" / "mia.glb"), "--world", str(town_dir), "--owner", "mia", "--check"])
    assert "no problems found" in capsys.readouterr().out


def test_type_guesses():
    from commonroom_server.scene_import import guess_type
    types = {"kettle", "lantern", "lamp", "fixture"}
    assert guess_type("kettle", types) == "kettle"
    assert guess_type("big_kettle", types) == "kettle"
    assert guess_type("kettle_2", types) == "kettle"
    assert guess_type("street_lantern_1", types) == "lantern"
    assert guess_type("street_light", types) == "fixture"


def test_blockout_matches_the_committed_files(tmp_path):
    """worlds/town's .glb files are generated; regenerate them if this fails."""
    _blockout().main(["make_blockout.py", str(tmp_path)])
    for rel in ("town.glb", "houses/mia.glb", "houses/otto.glb"):
        assert (tmp_path / rel).read_bytes() == (TOWN / rel).read_bytes(), rel


# ------------------------------------------------------------- serving

@pytest.fixture
def client():
    agents = AgentsConfig(agents={"mia": AgentSpec("mia", "Mia", "tok-mia")}, viewer_password="open-sesame")
    app = create_app(load_world(TOWN), agents, viewer_dir=None, clock_interval=0)
    with TestClient(app) as c:
        yield c


def test_world_state_lists_areas_with_models(client):
    auth = {"Authorization": "Bearer open-sesame"}
    state = client.get(f"{API_PREFIX}/api/state", headers=auth).json()
    areas = {a["id"]: a for a in state["areas"]}
    assert areas["mia"]["kind"] == "house" and areas["mia"]["plot"] == "1"
    assert areas["mia"]["entrance"] == "mia.garden"
    assert next(r for r in state["rooms"] if r["id"] == "mia.kitchen")["area"] == "mia"

    url = areas["town"]["model_url"]
    assert client.get(url).status_code == 401, "3D files need the viewer password"
    res = client.get(url, headers=auth)
    assert res.status_code == 200 and res.content[:4] == b"glTF"
    assert client.get(f"{API_PREFIX}/world/files/world.yaml", headers=auth).status_code == 404
    assert client.get(f"{API_PREFIX}/world/files/../../agents.yaml", headers=auth).status_code == 404


def test_imported_world_without_objects_yaml_loads(tmp_path):
    """import-glb gives new objects the type 'fixture', so it must exist even without an objects.yaml."""
    cli(["import-glb", str(TOWN / "town.glb"), "--world", str(tmp_path)])
    cli(["import-glb", str(TOWN / "houses" / "mia.glb"), "--world", str(tmp_path), "--owner", "mia"])
    cfg = load_world(tmp_path)
    fixtures = [o for o in cfg.objects if o.type == "fixture"]
    assert fixtures, "everything is a fixture when there are no types to match"
    w = World(cfg)
    assert w.object_view(w.objects["mia.kettle"])["name"] == "kettle"  # named after its id, not "fixture"


def test_unknown_type_error_lists_the_known_ones(town_dir):
    house = yaml.safe_load((town_dir / "houses" / "mia.yaml").read_text())
    house["objects"][0]["type"] = "arcade"
    (town_dir / "houses" / "mia.yaml").write_text(yaml.safe_dump(house))
    with pytest.raises(ConfigError, match="unknown type 'arcade'.*fixture"):
        load_world(town_dir)


def test_bad_poses_are_refused(town_dir):
    objects = town_dir / "objects.yaml"
    text = objects.read_text()
    objects.write_text(text.replace("    seats: 3\n", "    seats: 0\n", 1))
    with pytest.raises(ConfigError, match="'sofa' has a verb with a pose, so it needs at least 1 seat"):
        load_world(town_dir)
    objects.write_text(text.replace("pose: sitting", "pose: squatting", 1))
    with pytest.raises(ConfigError, match="pose must be one of sitting, lying, standing, not 'squatting'"):
        load_world(town_dir)

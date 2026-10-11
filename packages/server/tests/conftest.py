from pathlib import Path

import pytest

from commonroom_server.config import AgentsConfig, AgentSpec, load_world
from commonroom_server.world import World

WORLD_DIR = Path(__file__).parent / "world"


@pytest.fixture
def config():
    return load_world(WORLD_DIR)


@pytest.fixture
def agents():
    return AgentsConfig(agents={
        "mia": AgentSpec("mia", "Mia", "tok-mia"),
        "otto": AgentSpec("otto", "Otto", "tok-otto"),
        "kai": AgentSpec("kai", "Kai", "tok-kai", start_room="study"),
    })


@pytest.fixture
def world(config, agents):
    w = World(config)
    for spec in agents.agents.values():
        w.connect(spec)
    w.commit()
    return w

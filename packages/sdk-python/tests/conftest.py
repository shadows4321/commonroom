import asyncio
import socket
import sys
from pathlib import Path

import pytest
import uvicorn

from commonroom_server.app import create_app
from commonroom_server.config import AgentsConfig, AgentSpec, load_world

SERVER_TESTS = Path(__file__).resolve().parents[2] / "server" / "tests"
WORLD_DIR = SERVER_TESTS / "world"
sys.path.insert(0, str(SERVER_TESTS))  # for avatar_files, the test-file builders


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(tmp_path):
    agents = AgentsConfig(agents={
        "mia": AgentSpec("mia", "Mia", "tok-mia"),
        "otto": AgentSpec("otto", "Otto", "tok-otto"),
    })
    app = create_app(load_world(WORLD_DIR), agents, viewer_dir=None, clock_interval=0, avatars_dir=tmp_path / "avatars")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    yield f"ws://127.0.0.1:{port}"
    srv.should_exit = True
    await task

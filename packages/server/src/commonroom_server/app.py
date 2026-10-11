"""FastAPI application: WebSocket endpoints for AIs and viewers, plus the viewer's static files."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from importlib import metadata
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from commonroom_protocol import (
    ACK, ACTION_FAILED, API_PREFIX, ERROR, PROTOCOL_VERSION, REGISTER, SUPPORTED_VERSIONS, UPDATE, WELCOME,
    WORLD_FORMAT_VERSION, WORLD_STATE, Reason, negotiate,
)

from .config import AgentsConfig, AgentSpec, WorldConfig
from .store import Store
from .world import Batch, Failure, World

log = logging.getLogger("commonroom")

DEFAULT_VIEWER_DIR = Path(__file__).resolve().parents[3] / "viewer"
REGISTER_TIMEOUT = 10.0

try:
    SERVER_VERSION = metadata.version("commonroom-server")
except metadata.PackageNotFoundError:
    SERVER_VERSION = "0.0.0+unknown"


def version_info() -> dict:
    return {
        "server": "commonroom-server",
        "server_version": SERVER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "supported_protocol_versions": list(SUPPORTED_VERSIONS),
        "world_format_version": WORLD_FORMAT_VERSION,
        "endpoints": {
            "agent": f"{API_PREFIX}/ws",
            "viewer": f"{API_PREFIX}/ws/viewer",
            "state": f"{API_PREFIX}/api/state",
            "events": f"{API_PREFIX}/api/events",
        },
    }


class Connection:
    """A WebSocket with its own outgoing queue, so a slow client never stalls the world."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.writer = asyncio.create_task(self._write())

    async def _write(self) -> None:
        with contextlib.suppress(Exception):
            while (msg := await self.queue.get()) is not None:
                await self.ws.send_json(msg)

    def send(self, msg: dict) -> None:
        self.queue.put_nowait(msg)

    async def close(self, code: int = 1000) -> None:
        self.queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer, timeout=2)
        with contextlib.suppress(Exception):
            await self.ws.close(code)


class Hub:
    """Owns the world and fans its changes out to connected AIs and viewers."""

    def __init__(self, world: World, agents: AgentsConfig):
        self.world = world
        self.agents = agents
        self.agent_conns: dict[str, Connection] = {}
        self.viewers: set[Connection] = set()

    def authenticate(self, token: str) -> AgentSpec | None:
        for spec in self.agents.agents.values():
            if hmac.compare_digest(spec.token.encode(), str(token).encode()):
                return spec
        return None

    def viewer_allowed(self, token: str | None) -> bool:
        expected = self.agents.viewer_token
        return expected is None or (token is not None and hmac.compare_digest(expected.encode(), token.encode()))

    def dispatch(self, batch: Batch) -> None:
        if not batch:
            return
        for ev in batch.events:
            for eid, conn in self.agent_conns.items():
                if ev.visible_to(eid):
                    conn.send(ev.to_wire(eid))
        if self.viewers:
            reveal = self.world.settings.viewer_sees_whispers
            update = {
                "type": UPDATE,
                "events": [ev.to_wire(reveal_private=reveal) for ev in batch.events],
                "entities": [self.world.entity_view(e) for e in batch.entities.values()],
                "objects": [self.world.object_view(o, nested=False) for o in batch.objects.values()],
                "clock": self.world.clock.view(),
            }
            for conn in self.viewers:
                conn.send(update)

    async def clock_loop(self, interval: float) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            self.world.advance_clock(now - last)
            last = now
            self.dispatch(self.world.commit())


def create_app(world_config: WorldConfig, agents: AgentsConfig, db_path: str | None = None,
               viewer_dir: str | Path | None = DEFAULT_VIEWER_DIR, clock_interval: float = 1.0) -> FastAPI:
    store = Store(db_path, world_config.settings.recent_events) if db_path else None
    hub = Hub(World(world_config, store), agents)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(hub.clock_loop(clock_interval)) if clock_interval > 0 else None
        yield
        if task:
            task.cancel()
        for eid in list(hub.agent_conns):
            hub.world.disconnect(eid)
        hub.world.commit()
        if store:
            store.close()

    app = FastAPI(title="commonroom", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/version")
    async def version():
        """Unversioned discovery endpoint: which protocol versions and URLs this server speaks."""
        return version_info()

    @app.get(f"{API_PREFIX}/api/state")
    async def state(token: str | None = Query(default=None)):
        if not hub.viewer_allowed(token):
            raise HTTPException(401, "bad viewer token")
        return hub.world.world_state()

    @app.get(f"{API_PREFIX}/api/events")
    async def events(after: int = 0, limit: int = 200, token: str | None = Query(default=None)):
        if not hub.viewer_allowed(token):
            raise HTTPException(401, "bad viewer token")
        if store is None:
            return [ev.to_wire(reveal_private=True) for ev in hub.world.global_recent if ev.id > after][:limit]
        reveal = hub.world.settings.viewer_sees_whispers
        return [ev.to_wire(reveal_private=reveal) for ev in store.events(after, min(limit, 1000))]

    @app.websocket(f"{API_PREFIX}/ws")
    async def agent_socket(ws: WebSocket):
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), REGISTER_TIMEOUT)
        except Exception:
            await ws.close(4400)
            return
        error = None
        spec = None
        if not isinstance(hello, dict) or hello.get("type") != REGISTER:
            error = ("bad_request", "First message must be a register intent.")
        elif (protocol := negotiate(hello.get("protocol"))) is None:
            error = ("protocol_mismatch", f"No common protocol version. Server supports "
                                          f"{', '.join(SUPPORTED_VERSIONS)}; you offered {hello.get('protocol')!r}.")
        elif (spec := hub.authenticate(hello.get("token", ""))) is None:
            error = ("unauthorized", "Unknown token.")
        if error:
            await ws.send_json({"type": ERROR, "id": hello.get("id") if isinstance(hello, dict) else None,
                                "reason": error[0], "message": error[1],
                                "supported_protocol_versions": list(SUPPORTED_VERSIONS)})
            await ws.close(4401 if error[0] == "unauthorized" else 4400)
            return

        conn = Connection(ws)
        old = hub.agent_conns.get(spec.id)
        if old is not None:
            old.send({"type": ERROR, "reason": "replaced", "message": "Another connection logged in as you."})
            asyncio.create_task(old.close(4409))
        hub.agent_conns[spec.id] = conn
        entity = hub.world.connect(spec, hello.get("name"), hello.get("description"))
        batch = hub.world.commit()
        conn.send({"type": WELCOME, "id": hello.get("id"), "protocol": protocol, "server_version": SERVER_VERSION,
                   "world": hub.world.config.name,
                   "entity_id": entity.id, "snapshot": hub.world.snapshot(entity.id)})
        hub.dispatch(batch)
        log.info("%s connected (protocol %s, client %s)", entity.id, protocol, hello.get("client", "unknown"))

        try:
            while True:
                msg = await ws.receive_json()
                handle_intent(hub, spec.id, conn, msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("connection for %s failed", spec.id)
        finally:
            if hub.agent_conns.get(spec.id) is conn:
                del hub.agent_conns[spec.id]
                hub.world.disconnect(spec.id)
                hub.dispatch(hub.world.commit())
                log.info("%s disconnected", spec.id)
            await conn.close()

    @app.websocket(f"{API_PREFIX}/ws/viewer")
    async def viewer_socket(ws: WebSocket, token: str | None = Query(default=None)):
        await ws.accept()
        if not hub.viewer_allowed(token):
            await ws.send_json({"type": ERROR, "reason": "unauthorized", "message": "Bad viewer token."})
            await ws.close(4401)
            return
        conn = Connection(ws)
        conn.send({"type": WORLD_STATE, "protocol": PROTOCOL_VERSION, "server_version": SERVER_VERSION,
                   **hub.world.world_state()})
        hub.viewers.add(conn)
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.viewers.discard(conn)
            await conn.close()

    if viewer_dir is not None and Path(viewer_dir).is_dir():
        viewer_dir = Path(viewer_dir)

        @app.get("/")
        async def index():
            return FileResponse(viewer_dir / "index.html")

        app.mount("/viewer", StaticFiles(directory=viewer_dir), name="viewer")

    return app


def handle_intent(hub: Hub, entity_id: str, conn: Connection, msg) -> None:
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        conn.send({"type": ACTION_FAILED, "id": None, "intent": None, "reason": Reason.BAD_REQUEST,
                   "message": "Every message must be a JSON object with a 'type'.", "details": {}})
        return
    intent_id, kind = msg.get("id"), msg["type"]
    try:
        result = hub.world.handle(entity_id, msg)
        reply = {"type": ACK, "id": intent_id, "intent": kind, "result": result}
    except Failure as f:
        reply = {"type": ACTION_FAILED, "id": intent_id, "intent": kind, "reason": f.reason,
                 "message": f.message, "details": f.details}
    except Exception:
        log.exception("intent %r from %s crashed", kind, entity_id)
        reply = {"type": ACTION_FAILED, "id": intent_id, "intent": kind, "reason": Reason.INTERNAL_ERROR,
                 "message": "The server hit an error handling that.", "details": {}}
    batch = hub.world.commit()
    conn.send(reply)
    hub.dispatch(batch)

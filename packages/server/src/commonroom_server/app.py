"""FastAPI application: WebSocket endpoints for AIs and viewers, plus the viewer's static files."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import tempfile
import time
from importlib import metadata
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from commonroom_protocol import (
    ACK, ACTION_FAILED, API_PREFIX, AUTH_REQUIRED, ERROR, PROTOCOL_VERSION, REGISTER, SUPPORTED_VERSIONS, UPDATE,
    VIEWER_AUTH, WELCOME, WORLD_FORMAT_VERSION, WORLD_STATE, Reason, negotiate,
)

from . import avatars
from .avatars import AvatarError, AvatarLimits, AvatarStore
from .config import AgentsConfig, AgentSpec, WorldConfig
from .store import Store
from .world import Batch, Failure, World

log = logging.getLogger("commonroom")

DEFAULT_VIEWER_DIR = Path(__file__).resolve().parents[3] / "viewer"
REGISTER_TIMEOUT = 10.0
AUTH_FAILURE_DELAY = 1.0  # seconds; slows down password guessing

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
            "avatar_upload": f"{API_PREFIX}/avatar",
            "avatar_files": f"{API_PREFIX}/avatars/{{file}}",
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

    @property
    def viewer_password_required(self) -> bool:
        return self.agents.viewer_password is not None

    def viewer_allowed(self, password: str | None) -> bool:
        expected = self.agents.viewer_password
        if expected is None:
            return True
        return isinstance(password, str) and hmac.compare_digest(expected.encode(), password.encode())

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


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else None


def _problem(status: int, reason: str, message: str) -> JSONResponse:
    return JSONResponse({"reason": reason, "message": message}, status_code=status)


_AVATAR_STATUS = {"too_large": 413, "unsupported_type": 415}


def create_app(world_config: WorldConfig, agents: AgentsConfig, db_path: str | None = None,
               viewer_dir: str | Path | None = DEFAULT_VIEWER_DIR, clock_interval: float = 1.0,
               avatars_dir: str | Path | None = None) -> FastAPI:
    store = Store(db_path, world_config.settings.recent_events) if db_path else None
    hub = Hub(World(world_config, store), agents)
    avatar_limits = AvatarLimits.from_settings(world_config.settings.avatars)
    avatar_store = AvatarStore(avatars_dir or tempfile.mkdtemp(prefix="commonroom-avatars-"))

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

    async def require_viewer(request: Request, token: str | None) -> None:
        if not hub.viewer_allowed(_bearer(request) or token):
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            raise HTTPException(401, "wrong viewer password")

    @app.get(f"{API_PREFIX}/api/state")
    async def state(request: Request, token: str | None = Query(default=None)):
        await require_viewer(request, token)
        return hub.world.world_state()

    @app.get(f"{API_PREFIX}/api/events")
    async def events(request: Request, after: int = 0, limit: int = 200, token: str | None = Query(default=None)):
        await require_viewer(request, token)
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
        password = token  # 1.0 style: ?token= in the URL
        if password is None and hub.viewer_password_required:
            # 1.1: ask for the password as a message, so it never appears in URLs or access logs.
            await ws.send_json({"type": AUTH_REQUIRED, "protocol": PROTOCOL_VERSION})
            try:
                msg = await asyncio.wait_for(ws.receive_json(), REGISTER_TIMEOUT)
            except Exception:
                await ws.close(4400)
                return
            if isinstance(msg, dict) and msg.get("type") == VIEWER_AUTH:
                password = msg.get("password")
        if not hub.viewer_allowed(password):
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            await ws.send_json({"type": ERROR, "reason": "unauthorized", "message": "Wrong viewer password."})
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

    def avatar_owner(request: Request) -> AgentSpec | None:
        token = _bearer(request)
        return hub.authenticate(token) if token else None

    def replace_avatar(spec: AgentSpec, avatar: dict | None) -> dict | None:
        hub.world.ensure_entity(spec)
        previous = hub.world.set_avatar(spec.id, avatar)
        hub.dispatch(hub.world.commit())
        if previous and not hub.world.avatar_in_use(previous["sha256"]):
            avatar_store.remove(previous["sha256"])
        return avatar

    @app.put(f"{API_PREFIX}/avatar")
    async def upload_avatar(request: Request):
        """Set your AI's avatar: the request body is a VRM, GLB or PNG file; auth is the AI's token."""
        spec = avatar_owner(request)
        if spec is None:
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            return _problem(401, "unauthorized", "Send your AI's token as 'Authorization: Bearer <token>'.")
        limit = avatar_limits.max_upload_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return _problem(413, "too_large", f"Avatars can be at most {limit // (1024 * 1024)} MB.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return _problem(413, "too_large", f"Avatars can be at most {limit // (1024 * 1024)} MB.")
        data = bytes(body)
        try:
            info = await asyncio.to_thread(avatars.inspect, data, avatar_limits)
        except AvatarError as exc:
            return _problem(_AVATAR_STATUS.get(exc.reason, 422), exc.reason, exc.message)
        await asyncio.to_thread(avatar_store.save, info, data)
        log.info("%s uploaded a %s avatar (%d bytes)", spec.id, info.kind, info.size)
        return {"avatar": replace_avatar(spec, info.view())}

    @app.delete(f"{API_PREFIX}/avatar")
    async def delete_avatar(request: Request):
        spec = avatar_owner(request)
        if spec is None:
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            return _problem(401, "unauthorized", "Send your AI's token as 'Authorization: Bearer <token>'.")
        return {"avatar": replace_avatar(spec, None)}

    @app.get(f"{API_PREFIX}/avatars/{{name}}")
    async def avatar_file(name: str):
        # No password: names are SHA-256 hashes, only learned through the authenticated streams.
        path = avatar_store.path(name)
        if path is None:
            raise HTTPException(404, "no such avatar")
        return FileResponse(path, media_type=avatars.CONTENT_TYPES[name.rsplit(".", 1)[1]],
                            headers={"Cache-Control": "public, max-age=31536000, immutable",
                                     "X-Content-Type-Options": "nosniff"})

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

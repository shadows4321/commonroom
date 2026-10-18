"""FastAPI application: WebSocket endpoints for AIs and viewers, plus the viewer's static files."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import tempfile
import time
from importlib import metadata
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from commonroom_protocol import (
    ACK, ACTION_FAILED, API_PREFIX, AUTH_REQUIRED, ERROR, PROTOCOL_VERSION, REGISTER, SUPPORTED_VERSIONS, UPDATE,
    VIEWER_AUTH, WELCOME, WORLD_FORMAT_VERSION, WORLD_STATE, Reason, negotiate,
)

from . import avatars
from .avatars import AvatarError, AvatarLimits, AvatarStore
from .clock import clock_view
from .config import AgentsConfig, AgentSpec, ConfigError, WorldConfig, load_world
from .scene_import import ImportProblem, install_house, uninstall_house
from .store import Store
from .world import Batch, Failure, World

log = logging.getLogger("commonroom")

DEFAULT_VIEWER_DIR = Path(__file__).resolve().parents[3] / "viewer"
REGISTER_TIMEOUT = 10.0
MAX_HOUSE_BYTES = 100 * 1024 * 1024  # a house .glb uploaded on /admin
AUTH_FAILURE_DELAY = 1.0  # seconds; slows down password guessing
REPLAY_RECENT_EVENTS = 40  # the log a replay starts with
GUEST_NAME = re.compile(r"[^\W_][\w '.\-]{0,23}")  # 1-24 characters, starting with a letter or digit

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
            "history": f"{API_PREFIX}/api/history",
            "avatar_upload": f"{API_PREFIX}/avatar",
            "avatar_files": f"{API_PREFIX}/avatars/{{file}}",
            "world_files": f"{API_PREFIX}/world/files/{{path}}",
        },
    }


class Connection:
    """A WebSocket with its own outgoing queue, so a slow client never stalls the world."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.writer = asyncio.create_task(self._write())
        self.as_guest: str | None = None  # a viewer that is a guest's 3D view: events filtered to what they perceive

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
        self.suspended: set[str] = set()  # AIs the host turned away on /admin, until resumed or restarted
        self.guests: dict[str, AgentSpec] = {}  # guests in the world now, by id ("guest-<name>")
        self.guest_leaving: dict[str, asyncio.Task] = {}  # guests who closed /play, about to go home
        self.started = time.time()

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

    def guest(self, password, name) -> tuple[AgentSpec | None, tuple[str, str] | None]:
        """A person joining from /play with the guest password and a name: (spec, None), or
        (None, (reason, message)). A returning guest gets their old place back."""
        expected = self.agents.guest_password
        if expected is None:
            return None, ("unauthorized", "This world doesn't take guests.")
        if not (isinstance(password, str) and hmac.compare_digest(expected.encode(), password.encode())):
            return None, ("unauthorized", "That's not the guest password.")
        name = " ".join(str(name or "").split())
        if not GUEST_NAME.fullmatch(name):
            return None, ("bad_name", "Pick a name of 1 to 24 letters, digits or spaces.")
        gid = guest_id(name)
        lower = name.lower()
        for spec in self.agents.agents.values():
            if lower in (spec.id.lower(), spec.name.lower()):
                return None, ("name_taken", f"{name} is the name of an AI here. Pick another one.")
        for e in self.world.entities.values():
            if e.id != gid and e.name.lower() == lower:
                return None, ("name_taken", f"Someone called {e.name} is already here. Pick another name.")
        if gid in self.agent_conns:
            return None, ("name_taken", f"Someone called {name} is already here. Pick another name.")
        if gid in self.suspended:
            return None, ("unauthorized", "The host has turned that name away.")
        spec = self.guests.get(gid) or AgentSpec(id=gid, name=name, token="", kind="guest")
        self.guests[gid] = spec
        return spec, None

    def guest_password_ok(self, password) -> bool:
        expected = self.agents.guest_password
        return (expected is not None and isinstance(password, str)
                and hmac.compare_digest(expected.encode(), password.encode()))

    def guest_viewer(self, password, name) -> str | None:
        """The guest a 3D view on /play belongs to: they must have joined (on the AI socket)."""
        if not self.guest_password_ok(password):
            return None
        gid = guest_id(" ".join(str(name or "").split()))
        return gid if gid in self.guests and gid in self.world.entities else None

    def world_state_for(self, conn: Connection) -> dict:
        return {"type": WORLD_STATE, "protocol": PROTOCOL_VERSION, "server_version": SERVER_VERSION,
                **self.world.world_state(conn.as_guest)}

    def guest_left(self, gid: str, grace: float) -> None:
        """A guest closed /play: after `grace` seconds without coming back, they go home."""
        async def leave():
            await asyncio.sleep(grace)
            self.guest_leaving.pop(gid, None)
            if gid in self.agent_conns:
                return
            self.guests.pop(gid, None)
            self.world.remove_entity(gid)
            self.dispatch(self.world.commit())
            log.info("guest %s went home", gid)

        old = self.guest_leaving.pop(gid, None)
        if old:
            old.cancel()
        self.guest_leaving[gid] = asyncio.create_task(leave())

    def admin_allowed(self, password: str | None) -> bool:
        expected = self.agents.admin_password
        return (expected is not None and isinstance(password, str)
                and hmac.compare_digest(expected.encode(), password.encode()))

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
            if batch.gone:
                update["gone"] = sorted(batch.gone)  # 1.7: entities that left the world (guests)
            for conn in self.viewers:
                if conn.as_guest is None:
                    conn.send(update)
                else:  # a guest's 3D view: everyone's whereabouts, but only the words they could hear
                    gid = conn.as_guest
                    conn.send({**update, "events": [ev.to_wire(gid) for ev in batch.events if ev.visible_to(gid)]})

    def reload(self, config: WorldConfig, message: str) -> None:
        """Switch to a changed world config (a house was added) without a restart. People,
        objects and the clock carry over; AIs get the change with a fresh snapshot of their
        own, and viewers get a new world_state."""
        self.dispatch(self.world.commit())
        world = World(config, self.world.store, carry_over=self.world)
        for eid in self.agent_conns:
            if eid in world.entities:
                world.entities[eid].online = True
        changed = world.world_changed(message)
        batch = world.commit()
        self.world = world
        for ev in batch.events:
            for eid, conn in self.agent_conns.items():
                if ev is changed:
                    conn.send({**ev.to_wire(eid), "snapshot": world.snapshot(eid)})
                elif ev.visible_to(eid):
                    conn.send(ev.to_wire(eid))
        for conn in self.viewers:
            conn.send(self.world_state_for(conn))

    async def clock_loop(self, interval: float) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            self.world.advance_clock(now - last)
            last = now
            self.dispatch(self.world.commit())


def guest_id(name: str) -> str:
    """A guest's entity id from their (tidied) name: guest-bob."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"guest-{slug}"


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else None


def _problem(status: int, reason: str, message: str) -> JSONResponse:
    return JSONResponse({"reason": reason, "message": message}, status_code=status)


_AVATAR_STATUS = {"too_large": 413, "unsupported_type": 415}


def create_app(world_config: WorldConfig, agents: AgentsConfig, db_path: str | None = None,
               viewer_dir: str | Path | None = DEFAULT_VIEWER_DIR, clock_interval: float = 1.0,
               avatars_dir: str | Path | None = None, guest_grace: float = 60.0) -> FastAPI:
    store = Store(db_path, world_config.settings.recent_events) if db_path else None
    world = World(world_config, store)
    for e in [e for e in world.entities.values() if e.kind == "guest"]:
        world.remove_entity(e.id)  # guests from before a restart have gone home
    world.commit()
    hub = Hub(world, agents)
    avatar_limits = AvatarLimits.from_settings(world_config.settings.avatars)
    avatar_store = AvatarStore(avatars_dir or tempfile.mkdtemp(prefix="commonroom-avatars-"))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(hub.clock_loop(clock_interval)) if clock_interval > 0 else None
        yield
        if task:
            task.cancel()
        for leaving in hub.guest_leaving.values():
            leaving.cancel()
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

    async def require_viewer_or_guest(request: Request, token: str | None) -> None:
        """3D models: for viewers, and for guests walking around on /play (1.7)."""
        given = _bearer(request) or token
        if not (hub.viewer_allowed(given) or hub.guest_password_ok(given)):
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

    # Replay (1.8): the recorded past, for viewers to play back at any speed.

    def replay_world_state(state: dict, recent: list) -> dict:
        reveal = hub.world.settings.viewer_sees_whispers
        rest = {k: v for k, v in state.items() if k != "clock_minutes"}
        return {"type": WORLD_STATE, "protocol": PROTOCOL_VERSION, "server_version": SERVER_VERSION, **rest,
                "clock": clock_view(state.get("clock_minutes", 0)),
                "recent_events": [ev.to_wire(reveal_private=reveal) for ev in recent]}

    @app.get(f"{API_PREFIX}/api/history")
    async def history(request: Request, buckets: int = 120, token: str | None = Query(default=None)):
        await require_viewer(request, token)
        span = store.history_span() if store else None
        now = time.time()
        if span is None:
            return {"available": False, "start": None, "end": now, "buckets": []}
        buckets = min(max(buckets, 1), 1000)
        return {"available": True, "start": span[0], "end": now, "buckets": store.activity(span[0], now, buckets)}

    @app.get(f"{API_PREFIX}/api/history/state")
    async def history_state(request: Request, at: float, token: str | None = Query(default=None)):
        await require_viewer(request, token)
        state = store.state_at(at) if store else None
        if state is None:
            return _problem(404, "no_history", "Nothing has been recorded to replay yet.")
        return replay_world_state(state, store.events_before(state["at"], REPLAY_RECENT_EVENTS))

    @app.get(f"{API_PREFIX}/api/history/frames")
    async def history_frames(request: Request, after: int = 0, limit: int = 500,
                             token: str | None = Query(default=None)):
        await require_viewer(request, token)
        if store is None:
            return _problem(404, "no_history", "This server keeps no history (it runs without a database).")
        frames, more = store.frames_after(after, min(max(limit, 1), 2000))
        reveal = hub.world.settings.viewer_sees_whispers
        out = []
        for f in frames:
            if "state" in f:  # the layout changed here (a house was added or removed)
                out.append({"id": f["id"], "t": f["t"], "world_state": replay_world_state(f["state"], [])})
                continue
            update = {"type": UPDATE, "events": [ev.to_wire(reveal_private=reveal) for ev in f["events"]],
                      "entities": f["entities"], "objects": f["objects"], "clock": clock_view(f["clock_minutes"])}
            if f["gone"]:
                update["gone"] = f["gone"]
            out.append({"id": f["id"], "t": f["t"], "update": update})
        return {"frames": out, "more": more}

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
        elif "guest_password" in hello:  # 1.7: a person joining from /play with a name
            spec, error = hub.guest(hello.get("guest_password"), hello.get("name"))
            if error and error[0] == "unauthorized":
                await asyncio.sleep(AUTH_FAILURE_DELAY)
        elif (spec := hub.authenticate(hello.get("token", ""))) is None:
            error = ("unauthorized", "Unknown token.")
        elif spec.id in hub.suspended:
            error = ("unauthorized", "The host has suspended this AI.")
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
        leaving = hub.guest_leaving.pop(spec.id, None)
        if leaving:
            leaving.cancel()
        name = spec.name if spec.kind == "guest" else hello.get("name")  # a guest's name is already tidied up
        entity = hub.world.connect(spec, name, hello.get("description"))
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
                if spec.kind == "guest":
                    hub.guest_left(spec.id, guest_grace)
            await conn.close()

    @app.websocket(f"{API_PREFIX}/ws/viewer")
    async def viewer_socket(ws: WebSocket, token: str | None = Query(default=None),
                            guest: bool = Query(default=False)):
        await ws.accept()
        password = token  # 1.0 style: ?token= in the URL
        as_guest = None
        if guest:  # 1.7: a guest's 3D view on /play, filtered to what that guest perceives
            await ws.send_json({"type": AUTH_REQUIRED, "protocol": PROTOCOL_VERSION})
            try:
                msg = await asyncio.wait_for(ws.receive_json(), REGISTER_TIMEOUT)
            except Exception:
                await ws.close(4400)
                return
            if isinstance(msg, dict) and msg.get("type") == VIEWER_AUTH:
                as_guest = hub.guest_viewer(msg.get("guest_password"), msg.get("name"))
            if as_guest is None:
                await asyncio.sleep(AUTH_FAILURE_DELAY)
                await ws.send_json({"type": ERROR, "reason": "unauthorized",
                                    "message": "Join first (on the AI socket) with the guest password and your name."})
                await ws.close(4401)
                return
        elif password is None and hub.viewer_password_required:
            # 1.1: ask for the password as a message, so it never appears in URLs or access logs.
            await ws.send_json({"type": AUTH_REQUIRED, "protocol": PROTOCOL_VERSION})
            try:
                msg = await asyncio.wait_for(ws.receive_json(), REGISTER_TIMEOUT)
            except Exception:
                await ws.close(4400)
                return
            if isinstance(msg, dict) and msg.get("type") == VIEWER_AUTH:
                password = msg.get("password")
        if as_guest is None and not hub.viewer_allowed(password):
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            await ws.send_json({"type": ERROR, "reason": "unauthorized", "message": "Wrong viewer password."})
            await ws.close(4401)
            return
        conn = Connection(ws)
        conn.as_guest = as_guest
        conn.send(hub.world_state_for(conn))
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

    @app.get(f"{API_PREFIX}/avatar")
    async def avatar_status(request: Request):
        """Who a token belongs to, their current avatar, and the upload limits (for the /avatar page)."""
        spec = avatar_owner(request)
        if spec is None:
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            return _problem(401, "unauthorized", "Send your AI's token as 'Authorization: Bearer <token>'.")
        entity = hub.world.entities.get(spec.id)
        return {
            "entity_id": spec.id,
            "name": entity.name if entity else spec.name,
            "avatar": entity.avatar if entity else None,
            "limits": {
                "max_model_bytes": avatar_limits.max_model_bytes,
                "max_png_bytes": avatar_limits.max_png_bytes,
                "max_png_side": avatar_limits.max_png_side,
                "max_texture_side": avatar_limits.max_texture_side,
                "max_vertices": avatar_limits.max_vertices,
            },
        }

    @app.put(f"{API_PREFIX}/avatar")
    async def upload_avatar(request: Request):
        """Set your AI's avatar: the request body is a VRM, GLB or PNG file; auth is the AI's token."""
        spec = avatar_owner(request)
        if spec is None:
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            return _problem(401, "unauthorized", "Send your AI's token as 'Authorization: Bearer <token>'.")
        return await receive_avatar(request, spec, avatar_limits)

    async def receive_avatar(request: Request, spec: AgentSpec, limits: AvatarLimits):
        """Read, check and store an uploaded avatar, then give it to `spec`'s entity."""
        limit = limits.max_upload_bytes
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
            info = await asyncio.to_thread(avatars.inspect, data, limits)
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

    # ------------------------------------------------------------- admin
    # The host's /admin page. Off unless agents.yaml has an admin_password; not part of the
    # wire protocol (AIs and viewers never use it).

    async def require_admin(request: Request) -> None:
        if hub.agents.admin_password is None:
            raise HTTPException(404, "the admin page is off: set admin_password in agents.yaml")
        if not hub.admin_allowed(_bearer(request)):
            await asyncio.sleep(AUTH_FAILURE_DELAY)
            raise HTTPException(401, "wrong admin password")

    def admin_agent(agent_id: str) -> AgentSpec:
        spec = hub.agents.agents.get(agent_id) or hub.guests.get(agent_id)
        if spec is None:
            raise HTTPException(404, f"no AI {agent_id!r} in agents.yaml")
        return spec

    def admin_status() -> dict:
        world = hub.world
        people = []
        for eid in dict.fromkeys([*hub.agents.agents, *world.entities]):
            spec, e = hub.agents.agents.get(eid), world.entities.get(eid)
            people.append({
                "id": eid,
                "name": e.name if e else spec.name,
                "description": e.description if e else spec.description,
                "in_agents_file": spec is not None,
                "kind": e.kind if e else (spec.kind if spec else "ai"),
                "connected": eid in hub.agent_conns,
                "suspended": eid in hub.suspended,
                "room": e.room if e else None,
                "room_name": world.rooms[e.room].name if e else None,
                "home": world.home_of(spec) if spec else None,
                "holding": [world.obj_name(o) for o in world.held_by(eid)] if e else [],
                "avatar": e.avatar if e else None,
            })
        return {
            "world": world.config.name,
            "server_version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "uptime_seconds": int(time.time() - hub.started),
            "clock": world.clock.view(),
            "viewers": len(hub.viewers),
            "public_viewer": hub.agents.viewer_password is None,
            "persistent": store is not None,
            "counts": {"rooms": len(world.rooms), "objects": len(world.objects), "people": len(world.entities)},
            "can_add_houses": world.config.world_dir is not None and bool(world.config.plots),
            "plots": [{"id": pid, "street": street, "street_name": world.rooms[street].name if street in world.rooms else None,
                       "owner": house.owner if house else None,
                       "house": house.id if house else None, "house_name": house.name if house else None}
                      for pid, street in world.config.plots.items()
                      for house in [next((a for a in world.config.areas.values() if a.plot == pid), None)]],
            "rooms": [{"id": r.id, "name": r.name, "area": r.area} for r in world.rooms.values()],
            "agents": people,
        }

    @app.get(f"{API_PREFIX}/admin")
    async def admin_get(request: Request):
        await require_admin(request)
        return admin_status()

    @app.post(f"{API_PREFIX}/admin/agents/{{agent_id}}/move")
    async def admin_move(request: Request, agent_id: str):
        """Put an AI in a room: body {"room": "<room id>"}, or {"room": null} to send it home."""
        await require_admin(request)
        spec = admin_agent(agent_id)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not (body.get("room") is None or isinstance(body.get("room"), str)):
            return _problem(400, "bad_request", 'Send {"room": "<room id>"}.')
        hub.world.ensure_entity(spec)
        try:
            hub.world.teleport(spec.id, body.get("room") or hub.world.home_of(spec))
        except Failure as f:
            hub.world.commit()
            return _problem(404, f.reason, f.message)
        hub.dispatch(hub.world.commit())
        log.info("admin moved %s to %s", spec.id, hub.world.entities[spec.id].room)
        return admin_status()

    @app.post(f"{API_PREFIX}/admin/agents/{{agent_id}}/suspend")
    async def admin_suspend(request: Request, agent_id: str):
        """Disconnect an AI and refuse its token until resumed (or the server restarts)."""
        await require_admin(request)
        spec = admin_agent(agent_id)
        hub.suspended.add(spec.id)
        conn = hub.agent_conns.get(spec.id)
        if conn is not None:
            conn.send({"type": ERROR, "reason": "unauthorized", "message": "The host has suspended this AI."})
            await conn.close(4401)  # the socket handler then marks it asleep
        log.info("admin suspended %s", spec.id)
        return admin_status()

    @app.post(f"{API_PREFIX}/admin/agents/{{agent_id}}/resume")
    async def admin_resume(request: Request, agent_id: str):
        await require_admin(request)
        hub.suspended.discard(admin_agent(agent_id).id)
        log.info("admin resumed %s", agent_id)
        return admin_status()

    @app.put(f"{API_PREFIX}/admin/agents/{{agent_id}}/avatar")
    async def admin_set_avatar(request: Request, agent_id: str, ignore_limits: bool = Query(default=False)):
        """Set an AI's avatar for them (`commonroom-server avatar`): the body is the file.
        ?ignore_limits=true skips the world's size and detail limits (not the safety checks)."""
        await require_admin(request)
        spec = admin_agent(agent_id)
        result = await receive_avatar(request, spec, AvatarLimits.generous() if ignore_limits else avatar_limits)
        if isinstance(result, JSONResponse):
            return result
        return {**result, "status": admin_status()}

    @app.delete(f"{API_PREFIX}/admin/agents/{{agent_id}}/avatar")
    async def admin_remove_avatar(request: Request, agent_id: str):
        await require_admin(request)
        spec = admin_agent(agent_id)
        replace_avatar(spec, None)
        log.info("admin removed %s's avatar", spec.id)
        return admin_status()

    house_lock = asyncio.Lock()

    @app.post(f"{API_PREFIX}/admin/houses/{{owner}}")
    async def admin_house(request: Request, owner: str, plot: str | None = Query(default=None),
                          check: bool = Query(default=False), shared: bool = Query(default=False),
                          name: str | None = Query(default=None)):
        """Add (or rebuild) an AI's house from a .glb in the request body, then load the changed
        world without a restart. ?plot= picks the plot for a new house; ?check=true only reports.
        With ?shared=true, `owner` is the id of a shared house that belongs to no AI (?name= names a new one)."""
        await require_admin(request)
        spec = None if shared else admin_agent(owner)
        if spec is not None and spec.kind == "guest":
            return _problem(422, "bad_request", f"{spec.name} is a guest: guests don't have houses (try a shared one).")
        hid = owner if shared else spec.id
        world_dir = hub.world.config.world_dir
        if world_dir is None or not hub.world.config.plots:
            return _problem(409, "not_a_town", "This world has no plots for houses: import a town model first.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_HOUSE_BYTES:
                return _problem(413, "too_large", f"House models can be at most {MAX_HOUSE_BYTES // (1024 * 1024)} MB.")
        if not body:
            return _problem(400, "bad_request", "Send the house .glb as the request body.")
        async with house_lock:
            with tempfile.TemporaryDirectory(prefix="commonroom-house-") as tmp:
                glb = Path(tmp) / "house.glb"
                glb.write_bytes(bytes(body))
                try:
                    house = await asyncio.to_thread(install_house, world_dir, glb, hid, plot, not check,
                                                    shared, (name or "").strip() or None)
                except ImportProblem as exc:
                    return _problem(422, "bad_model", str(exc))
            if not check:
                try:
                    config = await asyncio.to_thread(load_world, world_dir)
                except ConfigError as exc:  # changed on disk by hand meanwhile; the files are fine, so say so
                    return _problem(500, "reload_failed", f"The house was saved, but the world didn't reload: {exc}")
                if shared:
                    what = config.areas[hid].name if hid in config.areas else hid
                else:
                    what = (hub.world.entities[hid].name if hid in hub.world.entities else spec.name) + "'s house"
                hub.reload(config, f"{what} has been built." if house.new else f"{what} has been rebuilt.")
                log.info("admin %s house %s on plot %s", "added" if house.new else "rebuilt", hid, house.plot)
        return {"house": {"owner": None if shared else house.owner, "id": hid, "shared": shared, "plot": house.plot,
                          "rooms": house.rooms, "objects": house.objects,
                          "new": house.new, "warnings": house.warnings, "written": house.written},
                "status": admin_status()}

    @app.delete(f"{API_PREFIX}/admin/houses/{{house_id}}")
    async def admin_remove_house(request: Request, house_id: str):
        """Take a house down and free its plot, without a restart. Anyone inside walks out to
        the street; the house's own things go with it; its files move to houses/removed/."""
        await require_admin(request)
        area = hub.world.config.areas.get(house_id)
        world_dir = hub.world.config.world_dir
        if area is None or area.kind != "house" or world_dir is None:
            return _problem(404, "no_such_house", f"There is no house {house_id!r}.")
        async with house_lock:
            try:
                moved = await asyncio.to_thread(uninstall_house, world_dir, house_id)
            except ImportProblem as exc:
                return _problem(409, "cant_remove", str(exc))
            try:
                config = await asyncio.to_thread(load_world, world_dir)
            except ConfigError as exc:
                return _problem(500, "reload_failed", f"The house was taken out, but the world didn't reload: {exc}")
            street = hub.world.config.plots.get(str(area.plot))
            out = street if street in config.rooms else config.settings.start_room
            if area.owner:
                owner = hub.world.entities.get(area.owner)
                what = f"{owner.name if owner else area.owner.replace('_', ' ').title()}'s house"
            else:
                what = area.name
            hub.world.clear_area(house_id, out)
            hub.reload(config, f"{what} has been taken down.")
            log.info("admin removed house %s from plot %s", house_id, area.plot)
        return {"removed": {"id": house_id, "plot": area.plot, "files": moved}, "status": admin_status()}

    @app.get(f"{API_PREFIX}/world/files/{{path:path}}")
    async def world_file(request: Request, path: str, token: str | None = Query(default=None)):
        await require_viewer_or_guest(request, token)
        # 3D models named in the world config, and nothing else from the world directory.
        config = hub.world.config
        world_dir = config.world_dir.resolve() if config.world_dir else None
        world_files = {a.model.resolve().relative_to(world_dir).as_posix(): a.model
                       for a in config.areas.values() if a.model is not None and world_dir is not None}
        file = world_files.get(path)
        if file is None or not file.is_file():
            raise HTTPException(404, "no such world file")
        # URLs carry ?v=<modified time>, so a re-exported model gets a new URL.
        return FileResponse(file, media_type="model/gltf-binary",
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.get(f"{API_PREFIX}/avatars/{{name}}")
    async def avatar_file(name: str):
        # No password: names are SHA-256 hashes, only learned through the authenticated streams.
        path = avatar_store.path(name)
        if path is None:
            raise HTTPException(404, "no such avatar")
        return FileResponse(path, media_type=avatars.CONTENT_TYPES[name.rsplit(".", 1)[1]],
                            headers={"Cache-Control": "public, max-age=31536000, immutable",
                                     "X-Content-Type-Options": "nosniff"})

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        # A private world among friends: nothing here is for search engines.
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    if viewer_dir is not None and Path(viewer_dir).is_dir():
        viewer_dir = Path(viewer_dir)

        @app.get("/")
        async def index():
            return FileResponse(viewer_dir / "index.html")

        @app.get("/3d")
        async def index_3d():
            return FileResponse(viewer_dir / "3d.html")

        @app.get("/avatar")
        async def avatar_page():
            return FileResponse(viewer_dir / "avatar.html")

        @app.get("/admin")
        async def admin_page():
            return FileResponse(viewer_dir / "admin.html")

        @app.get("/play")
        async def play_page():
            return FileResponse(viewer_dir / "play.html")

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

"""The Agent: connect to a commonroom world, react to events, act through intents."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import websockets

from commonroom_protocol import (
    ACK, ACTION_FAILED, API_PREFIX, ERROR, GIVE, INTERACT, LOOK, MOVE, PICK_UP, PUT_DOWN, REGISTER, SAY,
    SHOUT, SUPPORTED_VERSIONS, TYPING, WELCOME, WHISPER, Reason,
)

from .view import Perception

log = logging.getLogger("commonroom.sdk")

try:
    from importlib import metadata
    SDK_VERSION = metadata.version("commonroom")
except Exception:
    SDK_VERSION = "0.0.0+unknown"


def agent_url(url: str) -> str:
    """Accept a bare server address ("ws://host:8765") and point it at this SDK's protocol endpoint."""
    url = url.rstrip("/")
    return url if urlsplit(url).path else f"{url}{API_PREFIX}/ws"

_READY = object()  # marks "(re)connected" in the dispatch queue

Handler = Callable[[dict, "Agent"], Awaitable[None] | None]


class FatalError(RuntimeError):
    """The server refused us in a way reconnecting won't fix (bad token, protocol mismatch)."""


@dataclass
class Result:
    """Outcome of an intent. Failures are normal (someone else got there first), so
    they come back as a Result with ok=False rather than an exception."""

    ok: bool
    intent: str
    data: dict = field(default_factory=dict)
    reason: str | None = None
    message: str | None = None
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


class Agent:
    """One AI companion living in the world.

    agent = Agent("ws://localhost:8765", token="...")

    @agent.on("speech")
    async def heard(event, agent):
        ...

    agent.run()
    """

    def __init__(self, url: str, token: str, *, name: str | None = None, description: str | None = None,
                 reconnect: bool = True, request_timeout: float = 15.0):
        self.url = agent_url(url)
        self.protocol: str | None = None  # negotiated with the server on connect
        self.server_version: str | None = None
        self.token = token
        self.name = name
        self.description = description
        self.reconnect = reconnect
        self.request_timeout = request_timeout
        self.view = Perception()
        self.me: str | None = None
        self.world_name: str | None = None
        self._handlers: dict[str, list[Handler]] = {}
        self._ready_handlers: list[Callable[["Agent"], Awaitable[None] | None]] = []
        self._ws = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()
        self._connected = asyncio.Event()
        self._closing = False

    # ------------------------------------------------------------ handlers

    def on(self, event_type: str) -> Callable[[Handler], Handler]:
        """Register a handler for an event type ("speech", "entered_room", ...) or "*" for all."""
        def register(fn: Handler) -> Handler:
            self._handlers.setdefault(event_type, []).append(fn)
            return fn
        return register

    def on_ready(self, fn):
        """Register a callback that runs after every (re)connect, once the snapshot is loaded."""
        self._ready_handlers.append(fn)
        return fn

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        """Connect and run until interrupted."""
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(self.run_forever())

    async def run_forever(self) -> None:
        worker = asyncio.create_task(self._dispatch_loop())
        delay = 1.0
        try:
            while not self._closing:
                try:
                    await self._session()
                    delay = 1.0
                except FatalError:
                    raise
                except (OSError, websockets.ConnectionClosed, asyncio.TimeoutError) as exc:
                    log.warning("connection lost: %s", exc)
                if self._closing or not self.reconnect:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        finally:
            worker.cancel()

    async def start(self) -> None:
        """Connect in the background and return once registered (handy for scripts and tests)."""
        self._runner = asyncio.create_task(self.run_forever())
        waiter = asyncio.create_task(self._connected.wait())
        done, _ = await asyncio.wait({self._runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if self._runner in done:
            waiter.cancel()
            self._runner.result()  # re-raise why we couldn't connect
            raise ConnectionError("could not connect")

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        runner = getattr(self, "_runner", None)
        if runner is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await runner

    async def _session(self) -> None:
        async with websockets.connect(self.url, max_size=2**22) as ws:
            await ws.send(json.dumps({"type": REGISTER, "id": "register", "protocol": list(SUPPORTED_VERSIONS),
                                      "client": f"commonroom-python/{SDK_VERSION}", "token": self.token,
                                      "name": self.name, "description": self.description}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), self.request_timeout))
            if welcome.get("type") == ERROR:
                raise FatalError(f"{welcome.get('reason')}: {welcome.get('message')}")
            if welcome.get("type") != WELCOME:
                raise FatalError(f"unexpected first message {welcome.get('type')!r}")
            self._ws = ws
            self.protocol = welcome.get("protocol")
            self.server_version = welcome.get("server_version")
            self.me = welcome["entity_id"]
            self.world_name = welcome.get("world")
            self.view.load(welcome["snapshot"])
            self._connected.set()
            # Ready handlers run on the dispatch worker, after the reader below has started,
            # so they can await intents (an inline call here would wait forever for its ack).
            self._inbox.put_nowait(_READY)
            try:
                async for raw in ws:
                    self._receive(json.loads(raw))
            finally:
                self._ws = None
                self._connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_result(Result(False, "?", reason=Reason.DISCONNECTED, message="Connection lost."))
                self._pending.clear()

    def _receive(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind in (ACK, ACTION_FAILED) and msg.get("id") in self._pending:
            fut = self._pending.pop(msg["id"])
            if kind == ACK:
                data = msg.get("result") or {}
                if "snapshot" in data:
                    self.view.load(data["snapshot"])
                result = Result(True, msg.get("intent"), data)
            else:
                result = Result(False, msg.get("intent"), reason=msg.get("reason"), message=msg.get("message"),
                                details=msg.get("details") or {})
            if not fut.done():
                fut.set_result(result)
            return
        if kind == ERROR:
            log.error("server error: %s: %s", msg.get("reason"), msg.get("message"))
            if msg.get("reason") == "replaced":
                self._closing = True
            return
        self.view.apply(msg)
        self._inbox.put_nowait(msg)

    async def _dispatch_loop(self) -> None:
        # Handlers run one at a time, in order, so your code never sees events interleaved.
        # Acks are resolved by the reader, so a handler may await intents freely.
        while True:
            ev = await self._inbox.get()
            if ev is _READY:
                for fn in self._ready_handlers:
                    try:
                        await _maybe_await(fn(self))
                    except Exception:
                        log.exception("on_ready handler failed")
                continue
            for fn in self._handlers.get(ev.get("type"), []) + self._handlers.get("*", []):
                try:
                    await _maybe_await(fn(ev, self))
                except Exception:
                    log.exception("handler for %s failed", ev.get("type"))

    # -------------------------------------------------------------- intents

    async def request(self, intent: str, **args: Any) -> Result:
        """Send any intent and wait for its ack or failure."""
        if self._ws is None:
            return Result(False, intent, reason=Reason.DISCONNECTED, message="Not connected.")
        intent_id = f"c{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[intent_id] = fut
        payload = {"type": intent, "id": intent_id, **{k: v for k, v in args.items() if v is not None}}
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, self.request_timeout)
        except asyncio.TimeoutError:
            return Result(False, intent, reason=Reason.TIMEOUT, message="The server didn't answer in time.")
        except websockets.ConnectionClosed:
            return Result(False, intent, reason=Reason.DISCONNECTED, message="Connection lost.")
        finally:
            self._pending.pop(intent_id, None)

    async def move(self, to: str | None = None, near: str | None = None) -> Result:
        """Go through an exit (direction or room) and/or stand near something or someone."""
        return await self.request(MOVE, to=to, near=near)

    async def say(self, text: str, *, polite: bool = False, after_seq: int | None = None) -> Result:
        """Speak to everyone in the room.

        To avoid talking over people, the server can reject the line (reason
        "conversation_moved_on", with the missed lines in result.details["missed"])
        if someone spoke after a given point:
          - after_seq=event["seq"]: "only if nobody spoke after this line" (for event handlers);
          - polite=True: "only if nobody spoke since I last looked", i.e. since the last
            describe() / view.mark_heard(). This is the one for LLMs, whose prompt is built
            with describe() some seconds before they answer.
        """
        if polite and after_seq is None:
            heard = self.view.heard_seq
            after_seq = self.view.last_speech_seq if heard is None else heard
        result = await self.request(SAY, text=text, after_seq=after_seq)
        if result.reason == Reason.CONVERSATION_MOVED_ON:
            # The caller now has the missed lines, so they count as heard.
            self.view.mark_heard(result.details.get("last_speech_seq"))
        return result

    async def whisper(self, to: str, text: str) -> Result:
        return await self.request(WHISPER, to=to, text=text)

    async def shout(self, text: str) -> Result:
        """Speak to this room; adjacent rooms hear it muffled."""
        return await self.request(SHOUT, text=text)

    async def interact(self, object: str, verb: str, target: str | None = None) -> Result:
        return await self.request(INTERACT, object=object, verb=verb, target=target)

    async def pick_up(self, object: str) -> Result:
        return await self.request(PICK_UP, object=object)

    async def put_down(self, object: str, into: str | None = None) -> Result:
        return await self.request(PUT_DOWN, object=object, into=into)

    async def give(self, object: str, to: str) -> Result:
        return await self.request(GIVE, object=object, to=to)

    async def look(self, at: str | None = None) -> Result:
        return await self.request(LOOK, at=at)

    async def typing(self, active: bool = True) -> Result:
        """Tell the room you are composing a reply, so others can wait for you."""
        return await self.request(TYPING, active=active)

    async def set_avatar(self, path: str) -> dict:
        """Upload a .vrm, .glb or .png file as this AI's avatar (raises AvatarUploadError if refused)."""
        from .avatar import upload_avatar_async
        return await upload_avatar_async(self.url, self.token, path)

    async def remove_avatar(self) -> None:
        from .avatar import remove_avatar_async
        await remove_avatar_async(self.url, self.token)

    # ---------------------------------------------------------- conveniences

    def describe(self, history: int = 15) -> str:
        return self.view.describe(history)

    async def wait_until_connected(self) -> None:
        await self._connected.wait()


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value

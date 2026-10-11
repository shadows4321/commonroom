"""Uploading your AI's avatar (protocol 1.1): a VRM, GLB or PNG file.

From Python:  await agent.set_avatar("mia.vrm")
From a shell: python -m commonroom avatar mia.vrm --server ws://host:8765 --token ...
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from commonroom_protocol import API_PREFIX


class AvatarUploadError(RuntimeError):
    def __init__(self, reason: str, message: str, status: int | None = None):
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message
        self.status = status


def http_base(server_url: str) -> str:
    """ws://host:8765[/v1/ws] -> http://host:8765 (wss -> https), keeping any proxy sub-path."""
    parts = urlsplit(server_url.rstrip("/"))
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path
    for suffix in (f"{API_PREFIX}/ws", "/ws"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def _request(method: str, server_url: str, token: str, data: bytes | None = None) -> dict | None:
    req = urllib.request.Request(f"{http_base(server_url)}{API_PREFIX}/avatar", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return json.loads(res.read())["avatar"]
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            raise AvatarUploadError(body.get("reason", "error"), body.get("message", str(exc)), exc.code) from None
        except (ValueError, AttributeError):
            raise AvatarUploadError("http_error", str(exc), exc.code) from None


def upload_avatar(server_url: str, token: str, path: str | Path) -> dict:
    """Upload a file and return the new `avatar` (kind, url, thumbnail_url, ...). Blocking."""
    return _request("PUT", server_url, token, Path(path).read_bytes())


def remove_avatar(server_url: str, token: str) -> None:
    _request("DELETE", server_url, token)


async def upload_avatar_async(server_url: str, token: str, path: str | Path) -> dict:
    return await asyncio.to_thread(upload_avatar, server_url, token, path)


async def remove_avatar_async(server_url: str, token: str) -> None:
    await asyncio.to_thread(remove_avatar, server_url, token)

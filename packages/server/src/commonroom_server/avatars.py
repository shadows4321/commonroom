"""Avatar uploads: checking VRM / GLB / PNG files from friends, and storing them.

Uploaded files are untrusted and every viewer's browser will load them, so the
checks here are strict: the file must be what it claims to be, stay within size
limits, and be self-contained (a GLB may not point at outside URLs, which would
make viewers' browsers fetch them).
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from commonroom_protocol import API_PREFIX

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

FILE_NAME_RE = re.compile(r"^[0-9a-f]{64}(?:\.thumb)?\.(?:vrm|glb|png|jpg)$")
CONTENT_TYPES = {"vrm": "model/gltf-binary", "glb": "model/gltf-binary", "png": "image/png", "jpg": "image/jpeg"}


class AvatarError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class AvatarLimits:
    max_model_bytes: int = 30 * 1024 * 1024
    max_png_bytes: int = 4 * 1024 * 1024
    max_png_side: int = 2048
    max_texture_side: int = 4096
    max_vertices: int = 300_000

    @classmethod
    def from_settings(cls, raw: dict | None) -> "AvatarLimits":
        raw = raw or {}
        limits = cls()
        if "max_model_mb" in raw:
            limits.max_model_bytes = int(float(raw["max_model_mb"]) * 1024 * 1024)
        if "max_png_mb" in raw:
            limits.max_png_bytes = int(float(raw["max_png_mb"]) * 1024 * 1024)
        for key in ("max_png_side", "max_texture_side", "max_vertices"):
            if key in raw:
                setattr(limits, key, int(raw[key]))
        return limits

    @property
    def max_upload_bytes(self) -> int:
        return max(self.max_model_bytes, self.max_png_bytes)


@dataclass
class AvatarInfo:
    kind: str  # "vrm" | "glb" | "png"
    sha256: str
    size: int
    thumbnail: bytes | None = None
    thumbnail_ext: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def file_name(self) -> str:
        return f"{self.sha256}.{self.kind}"

    @property
    def thumbnail_name(self) -> str | None:
        if self.kind == "png":
            return self.file_name
        return f"{self.sha256}.thumb.{self.thumbnail_ext}" if self.thumbnail else None

    def view(self) -> dict:
        """The `avatar` field of an entity view."""
        thumb = self.thumbnail_name
        return {
            "kind": self.kind,
            "url": f"{API_PREFIX}/avatars/{self.file_name}",
            "thumbnail_url": f"{API_PREFIX}/avatars/{thumb}" if thumb else None,
            "sha256": self.sha256,
            "bytes": self.size,
            "meta": self.meta,
        }


# ------------------------------------------------------------------ images

def image_size(data: bytes) -> tuple[str, int, int] | None:
    """(ext, width, height) for a PNG or JPEG, or None if it is neither."""
    if data.startswith(PNG_MAGIC) and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", width, height
            i += 2 + length
    return None


# -------------------------------------------------------------------- GLB

def _parse_glb(data: bytes) -> tuple[dict, bytes]:
    if len(data) < 20:
        raise AvatarError("invalid_avatar", "The file is too short to be a GLB/VRM.")
    _, version, length = struct.unpack("<4sII", data[:12])
    if version != 2:
        raise AvatarError("invalid_avatar", f"Only glTF 2.0 is supported (this file is version {version}).")
    if length != len(data):
        raise AvatarError("invalid_avatar", "The GLB header's length doesn't match the file; is it truncated?")
    chunk_len, chunk_type = struct.unpack("<II", data[12:20])
    if chunk_type != CHUNK_JSON or 20 + chunk_len > len(data):
        raise AvatarError("invalid_avatar", "The GLB's first chunk must be its JSON.")
    try:
        doc = json.loads(data[20:20 + chunk_len].rstrip(b" \x00"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AvatarError("invalid_avatar", "The GLB's JSON chunk is not valid JSON.") from None
    if not isinstance(doc, dict):
        raise AvatarError("invalid_avatar", "The GLB's JSON chunk is not an object.")
    binary = b""
    offset = 20 + chunk_len
    if offset + 8 <= len(data):
        bin_len, bin_type = struct.unpack("<II", data[offset:offset + 8])
        if bin_type == CHUNK_BIN:
            binary = data[offset + 8:offset + 8 + bin_len]
    return doc, binary


def _image_bytes(doc: dict, binary: bytes, index: Any) -> bytes | None:
    images, views = doc.get("images") or [], doc.get("bufferViews") or []
    if not isinstance(index, int) or not 0 <= index < len(images):
        return None
    view_index = images[index].get("bufferView")
    if not isinstance(view_index, int) or not 0 <= view_index < len(views):
        return None
    view = views[view_index]
    start = int(view.get("byteOffset", 0))
    return binary[start:start + int(view.get("byteLength", 0))]


def _text(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _vrm_meta(doc: dict) -> tuple[dict, Any]:
    """Normalised license/author info and the thumbnail image index, for VRM 1.0 and 0.x."""
    ext = doc.get("extensions") or {}
    if isinstance(ext.get("VRMC_vrm"), dict):
        m = ext["VRMC_vrm"].get("meta") or {}
        meta = {
            "vrm_version": "1.0",
            "title": _text(m.get("name")),
            "authors": [_text(a) for a in (m.get("authors") or [])][:10],
            "license": _text(m.get("licenseUrl")),
            "avatar_permission": _text(m.get("avatarPermission")),
            "commercial_usage": _text(m.get("commercialUsage")),
            "allow_redistribution": m.get("allowRedistribution") if isinstance(m.get("allowRedistribution"), bool) else None,
        }
        return meta, m.get("thumbnailImage")
    m = (ext.get("VRM") or {}).get("meta") or {}
    meta = {
        "vrm_version": "0.x",
        "title": _text(m.get("title")),
        "authors": [_text(m["author"])] if m.get("author") else [],
        "license": _text(m.get("licenseName")),
        "avatar_permission": _text(m.get("allowedUserName")),
        "commercial_usage": _text(m.get("commercialUssageName")),  # sic: the VRM 0.x spelling
        "allow_redistribution": None,
    }
    texture = m.get("texture")
    textures = doc.get("textures") or []
    source = textures[texture].get("source") if isinstance(texture, int) and 0 <= texture < len(textures) else None
    return meta, source


def _inspect_glb(data: bytes, limits: AvatarLimits) -> AvatarInfo:
    if len(data) > limits.max_model_bytes:
        raise AvatarError("too_large", f"Models can be at most {limits.max_model_bytes // (1024 * 1024)} MB.")
    doc, binary = _parse_glb(data)

    for buf in doc.get("buffers") or []:
        if "uri" in buf:
            raise AvatarError("not_self_contained", "The model refers to an outside buffer; export it as a single GLB/VRM.")
    for index, img in enumerate(doc.get("images") or []):
        if "uri" in img:
            raise AvatarError("not_self_contained", "The model refers to an outside image; embed textures in the file.")
        img_bytes = _image_bytes(doc, binary, index)
        size = image_size(img_bytes or b"")
        if size and max(size[1], size[2]) > limits.max_texture_side:
            raise AvatarError("texture_too_large", f"Textures can be at most {limits.max_texture_side}px on a side "
                                                   f"(found {size[1]}x{size[2]}).")

    accessors = doc.get("accessors") or []
    vertices = 0
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            pos = (prim.get("attributes") or {}).get("POSITION")
            if isinstance(pos, int) and 0 <= pos < len(accessors):
                vertices += int(accessors[pos].get("count", 0))
    if vertices > limits.max_vertices:
        raise AvatarError("too_detailed", f"Models can have at most {limits.max_vertices:,} vertices (found {vertices:,}).")

    used = set(doc.get("extensionsUsed") or [])
    is_vrm = "VRMC_vrm" in used or "VRM" in used or "VRMC_vrm" in (doc.get("extensions") or {}) \
        or "VRM" in (doc.get("extensions") or {})
    info = AvatarInfo(kind="vrm" if is_vrm else "glb", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    info.meta["vertices"] = vertices
    if is_vrm:
        meta, thumb_index = _vrm_meta(doc)
        info.meta.update(meta)
        thumb = _image_bytes(doc, binary, thumb_index)
        size = image_size(thumb or b"")
        if size:
            info.thumbnail, info.thumbnail_ext = thumb, size[0]
    return info


def inspect(data: bytes, limits: AvatarLimits | None = None) -> AvatarInfo:
    """Check an uploaded avatar and describe it. Raises AvatarError if it isn't acceptable."""
    limits = limits or AvatarLimits()
    if data.startswith(GLB_MAGIC):
        return _inspect_glb(data, limits)
    if data.startswith(PNG_MAGIC):
        if len(data) > limits.max_png_bytes:
            raise AvatarError("too_large", f"PNG avatars can be at most {limits.max_png_bytes // (1024 * 1024)} MB.")
        size = image_size(data)
        if size is None:
            raise AvatarError("invalid_avatar", "That PNG is damaged.")
        if max(size[1], size[2]) > limits.max_png_side:
            raise AvatarError("texture_too_large", f"PNG avatars can be at most {limits.max_png_side}px on a side "
                                                   f"(found {size[1]}x{size[2]}).")
        return AvatarInfo(kind="png", sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                          meta={"width": size[1], "height": size[2]})
    raise AvatarError("unsupported_type", "Avatars must be a VRM, GLB or PNG file.")


class AvatarStore:
    """Files on disk, named by content hash so URLs never change and can be cached forever."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, info: AvatarInfo, data: bytes) -> None:
        self._write(info.file_name, data)
        if info.thumbnail and info.thumbnail_name != info.file_name:
            self._write(info.thumbnail_name, info.thumbnail)

    def _write(self, name: str, data: bytes) -> None:
        path = self.dir / name
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)

    def remove(self, sha256: str) -> None:
        for path in self.dir.glob(f"{sha256}.*"):
            path.unlink(missing_ok=True)

    def path(self, name: str) -> Path | None:
        if not FILE_NAME_RE.match(name):
            return None
        path = self.dir / name
        return path if path.is_file() else None

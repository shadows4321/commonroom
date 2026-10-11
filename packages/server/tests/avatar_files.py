"""Builders for small but valid PNG / GLB / VRM files, for avatar tests."""

import json
import struct
import zlib


def png(width: int = 4, height: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\xff\x80\x00\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def glb(doc: dict, binary: bytes = b"") -> bytes:
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js
    if binary:
        body += struct.pack("<II", len(binary), 0x004E4942) + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def model(vertices: int = 3, texture: bytes | None = None, vrm: str | None = "1.0", thumbnail: bool = True,
          title: str = "Test Avatar") -> bytes:
    """A one-triangle-ish model; optionally a VRM (1.0 or 0.x) whose thumbnail is `texture`."""
    texture = texture if texture is not None else png(8, 8)
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(texture)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(texture)}],
        "images": [{"bufferView": 0, "mimeType": "image/png"}],
        "textures": [{"source": 0}],
        "accessors": [{"count": vertices, "type": "VEC3", "componentType": 5126}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
    }
    if vrm == "1.0":
        doc["extensionsUsed"] = ["VRMC_vrm"]
        meta = {"name": title, "authors": ["Someone"], "licenseUrl": "https://vrm.dev/licenses/1.0/",
                "avatarPermission": "everyone", "commercialUsage": "personalNonProfit", "allowRedistribution": False}
        if thumbnail:
            meta["thumbnailImage"] = 0
        doc["extensions"] = {"VRMC_vrm": {"specVersion": "1.0", "meta": meta}}
    elif vrm == "0.x":
        doc["extensionsUsed"] = ["VRM"]
        meta = {"title": title, "author": "Someone", "licenseName": "CC_BY", "allowedUserName": "Everyone"}
        if thumbnail:
            meta["texture"] = 0
        doc["extensions"] = {"VRM": {"meta": meta}}
    return glb(doc, texture)

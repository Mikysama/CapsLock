"""Content-Length framed JSON-RPC transport helpers for LSP stdio."""

from __future__ import annotations

import asyncio
import json
from typing import Any


def encode_message(payload: dict[str, object]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    length: int | None = None
    while True:
        line = await reader.readline()
        if not line:
            raise EOFError("LSP server closed stdout")
        if line in {b"\r\n", b"\n"}:
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.casefold() == "content-length":
            length = int(value.strip())
    if length is None:
        return {}
    decoded = json.loads(await reader.readexactly(length))
    if not isinstance(decoded, dict):
        raise ValueError("LSP message must be an object")
    return decoded


__all__ = ["encode_message", "read_message"]

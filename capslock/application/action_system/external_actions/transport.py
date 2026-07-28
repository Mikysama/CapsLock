"""Shared bounded transport helpers for external Actions."""

from __future__ import annotations

from typing import Any

import httpx


def _preserve_manual_approval(source: dict[str, Any], target: dict[str, Any]) -> None:
    if source.get("force_manual_approval") is True:
        target["force_manual_approval"] = True


async def _read_bounded(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    encoding = response.headers.get("content-encoding", "").strip().casefold()
    if encoding not in {"", "identity"}:
        raise ValueError("compressed Web responses are not accepted")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid Web response Content-Length") from exc
        if declared < 0:
            raise ValueError("invalid Web response Content-Length")
        if declared > max_bytes:
            raise ValueError("Web response exceeds the configured byte limit")
    if response.is_stream_consumed:
        cached = response.content
        if len(cached) > max_bytes:
            return cached[:max_bytes], True
        return cached, False
    content = bytearray()
    truncated = False
    async for chunk in response.aiter_raw():
        remaining = max_bytes - len(content)
        if len(chunk) > remaining:
            content.extend(chunk[:remaining])
            truncated = True
            break
        content.extend(chunk)
        if len(content) == max_bytes:
            continue
    return bytes(content), truncated

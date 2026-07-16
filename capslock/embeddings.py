"""Optional local embedding providers and deterministic vector helpers."""

from __future__ import annotations

import hashlib
import math
import socket
import struct
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .domain import EmbeddingBackend, MemoryInfo
from .storage.memory import MemoryStore


DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MAX_EMBEDDING_TEXT_BYTES = 8 * 1024


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedProvider:
    def __init__(self, model: str, cache_dir: Path) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is not installed; install CapsLock with the local-embeddings extra"
            ) from exc
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.model = TextEmbedding(model_name=model, cache_dir=str(cache_dir))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(value) for value in vector] for vector in self.model.embed(texts)]


class LocalHttpEmbeddingProvider:
    def __init__(self, endpoint: str, model: str, *, timeout: float = 20) -> None:
        self.endpoint = validate_loopback_endpoint(endpoint)
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        url = self.endpoint.rstrip("/") + "/embeddings"
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            response = client.post(url, json={"model": self.model, "input": texts})
        if 300 <= response.status_code < 400:
            raise RuntimeError("local embedding endpoint redirects are not allowed")
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("local embedding endpoint returned an invalid response")
        vectors: list[list[float]] = []
        for item in sorted(data, key=lambda value: value.get("index", 0)):
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise RuntimeError("local embedding endpoint returned an invalid vector")
            vectors.append([float(value) for value in vector])
        return vectors


def validate_loopback_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("local embedding endpoint must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("local embedding endpoint cannot contain credentials, query, or fragment")
    if parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("local embedding endpoint must use a loopback host")
    addresses = {
        result[4][0]
        for result in socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
    }
    if not addresses or any(address not in {"127.0.0.1", "::1"} for address in addresses):
        raise ValueError("local embedding endpoint must resolve only to loopback addresses")
    return endpoint.rstrip("/")


def pack_vector(vector: list[float]) -> bytes:
    if not vector or len(vector) > 16_384 or any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding vector is empty, oversized, or non-finite")
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(data: bytes, dimensions: int) -> list[float]:
    if dimensions <= 0 or len(data) != dimensions * 4:
        raise ValueError("stored embedding vector has invalid dimensions")
    return list(struct.unpack(f"<{dimensions}f", data))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return -1.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


class EmbeddingService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        workspace: str,
        session_id: str,
        cache_dir: Path,
        provider_factory: object | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.session_id = session_id
        self.cache_dir = cache_dir
        self.provider_factory = provider_factory

    def provider(self) -> tuple[EmbeddingBackend, str, EmbeddingProvider] | None:
        settings = self.store.memory_settings(self.workspace)
        backend = settings["embedding_backend"]
        if backend is EmbeddingBackend.OFF:
            return None
        model = str(settings["embedding_model"] or DEFAULT_FASTEMBED_MODEL)
        factory = self.provider_factory
        if callable(factory):
            return backend, model, factory(backend, model, settings.get("embedding_endpoint"))
        if backend is EmbeddingBackend.FASTEMBED:
            return backend, model, FastEmbedProvider(model, self.cache_dir)
        endpoint = settings.get("embedding_endpoint")
        if not isinstance(endpoint, str):
            raise RuntimeError("local embedding endpoint is not configured")
        return backend, model, LocalHttpEmbeddingProvider(endpoint, model)

    def index(self, item: MemoryInfo) -> bool:
        if not item.content:
            return False
        configured = self.provider()
        if configured is None:
            return False
        backend, model, provider = configured
        text = item.content.encode("utf-8")[:MAX_EMBEDDING_TEXT_BYTES].decode("utf-8", "ignore")
        vector = provider.embed([text])[0]
        self.store.put_embedding(
            item,
            backend=backend,
            model=model,
            dimensions=len(vector),
            vector=pack_vector(vector),
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
        return True

    def semantic_ranks(self, query: str, *, limit: int = 20) -> dict[str, int]:
        configured = self.provider()
        if configured is None:
            return {}
        backend, model, provider = configured
        query_vector = provider.embed([query])[0]
        scores: list[tuple[str, float]] = []
        for item, packed, dimensions in self.store.embeddings(
            workspace=self.workspace,
            session_id=self.session_id,
            backend=backend,
            model=model,
        ):
            score = cosine_similarity(query_vector, unpack_vector(packed, dimensions))
            if score > 0:
                scores.append((item.id, score))
        scores.sort(key=lambda pair: pair[1], reverse=True)
        return {identifier: rank for rank, (identifier, _) in enumerate(scores[:limit], start=1)}

    def rebuild(self, items: list[MemoryInfo]) -> tuple[int, int]:
        self.store.clear_embeddings(workspace=self.workspace)
        indexed = failed = 0
        for item in items:
            try:
                indexed += int(self.index(item))
            except Exception:
                failed += 1
        return indexed, failed

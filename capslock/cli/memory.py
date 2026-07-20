"""Asynchronous memory command controller."""

from __future__ import annotations

import asyncio
import shlex

from ..domain import EmbeddingBackend, MemoryPolicy, MemoryScope, MemoryType
from .context import CliContext
from .views.memory import render_candidates, render_memories, render_memory


async def memory_command(context: CliContext, text: str) -> None:
    memory, console = context.agent.memory, context.console
    if memory is None:
        console.print("[warning]Memory is unavailable.[/]")
        return
    parts = shlex.split(text)
    operation, arguments = (parts[1], parts[2:]) if len(parts) > 1 else ("list", [])
    try:
        if operation == "list":
            scope = MemoryScope(arguments[0]) if arguments else None
            render_memories(console, await memory.list(scope=scope))
        elif operation == "search":
            render_memories(
                console, await memory.search(" ".join(arguments)), title="Memory search"
            )
        elif operation == "show":
            render_memory(console, await memory.resolve(_one(arguments, "show")))
        elif operation == "add":
            content = await asyncio.to_thread(console.input, "Memory content: ")
            item, rules = await memory.add(
                content=content,
                memory_type=MemoryType.NOTE,
                scope=MemoryScope.WORKSPACE,
            )
            render_memory(console, item)
            if rules:
                console.print(f"[warning]Redacted:[/] {', '.join(rules)}")
        elif operation in {"forget", "undo", "purge"}:
            identifier = _one(arguments, operation)
            if operation == "purge":
                confirmation = await asyncio.to_thread(
                    console.input, f"Permanently purge {identifier}? [y/N] "
                )
                if confirmation.strip().casefold() not in {"y", "yes"}:
                    return
            item = await getattr(memory, operation)(identifier)
            render_memory(console, item)
        elif operation == "export":
            if len(arguments) != 2:
                raise ValueError("usage: /memory export <scope> <path.json>")
            path, count = await memory.export_json(
                MemoryScope(arguments[0]), arguments[1]
            )
            console.print(f"[success]Exported {count} memories:[/] {path}")
        elif operation == "import":
            if len(arguments) != 2:
                raise ValueError("usage: /memory import <scope> <path.json>")
            items, rules = await memory.import_json(
                MemoryScope(arguments[0]), arguments[1]
            )
            console.print(f"[success]Imported {len(items)} memories.[/]")
            if rules:
                console.print(f"[warning]Redacted:[/] {', '.join(rules)}")
        elif operation == "policy":
            if arguments:
                await memory.set_policy(MemoryPolicy(arguments[0]))
            view = await memory.settings()
            console.print(
                f"policy={view.policy.value} writes={view.write_enabled} recall={view.recall_enabled} embeddings={view.embedding_backend.value}"
            )
        elif operation == "enable":
            await memory.set_local_write_enabled(True)
        elif operation in {"disable", "off"}:
            await memory.set_local_write_enabled(False)
        elif operation == "candidates":
            render_candidates(
                console, await memory.candidates(include_all="--all" in arguments)
            )
        elif operation == "candidate":
            await _candidate(context, arguments)
        elif operation == "context":
            hits = await memory.context(arguments[0] if arguments else None)
            for hit in hits:
                console.print(
                    f"{hit.memory.id[:12]} score={hit.score:.4f} {'; '.join(hit.reasons)}"
                )
        elif operation == "embeddings":
            await _embeddings(context, arguments)
        elif operation == "cleanup":
            console.print(str(await memory.cleanup()))
        else:
            raise ValueError("unknown memory command")
    except (ValueError, PermissionError, OSError) as exc:
        console.print(f"[error]Error:[/] {exc}")


async def _candidate(context: CliContext, arguments: list[str]) -> None:
    if len(arguments) < 2:
        raise ValueError("usage: /memory candidate <accept|reject|purge|show> <id>")
    operation, identifier = arguments[0], arguments[1]
    memory = context.agent.memory
    if operation == "accept":
        render_memory(context.console, await memory.accept_candidate(identifier))
    elif operation == "reject":
        await memory.reject_candidate(identifier)
    elif operation == "purge":
        await memory.purge_candidate(identifier)
    elif operation == "show":
        item = await memory.resolve_candidate(identifier)
        context.console.print(
            f"{item.id} {item.status.value} {item.content or '(cleared)'}"
        )
    else:
        raise ValueError("unknown candidate operation")


async def _embeddings(context: CliContext, arguments: list[str]) -> None:
    memory = context.agent.memory
    if not arguments:
        view = await memory.settings()
        context.console.print(
            f"embedding_backend={view.embedding_backend.value} model={view.embedding_model or '-'}"
        )
        return
    operation = arguments[0]
    if operation == "disable":
        await memory.configure_embeddings(EmbeddingBackend.OFF)
    elif operation == "enable" and len(arguments) >= 2:
        backend = EmbeddingBackend(arguments[1].replace("-", "_"))
        endpoint = (
            arguments[2]
            if backend is EmbeddingBackend.LOCAL_HTTP and len(arguments) > 2
            else None
        )
        model = (
            arguments[3]
            if backend is EmbeddingBackend.LOCAL_HTTP and len(arguments) > 3
            else None
        )
        await memory.configure_embeddings(backend, endpoint=endpoint, model=model)
    elif operation == "rebuild":
        context.console.print(f"indexed={await memory.rebuild_embeddings()}")
    else:
        raise ValueError(
            "usage: /memory embeddings [enable <fastembed|local-http> ...|disable|rebuild]"
        )


def _one(arguments: list[str], operation: str) -> str:
    if len(arguments) != 1:
        raise ValueError(f"usage: /memory {operation} <id>")
    return arguments[0]

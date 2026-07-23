"""Initialization, configuration, and credential CLI controllers."""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from openai import AsyncOpenAI
from rich.console import Console

from ..configuration import (
    CONFIG_VERSION,
    Settings,
    read_config_document,
    validate_config_document,
    write_config,
)
from ..credentials import (
    credential_status,
    delete_keyring_credential,
    parse_reference,
    set_keyring_credential,
)
from ..layout import ProjectLayout
from ..permissions import PermissionMode


async def initialize(console: Console, workspace: Path, args) -> int:
    layout = ProjectLayout.discover(workspace)
    path = layout.config
    if path.exists() and not args.update:
        console.print(f"[warning]Configuration already exists:[/] {path}")
        return await config_validate(console, path, json_output=False, strict=False)

    interactive = not args.non_interactive
    provider = args.provider or "primary"
    base_url = args.base_url or "https://api.deepseek.com"
    model = args.model or "deepseek-v4-flash"
    credential = args.credential or "env:CAPSLOCK_API_KEY"
    permission = args.permission_mode or "approve_for_me"
    memory_enabled = not args.disable_memory
    if interactive:
        provider = _ask(console, "Provider name", provider)
        base_url = _ask(console, "OpenAI-compatible base URL", base_url)
        model = _ask(console, "Model", model)
        credential = _ask(
            console, "Credential reference (env:NAME or keyring:NAME)", credential
        )
        permission = _ask(console, "Permission mode", permission)
    parse_reference(credential)
    PermissionMode.parse(permission)
    if credential.startswith("keyring:") and interactive:
        status = credential_status(credential)
        if not status.available:
            secret = await asyncio.to_thread(
                getpass.getpass, "API key (stored in OS keyring): "
            )
            set_keyring_credential(credential.split(":", 1)[1], secret)
    content = _initial_config(
        provider=provider,
        base_url=base_url,
        model=model,
        credential=credential,
        permission=PermissionMode.parse(permission).value,
        memory_enabled=memory_enabled,
        tavily_credential=args.tavily_credential,
    )
    # Validate the exact generated document before replacing user state.
    import tomllib

    issues = validate_config_document(tomllib.loads(content))
    if any(item.severity == "error" for item in issues):
        raise ValueError("generated configuration did not validate")
    if path.exists():
        import tomlkit

        current = tomlkit.parse(path.read_text(encoding="utf-8"))
        generated = tomlkit.parse(content)
        current.pop("model", None)
        for key in (
            "config_version",
            "providers",
            "models",
            "routing",
            "runtime",
            "memory",
        ):
            current[key] = generated[key]
        if "web" in generated:
            current["web"] = generated["web"]
        content = tomlkit.dumps(current)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.parent / "state" / "backups" / f"config.before-init.{stamp}.toml"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
    write_config(path, content)
    console.print(f"[success]Initialized:[/] {path}")
    settings = Settings.load(workspace, layout=layout)
    if args.check_provider:
        console.print(
            f"[warning]Sending a minimal connection test to {settings.model_config.base_url} using {settings.model_config.model}.[/]"
        )
        client = AsyncOpenAI(
            api_key=settings.model_config.api_key or "missing",
            base_url=settings.model_config.base_url,
            timeout=settings.model_config.timeout_seconds,
        )
        try:
            await client.chat.completions.create(
                model=settings.model_config.model,
                messages=[{"role": "user", "content": "Reply OK"}],
                max_tokens=1,
            )
        finally:
            await client.close()
        console.print("[success]Provider connection succeeded.[/]")
    return 0


async def config_validate(
    console: Console, path: Path, *, json_output: bool, strict: bool
) -> int:
    if not path.exists():
        issues = [
            {
                "severity": "warning",
                "code": "config_missing",
                "path": str(path),
                "message": "configuration does not exist",
            }
        ]
    else:
        try:
            document = read_config_document(path)
            issues = [item.__dict__ for item in validate_config_document(document)]
            if not any(item["severity"] == "error" for item in issues):
                # Exercise semantic parsing without modifying the source file.
                pass
        except ValueError as exc:
            issues = [
                {
                    "severity": "error",
                    "code": "config_parse",
                    "path": str(path),
                    "message": str(exc),
                }
            ]
    if json_output:
        console.print_json(json.dumps({"path": str(path), "issues": issues}))
    elif not issues:
        console.print("[success]Configuration is valid.[/]")
    else:
        for item in issues:
            console.print(
                f"[{item['severity']}]{item['severity'].upper()}[/] {item['path']}: {item['message']}"
            )
    errors = any(item["severity"] == "error" for item in issues)
    warnings = any(item["severity"] == "warning" for item in issues)
    return 1 if errors or (strict and warnings) else 0


async def credentials_command(console: Console, path: Path, args) -> int:
    operation = args.credentials_command or "status"
    if operation == "set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else await asyncio.to_thread(getpass.getpass, "Credential: ")
        )
        set_keyring_credential(args.name, secret)
        console.print(f"[success]Stored keyring:{args.name}.[/]")
        return 0
    if operation == "delete":
        if not args.yes:
            answer = await asyncio.to_thread(
                console.input, f"Delete keyring:{args.name}? [y/N] "
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                return 0
        delete_keyring_credential(args.name)
        console.print(f"[success]Deleted keyring:{args.name}.[/]")
        return 0
    references: set[str] = set()
    if path.exists():
        document = read_config_document(path)
        for provider in (document.get("providers") or {}).values():
            if isinstance(provider, dict):
                reference = provider.get("credential")
                if reference:
                    references.add(str(reference))
        web = document.get("web")
        if isinstance(web, dict) and web.get("tavily_credential"):
            references.add(str(web["tavily_credential"]))
    if not references:
        references.add("env:CAPSLOCK_API_KEY")
    for reference in sorted(references):
        status = credential_status(reference)
        console.print(f"{reference}: {'available' if status.available else 'missing'}")
    return 0


def _initial_config(**values: object) -> str:
    import tomlkit

    document = tomlkit.document()
    document.add("config_version", CONFIG_VERSION)
    providers = tomlkit.table(is_super_table=True)
    provider = tomlkit.table()
    provider.add("kind", "openai_compatible")
    provider.add("base_url", values["base_url"])
    provider.add("credential", values["credential"])
    provider.add("data_policy", f"provider:{values['provider']}")
    providers.add(str(values["provider"]), provider)
    document.add("providers", providers)
    models = tomlkit.table(is_super_table=True)
    profile = tomlkit.table()
    profile.add("provider", values["provider"])
    profile.add("model", values["model"])
    profile.add("context_window", 128000)
    profile.add("max_output_tokens", 8192)
    models.add("primary", profile)
    document.add("models", models)
    document.add("routing", {"reasoning": ["primary"], "fast": ["primary"]})
    document.add("runtime", {"permission_mode": values["permission"]})
    document.add("memory", {"enabled": values["memory_enabled"]})
    if values.get("tavily_credential"):
        parse_reference(str(values["tavily_credential"]))
        document.add("web", {"tavily_credential": values["tavily_credential"]})
    return tomlkit.dumps(document)


def _ask(console: Console, label: str, default: str) -> str:
    value = console.input(f"{label} [{default}]: ").strip()
    return value or default

"""CLI management for trusted local tool plugins."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from ..layout import ProjectLayout
from ..plugins import PluginService, PluginValidationError, load_plugin_manifest


async def plugin_command(console: Console, layout: ProjectLayout, args) -> int:
    service = PluginService(layout)
    command = args.plugin_command or "list"
    if command == "list":
        entries = service.entries()
        if not entries:
            console.print("No plugins installed.")
            return 0
        for entry in entries:
            state = (
                "trusted-native"
                if entry.enabled and entry.trusted_native
                else "sandboxed"
                if entry.enabled
                else "disabled"
            )
            console.print(
                f"[bold]{entry.manifest.name}[/] {entry.manifest.version} "
                f"[{state}] digest={entry.manifest.digest[:12]}"
            )
        return 0
    if command == "show":
        entry = service.registry.get(args.name, require_enabled=False)
        _print_manifest(console, entry.manifest, enabled=entry.enabled)
        return 0
    if command == "verify":
        manifest = await service.verify(args.name)
        console.print(
            f"[success]Verified[/] {manifest.name} {manifest.version} {manifest.digest}"
        )
        return 0
    if command in {"install", "upgrade"}:
        source = Path(args.path).expanduser().resolve()
        candidate = load_plugin_manifest(source)
        _print_manifest(console, candidate, enabled=False)
        if not _confirmed(console, args.yes, f"{command} this trusted local plugin"):
            return 3
        installed = await service.install(source)
        verb = "Installed" if command == "install" else "Upgraded"
        console.print(f"[success]{verb}[/] {installed.name} {installed.version}")
        return 0
    entry = service.registry.get(args.name, require_enabled=False)
    _print_manifest(console, entry.manifest, enabled=entry.enabled)
    if (
        command == "enable"
        and (
            bool(getattr(args, "trusted_native", False))
            or bool(getattr(args, "session_lifecycle", False))
        )
        and not args.yes
    ):
        console.print(
            "[warning]Trusted-native or session-lifecycle enable requires the flag and --yes.[/]"
        )
        return 3
    if not _confirmed(console, args.yes, f"{command} this plugin"):
        return 3
    if command == "enable":
        service.enable(
            args.name,
            trusted_native=bool(getattr(args, "trusted_native", False)),
            allow_session_lifecycle=bool(
                getattr(args, "session_lifecycle", False)
            ),
        )
    elif command == "disable":
        service.disable(args.name)
    elif command == "uninstall":
        service.uninstall(args.name)
    else:
        raise PluginValidationError(f"unsupported plugin command: {command}")
    verbs = {"enable": "Enabled", "disable": "Disabled", "uninstall": "Uninstalled"}
    console.print(f"[success]{verbs[command]}[/] {args.name}")
    return 0


def _print_manifest(console: Console, manifest, *, enabled: bool) -> None:
    console.print(
        f"[bold]{manifest.name}[/] {manifest.version}: {manifest.description}"
    )
    console.print(f"Digest: {manifest.digest}")
    console.print(f"State: {'enabled' if enabled else 'disabled'}")
    console.print(
        "Capabilities: "
        + str(manifest.capabilities.as_dict())
    )
    console.print("Tools: " + ", ".join(item.name for item in manifest.tools))
    console.print("Execution: OS sandbox with host-brokered capabilities")


def _confirmed(console: Console, yes: bool, operation: str) -> bool:
    if yes:
        return True
    if not console.is_terminal:
        console.print(
            f"[warning]Confirmation required to {operation}; rerun with --yes.[/]"
        )
        return False
    try:
        response = console.input(f"Confirm {operation}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return response.strip().casefold() in {"y", "yes"}

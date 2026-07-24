"""Fail-closed OS sandbox construction for Shell execution."""

from __future__ import annotations

import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ShellSandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxedCommand:
    argv: tuple[str, ...]
    cwd: Path
    temporary: Path


def sandboxed_command(
    *, command: str, workspace: Path, cwd: Path, network: list[str]
) -> SandboxedCommand:
    system = platform.system()
    temporary = Path(tempfile.mkdtemp(prefix="capslock-shell-"))
    if system == "Linux":
        backend = shutil.which("bwrap")
        if backend is None:
            raise ShellSandboxUnavailable(
                "Shell execution requires bubblewrap on Linux; host fallback is disabled"
            )
        if network and network != ["*"]:
            raise ShellSandboxUnavailable(
                "this sandbox backend cannot enforce host-scoped networking"
            )
        argv = [
            backend,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--tmpfs",
            "/",
        ]
        for system_path in ("/usr", "/etc"):
            if Path(system_path).exists():
                argv.extend(("--ro-bind", system_path, system_path))
        for link_path in ("/bin", "/sbin", "/lib", "/lib64"):
            target = (
                Path(link_path).readlink() if Path(link_path).is_symlink() else None
            )
            if target is not None:
                argv.extend(("--symlink", str(target), link_path))
            elif Path(link_path).exists():
                argv.extend(("--ro-bind", link_path, link_path))
        argv.extend(("--dir", "/tmp", "--bind", str(temporary), "/tmp"))
        current = Path(workspace.anchor)
        for part in workspace.parts[1:-1]:
            current /= part
            argv.extend(("--dir", str(current)))
        argv.extend(
            (
                "--bind",
                str(workspace),
                str(workspace),
                "--proc",
                "/proc",
                "--dev",
                "/dev",
            )
        )
        argv.append("--share-net" if network else "--unshare-net")
        argv.extend(("--chdir", str(cwd), "/bin/bash", "-lc", command))
        return SandboxedCommand(tuple(argv), workspace, temporary)
    if system == "Darwin":
        backend = shutil.which("sandbox-exec")
        if backend is None:
            raise ShellSandboxUnavailable(
                "Shell execution requires sandbox-exec on macOS; host fallback is disabled"
            )
        network_rule = "(allow network*)" if network else "(deny network*)"
        readable = "".join(
            f'(allow file-read* (subpath "{item}"))'
            for item in (
                "/System",
                "/usr",
                "/bin",
                "/sbin",
                "/Library",
                "/etc",
                workspace,
            )
        )
        profile = (
            "(version 1)(deny default)(allow process*)"
            + readable
            + f'(allow file-write* (subpath "{workspace}"))'
            + f'(allow file-write* (subpath "{temporary}")){network_rule}'
        )
        return SandboxedCommand(
            (backend, "-p", profile, "/bin/bash", "-lc", command), cwd, temporary
        )
    raise ShellSandboxUnavailable("Shell is disabled on this operating system")


__all__ = ["SandboxedCommand", "ShellSandboxUnavailable", "sandboxed_command"]

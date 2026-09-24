"""Fail-closed OS sandbox construction for Shell execution."""

from __future__ import annotations

import json
import os
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
    environment: dict[str, str] | None = None


def sandboxed_command(
    *,
    command: str,
    workspace: Path,
    cwd: Path,
    network: list[str],
    workspace_writable: bool = True,
) -> SandboxedCommand:
    system = platform.system()
    if network not in ([], ["*"]):
        raise ShellSandboxUnavailable(
            "this sandbox backend cannot enforce host-scoped networking"
        )
    if system == "Linux":
        backend = shutil.which("bwrap")
        if backend is None:
            raise ShellSandboxUnavailable(
                "Shell execution requires bubblewrap on Linux; host fallback is disabled"
            )
    elif system == "Darwin":
        backend = shutil.which("sandbox-exec")
        if backend is None:
            raise ShellSandboxUnavailable(
                "Shell execution requires sandbox-exec on macOS; host fallback is disabled"
            )
    else:
        raise ShellSandboxUnavailable("Shell is disabled on this operating system")

    temporary = Path(tempfile.mkdtemp(prefix="capslock-shell-"))
    try:
        if system == "Linux":
            argv = [
                backend,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--tmpfs",
                "/",
            ]
            if not workspace_writable:
                argv.extend(
                    (
                        "--clearenv",
                        "--setenv",
                        "PATH",
                        "/usr/local/bin:/usr/bin:/bin",
                        "--setenv",
                        "HOME",
                        "/tmp",
                        "--setenv",
                        "TMPDIR",
                        "/tmp",
                        "--setenv",
                        "GIT_OPTIONAL_LOCKS",
                        "0",
                        "--setenv",
                        "GIT_PAGER",
                        "cat",
                        "--setenv",
                        "PAGER",
                        "cat",
                    )
                )
            for system_path in ("/usr", "/etc"):
                if Path(system_path).exists():
                    argv.extend(("--ro-bind", system_path, system_path))
            # /etc/resolv.conf is commonly a symlink into /run on systemd hosts.
            # Mount that target explicitly; otherwise --share-net still has no DNS
            # inside the namespace and package installation fails mysteriously.
            resolver = Path("/etc/resolv.conf").resolve()
            if resolver.parent != Path("/etc") and resolver.is_file():
                current = Path(resolver.anchor)
                for part in resolver.parent.parts[1:]:
                    current /= part
                    argv.extend(("--dir", str(current)))
                argv.extend(("--ro-bind", str(resolver.parent), str(resolver.parent)))
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
                    "--bind" if workspace_writable else "--ro-bind",
                    str(workspace),
                    str(workspace),
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                )
            )
            argv.append("--share-net" if network else "--unshare-net")
            argv.extend(
                (
                    "--chdir",
                    str(cwd),
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    command,
                )
            )
            return SandboxedCommand(tuple(argv), workspace, temporary)
        network_rule = "(allow network*)" if network else "(deny network*)"
        readable = "".join(
            f"(allow file-read* (subpath {_sandbox_literal(item)}))"
            for item in (
                "/System",
                "/usr",
                "/bin",
                "/sbin",
                "/Library",
                "/etc",
                workspace,
                temporary,
            )
        )
        profile = (
            "(version 1)(deny default)(allow process*)"
            + readable
            + (
                f"(allow file-write* (subpath {_sandbox_literal(workspace)}))"
                if workspace_writable
                else ""
            )
            + f"(allow file-write* (subpath {_sandbox_literal(temporary)}))"
            + network_rule
        )
        return SandboxedCommand(
            (
                backend,
                "-p",
                profile,
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                command,
            ),
            cwd,
            temporary,
            _read_only_environment(temporary) if not workspace_writable else None,
        )
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _sandbox_literal(value: str | Path) -> str:
    return json.dumps(str(value))


def _read_only_environment(temporary: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(temporary),
        "TMPDIR": str(temporary),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        **({"TERM": os.environ["TERM"]} if "TERM" in os.environ else {}),
    }


__all__ = ["SandboxedCommand", "ShellSandboxUnavailable", "sandboxed_command"]

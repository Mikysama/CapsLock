"""Fail-closed LSP process sandbox construction."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path


def sandboxed_lsp_command(command: tuple[str, ...], root: Path) -> tuple[str, ...]:
    if platform.system() == "Linux":
        backend = shutil.which("bwrap")
        if backend is None:
            raise RuntimeError("LSP requires bubblewrap on Linux")
        return (
            backend,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--unshare-net",
            "--chdir",
            str(root),
            *command,
        )
    backend = shutil.which("sandbox-exec")
    if backend is None:
        raise RuntimeError("LSP requires sandbox-exec on macOS")
    profile = (
        "(version 1)(deny default)(allow process*)"
        "(allow file-read*)"
        '(allow file-write* (subpath "/tmp"))'
        "(deny network*)"
    )
    return (backend, "-p", profile, *command)


__all__ = ["sandboxed_lsp_command"]

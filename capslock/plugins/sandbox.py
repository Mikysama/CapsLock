"""Fail-closed OS sandbox command adapters for the plugin SDK."""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .manifest import PluginManifest


class SandboxUnavailableError(RuntimeError):
    code = "plugin_sandbox_unavailable"


@dataclass(frozen=True)
class SandboxCommand:
    argv: tuple[str, ...]
    cwd: Path
    mode: str


class SandboxAdapter:
    @staticmethod
    def detect() -> "SandboxAdapter | None":
        system = platform.system()
        if system == "Linux" and shutil.which("bwrap"):
            return BubblewrapSandbox()
        if system == "Darwin" and shutil.which("sandbox-exec"):
            return MacOSSandbox()
        return None

    def command(self, manifest: PluginManifest) -> SandboxCommand:
        raise NotImplementedError


class BubblewrapSandbox(SandboxAdapter):
    def command(self, manifest: PluginManifest) -> SandboxCommand:
        argv: list[str] = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(manifest.root),
            "/plugin",
            "--chdir",
            "/plugin",
        ]
        for path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
            if path.exists():
                argv.extend(("--ro-bind", str(path), str(path)))
        runtime = Path(sys.prefix).resolve()
        if runtime not in {Path("/usr"), Path("/usr/local")}:
            argv.extend(("--ro-bind", str(runtime), "/runtime"))
        executable = Path("/plugin") / manifest.entrypoint[0]
        if executable.suffix == ".py":
            interpreter = (
                "/runtime/bin/python"
                if runtime not in {Path("/usr"), Path("/usr/local")}
                else sys.executable
            )
            command = (interpreter, str(executable), *manifest.entrypoint[1:])
        else:
            command = (str(executable), *manifest.entrypoint[1:])
        return SandboxCommand(tuple((*argv, "--", *command)), manifest.root, "sandboxed")


class MacOSSandbox(SandboxAdapter):
    def command(self, manifest: PluginManifest) -> SandboxCommand:
        root = str(manifest.root).replace('"', '\\"')
        runtime = str(Path(sys.prefix).resolve()).replace('"', '\\"')
        profile = (
            '(version 1) (deny default) (allow process*) '
            '(allow file-read* (subpath "/usr") (subpath "/System") '
            f'(subpath "{root}") (subpath "{runtime}")) '
            '(allow file-write* (subpath "/private/tmp")) (deny network*)'
        )
        executable = manifest.root / manifest.entrypoint[0]
        command = (
            (sys.executable, str(executable), *manifest.entrypoint[1:])
            if executable.suffix == ".py"
            else (str(executable), *manifest.entrypoint[1:])
        )
        return SandboxCommand(
            ("sandbox-exec", "-p", profile, *command), manifest.root, "sandboxed"
        )


def native_command(manifest: PluginManifest) -> SandboxCommand:
    executable = manifest.root / manifest.entrypoint[0]
    command = (
        (sys.executable, str(executable), *manifest.entrypoint[1:])
        if executable.suffix == ".py"
        else (str(executable), *manifest.entrypoint[1:])
    )
    return SandboxCommand(command, manifest.root, "trusted-native")

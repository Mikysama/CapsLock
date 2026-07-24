"""Language server discovery and workspace root selection."""

from __future__ import annotations

from pathlib import Path

from ..configuration import LspServerSettings


def autodetect_servers() -> dict[str, LspServerSettings]:
    return {
        "python": LspServerSettings(
            ("pyright-langserver", "--stdio"), (".py",), ("pyproject.toml", ".git")
        ),
        "typescript": LspServerSettings(
            ("typescript-language-server", "--stdio"),
            (".ts", ".tsx", ".js", ".jsx"),
            ("package.json", ".git"),
        ),
        "rust": LspServerSettings(("rust-analyzer",), (".rs",), ("Cargo.toml", ".git")),
        "go": LspServerSettings(("gopls",), (".go",), ("go.mod", ".git")),
        "clang": LspServerSettings(
            ("clangd",),
            (".c", ".cc", ".cpp", ".h", ".hpp"),
            ("compile_commands.json", ".git"),
        ),
    }


def find_root(start: Path, markers: tuple[str, ...]) -> Path:
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in markers):
            return directory
    return start


__all__ = ["autodetect_servers", "find_root"]

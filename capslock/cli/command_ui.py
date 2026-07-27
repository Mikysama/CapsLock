"""Frontend-neutral interactions used by slash commands."""

from __future__ import annotations

import asyncio
import base64
import shutil
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from rich.markdown import Markdown


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class PlanApprovalResult:
    """Result of the dedicated Plan Mode approval surface."""

    choice: str
    feedback: str | None = None


class CommandUI(Protocol):
    async def select(self, title: str, choices: Sequence[Choice]) -> str | None: ...
    async def confirm(
        self, title: str, detail: str, *, default: bool = False
    ) -> bool: ...
    async def show(self, title: str, content: str) -> None: ...
    async def show_markdown(self, title: str, content: str) -> None: ...
    async def show_agent_response(
        self,
        title: str,
        question: str,
        content: str,
        tools: Sequence[Any] = (),
    ) -> None: ...
    async def input_text(self, title: str, prompt: str) -> str | None: ...
    async def request_plan_entry(self, objective: str) -> bool: ...
    async def request_plan_approval(
        self,
        *,
        objective: str,
        content: str,
        revision: int,
        sha256: str,
        permission_mode: str,
    ) -> PlanApprovalResult: ...
    async def copy(self, content: str) -> str: ...


class ConsoleCommandUI:
    def __init__(self, console) -> None:
        self.console = console

    async def select(self, title: str, choices: Sequence[Choice]) -> str | None:
        if not choices:
            return None
        if title:
            self.console.print(f"[command]{title}[/]")
        for index, choice in enumerate(choices, 1):
            self.console.print(f"  {index}. {choice.label} {choice.detail}")
        answer = await asyncio.to_thread(
            self.console.input, "Select [blank to cancel]: "
        )
        if not str(answer).strip():
            return None
        try:
            return choices[int(str(answer)) - 1].value
        except (ValueError, IndexError):
            raise ValueError("invalid selection") from None

    async def confirm(self, title: str, detail: str, *, default: bool = False) -> bool:
        self.console.print(f"[command]{title}[/]\n{detail}")
        answer = await asyncio.to_thread(self.console.input, "Confirm? [y/N] ")
        return str(answer).strip().casefold() in {"y", "yes"}

    async def show(self, title: str, content: str) -> None:
        self.console.print(f"[command]{title}[/]\n{content}")

    async def show_markdown(self, title: str, content: str) -> None:
        self.console.print(f"[command]{title}[/]")
        self.console.print(Markdown(content, code_theme="ansi_dark", hyperlinks=True))

    async def show_agent_response(
        self,
        title: str,
        question: str,
        content: str,
        tools: Sequence[Any] = (),
    ) -> None:
        from rich.console import Group
        from rich.text import Text

        from .views.conversation import (
            assistant_content,
            assistant_label,
            tool_group,
        )

        parts: list[Any] = [
            Text.assemble((f"/{title.casefold()} ", "bold warning"), (question, "dim"))
        ]
        if tools:
            parts.append(tool_group(list(tools), expanded=True))
        parts.extend((assistant_label(), assistant_content(content)))
        # One Rich render keeps the side answer atomic while the main run is active.
        self.console.print(Group(*parts))

    async def input_text(self, title: str, prompt: str) -> str | None:
        self.console.print(f"[command]{title}[/]")
        value = await asyncio.to_thread(self.console.input, prompt)
        return str(value) if str(value).strip() else None

    async def request_plan_entry(self, objective: str) -> bool:
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        body = Group(
            Text("CapsLock wants to enter Plan Mode to explore and design an implementation approach."),
            Text(),
            Text(f"Objective  {objective}", style="bold"),
            Text(),
            Text("In Plan Mode, CapsLock will:", style="text.secondary"),
            Text("  - Explore the codebase", style="text.secondary"),
            Text("  - Identify existing patterns", style="text.secondary"),
            Text("  - Design an implementation strategy", style="text.secondary"),
            Text("  - Present a plan for your approval", style="text.secondary"),
            Text(),
            Text(
                "No code changes will be made until you approve the plan.",
                style="text.secondary",
            ),
        )
        self.console.print(
            Panel(body, title="Enter Plan Mode?", border_style="plan", expand=True)
        )
        choice = await self.select(
            "",
            (
                Choice("enter", "Yes, enter Plan Mode"),
                Choice("reject", "No, start implementing now"),
            ),
        )
        return choice == "enter"

    async def request_plan_approval(
        self,
        *,
        objective: str,
        content: str,
        revision: int,
        sha256: str,
        permission_mode: str,
    ) -> PlanApprovalResult:
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        plan = Panel(
            Markdown(content, code_theme="ansi_dark", hyperlinks=True),
            border_style="border.muted",
            expand=True,
        )
        body = Group(
            Text("Here is CapsLock's plan:"),
            Text(
                f"{objective}  ·  revision {revision}  ·  sha256 {sha256[:12]}",
                style="text.secondary",
            ),
            plan,
            Text(
                "Plan approval starts a new implementation run. Tool calls still follow "
                f"{permission_mode} permissions.",
                style="text.secondary",
            ),
        )
        self.console.print(
            Panel(body, title="Ready to code?", border_style="plan", expand=True)
        )
        choice = await self.select(
            "CapsLock has finished planning. Would you like to proceed?",
            (
                Choice(
                    "implement",
                    "Yes, start implementation",
                    f"using {permission_mode}",
                ),
                Choice("feedback", "No, keep planning", "tell CapsLock what to change"),
                Choice("reject", "Reject and exit Plan Mode"),
            ),
        )
        selected = choice or "feedback"
        feedback = None
        if selected == "feedback":
            feedback = await self.input_text(
                "Keep planning", "Tell CapsLock what to change [optional]: "
            )
        return PlanApprovalResult(selected, feedback)

    async def copy(self, content: str) -> str:
        data = content.encode("utf-8")
        commands = (
            ("pbcopy",),
            ("wl-copy",),
            ("xclip", "-selection", "clipboard"),
            ("xsel", "--clipboard", "--input"),
        )
        for command in commands:
            if shutil.which(command[0]) is None:
                continue
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE
            )
            await process.communicate(data)
            if process.returncode == 0:
                return command[0]
        if len(data) > 100 * 1024:
            raise ValueError("OSC 52 clipboard payload exceeds 100 KiB; use /export")
        encoded = base64.b64encode(data).decode("ascii")
        self.console.file.write(f"\033]52;c;{encoded}\a")
        self.console.file.flush()
        return "osc52"

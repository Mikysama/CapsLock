"""Declarative CLI slash-command catalog and resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    command: str
    description: str
    handler: str
    children: tuple["CommandSpec", ...] = ()
    accepts_arguments: bool = False
    aliases: tuple[str, ...] = ()


COMMAND_TREE = (
    CommandSpec("/help", "显示可用指令", "help"),
    CommandSpec("/status", "显示会话、模型和权限模式", "status", aliases=("/session",)),
    CommandSpec(
        "/permissions", "查看或切换权限模式", "permissions",
        (
            CommandSpec("/permissions full", "完全访问：自动执行，保留审计与回滚", "permissions"),
            CommandSpec("/permissions approve", "仅确认高风险动作", "permissions"),
            CommandSpec("/permissions ask", "确认每次请求和动作", "permissions"),
        ),
        accepts_arguments=True,
    ),
    CommandSpec("/context", "显示已保留的上下文数量", "context"),
    CommandSpec("/cost", "显示本会话 token 与费用", "cost"),
    CommandSpec("/tasks", "显示任务清单", "tasks"),
    CommandSpec("/changes", "查看待审文件变更", "changes"),
    CommandSpec("/commands", "查看待审固定命令", "commands"),
    CommandSpec("/web", "查看 Web 动作提案", "web"),
    CommandSpec("/sources", "查看已保存的外部来源", "sources"),
    CommandSpec(
        "/mcp", "管理本地 MCP 服务", "mcp",
        (
            CommandSpec("/mcp list", "列出 MCP 服务", "mcp"),
            CommandSpec("/mcp status", "显示服务状态；需 server 名称", "mcp", accepts_arguments=True),
            CommandSpec("/mcp tools", "显示允许工具；需 server 名称", "mcp", accepts_arguments=True),
        ),
        accepts_arguments=True,
    ),
    CommandSpec("/approve", "批准变更、命令或外部动作；需 ID", "approve", accepts_arguments=True),
    CommandSpec("/reject", "拒绝变更、命令或外部动作；需 ID", "reject", accepts_arguments=True),
    CommandSpec("/undo", "预览并撤销最后一次 CapsLock 文件变更", "undo"),
    CommandSpec("/diff", "显示当前 Git 工作树差异", "diff"),
    CommandSpec("/clear", "说明如何开始新会话", "clear"),
    CommandSpec("/cancel", "说明如何取消当前运行", "cancel"),
    CommandSpec("/exit", "退出聊天", "exit", aliases=("/quit",)),
)


CommandNode = CommandSpec


def matching_command_nodes(prefix: str, nodes: tuple[CommandSpec, ...] = COMMAND_TREE) -> tuple[CommandSpec, ...]:
    normalized = prefix.casefold().rstrip()
    matches: list[CommandSpec] = []
    for node in nodes:
        children = matching_command_nodes(prefix, node.children)
        if node.command.casefold().startswith(normalized) or normalized.startswith(node.command.casefold()) or children:
            matches.append(CommandSpec(node.command, node.description, node.handler, children, node.accepts_arguments, node.aliases))
    return tuple(matches)


def command_completions(prefix: str) -> list[str]:
    normalized = prefix.casefold().rstrip()
    return [node.command for node in flatten_commands() if node.command.casefold().startswith(normalized)]


def command_menu_completions(prefix: str) -> list[str]:
    """Return interactive candidates, expanding an exact parent into its subtree."""
    normalized = prefix.casefold().rstrip()
    exact = next(
        (node for name, node in command_entries() if name.casefold() == normalized),
        None,
    )
    if exact is not None and exact.children:
        return [node.command for node in flatten_commands(exact.children)]
    return [name for name, _ in command_entries() if name.casefold().startswith(normalized)]


def command_descriptions() -> dict[str, str]:
    return {name: node.description for name, node in command_entries()}


def command_entries() -> tuple[tuple[str, CommandSpec], ...]:
    """Return canonical command names and aliases from the same catalog."""
    entries: list[tuple[str, CommandSpec]] = []
    for node in flatten_commands():
        entries.append((node.command, node))
        entries.extend((alias, node) for alias in node.aliases)
    return tuple(entries)


def flatten_commands(nodes: tuple[CommandSpec, ...] = COMMAND_TREE) -> tuple[CommandSpec, ...]:
    output: list[CommandSpec] = []
    for node in nodes:
        output.append(node)
        output.extend(flatten_commands(node.children))
    return tuple(output)


def resolve_command(text: str) -> CommandSpec | None:
    normalized = text.casefold().strip()
    candidates: list[CommandSpec] = []
    for node in flatten_commands():
        names = (node.command, *node.aliases)
        if any(normalized == name.casefold() for name in names):
            candidates.append(node)
        elif node.accepts_arguments and any(normalized.startswith(name.casefold() + " ") for name in names):
            candidates.append(node)
    return max(candidates, key=lambda item: len(item.command), default=None)

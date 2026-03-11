from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShortcutCommand:
    name: str
    argument: str = ""


def parse_shortcut_command(text: str) -> ShortcutCommand | None:
    stripped = text.strip()
    if not stripped:
        return None

    normalized = stripped[1:] if stripped.startswith("$") else stripped
    command, separator, raw_argument = normalized.partition(" ")
    argument = raw_argument.strip() if separator else ""

    if command == "h":
        return ShortcutCommand(name="help", argument="") if not argument else None
    if command == "e":
        return ShortcutCommand(name="esc", argument="") if not argument else None
    if command == "qx":
        return ShortcutCommand(name="queue_clear", argument="") if not argument else None
    if command == "ai":
        return ShortcutCommand(name="ai", argument=argument)
    if command == "f":
        return ShortcutCommand(name="fetch", argument=argument)
    if command == "p":
        return ShortcutCommand(name="progress_settings", argument=argument)
    if command == "q":
        return ShortcutCommand(name="queue", argument=argument)
    if command == "i":
        return ShortcutCommand(name="insert", argument=argument)
    return None


def build_shortcut_help_document() -> str:
    return (
        "可用快捷方式：\n"
        "- `h`：查看快捷方式说明文档\n"
        "- `ai <text>`：直接调用 AI 处理本地上下文，不发给 tmux\n"
        "- `f [lines]`：直接抓取当前 tmux 最新输出，默认 50 行\n"
        "- `p [interval_sec lines]`：查看或设置自动抓取的时间间隔和抓取行数\n"
        "- `e`：中断当前正在运行的 Codex\n"
        "- `q <text>`：放入队列，等当前任务结束后自动发送\n"
        "- `qx`：清空当前队列\n"
        "- `i <text>`：立刻插入到当前运行中的 Codex\n\n"
        "兼容旧写法：以上命令前面带 `$` 也仍然可用。"
    )


def build_running_shortcut_help(latest_output: str) -> str:
    clean_output = latest_output.strip() or "(latest tmux output is empty)"
    return (
        "Codex 仍在运行。普通消息默认会立即插入到当前运行中的 Codex。\n"
        f"{build_shortcut_help_document()}\n\n"
        "下面附 tmux 最新 50 行：\n\n"
        f"{clean_output}"
    )

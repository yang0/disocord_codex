from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    discord_channel_id: int
    tmux_bin: str
    tmux_session: str
    tmux_window: int
    tmux_pane: int
    check_interval_sec: int
    progress_interval_sec: int
    progress_capture_lines: int
    completion_lines: int
    state_path: Path

    @classmethod
    def from_env(cls, env: MutableMapping[str, str], *, base_dir: Path) -> "Settings":
        token = env.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is required")

        channel_id = int(env.get("DISCORD_CHANNEL_ID", "1479951053494554736"))
        state_path = Path(env.get("STATE_PATH", "./state/bridge_state.json")).expanduser()
        if not state_path.is_absolute():
            state_path = (base_dir / state_path).resolve()

        return cls(
            discord_bot_token=token,
            discord_channel_id=channel_id,
            tmux_bin=_resolve_tmux_bin(env),
            tmux_session=env.get("TMUX_SESSION", "oc_backup"),
            tmux_window=int(env.get("TMUX_WINDOW", "0")),
            tmux_pane=int(env.get("TMUX_PANE", "0")),
            check_interval_sec=int(env.get("CHECK_INTERVAL_SEC", "5")),
            progress_interval_sec=int(env.get("PROGRESS_INTERVAL_SEC", "300")),
            progress_capture_lines=int(env.get("PROGRESS_CAPTURE_LINES", "220")),
            completion_lines=int(env.get("COMPLETION_LINES", "100")),
            state_path=state_path,
        )


def load_env_file(path: Path, env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    target = env if env is not None else os.environ
    if not path.exists():
        return target

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        target.setdefault(key, value)
    return target


def _resolve_tmux_bin(env: MutableMapping[str, str]) -> str:
    explicit = env.get("TMUX_BIN", "").strip()
    if explicit:
        return explicit

    discovered = shutil.which("tmux")
    if discovered:
        return discovered

    fallback = Path.home() / ".local/bin/tmux"
    if fallback.exists():
        return str(fallback)

    return "tmux"

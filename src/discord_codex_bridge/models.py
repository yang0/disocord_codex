from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class DiscordRequest:
    request_id: str
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: str


@dataclass
class ActiveTask:
    request_id: str
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: str
    started_at: str
    last_progress_at: str

    @classmethod
    def from_request(cls, request: DiscordRequest, *, now: datetime) -> "ActiveTask":
        stamp = now.isoformat()
        return cls(
            request_id=request.request_id,
            channel_id=request.channel_id,
            author_id=request.author_id,
            author_name=request.author_name,
            content=request.content,
            created_at=request.created_at,
            started_at=stamp,
            last_progress_at=stamp,
        )

    def touch_progress(self, *, now: datetime) -> None:
        self.last_progress_at = now.isoformat()


@dataclass
class BridgeState:
    active: ActiveTask | None = None
    queue: list[DiscordRequest] = field(default_factory=list)
    progress_interval_sec_override: int | None = None
    progress_capture_lines_override: int | None = None


@dataclass(frozen=True)
class BridgeEffect:
    kind: str
    text: str = ""
    request: DiscordRequest | None = None


@dataclass(frozen=True)
class BridgeRouteConfig:
    name: str
    channel_id: int
    tmux_session: str
    state_path: Path
    tmux_window: int
    tmux_pane: int
    check_interval_sec: int
    progress_interval_sec: int
    progress_capture_lines: int
    completion_lines: int
    enabled: bool = True

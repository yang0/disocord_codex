from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from discord_codex_bridge.models import ActiveTask, BridgeState, DiscordRequest


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()

        payload = json.loads(self.path.read_text())
        active_payload = payload.get("active")
        queue_payload = payload.get("queue", [])
        return BridgeState(
            active=ActiveTask(**active_payload) if active_payload else None,
            queue=[DiscordRequest(**item) for item in queue_payload],
            progress_interval_sec_override=payload.get("progress_interval_sec_override"),
            progress_capture_lines_override=payload.get("progress_capture_lines_override"),
        )

    def save(self, state: BridgeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "active": asdict(state.active) if state.active else None,
                    "queue": [asdict(item) for item in state.queue],
                    "progress_interval_sec_override": state.progress_interval_sec_override,
                    "progress_capture_lines_override": state.progress_capture_lines_override,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        tmp.replace(self.path)

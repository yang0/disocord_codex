from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - exercised in tests without discord.py installed
    class _FallbackIntents:
        def __init__(self) -> None:
            self.guilds = False
            self.messages = False
            self.message_content = False

        @classmethod
        def default(cls) -> "_FallbackIntents":
            return cls()

    class _FallbackClient:
        def __init__(self, *, intents: Any | None = None) -> None:
            self._intents = intents
            self.user = "fallback-user"
            self._closed = False

        def get_channel(self, _channel_id: int) -> Any | None:
            return None

        async def fetch_channel(self, channel_id: int) -> Any:
            async def _send(_text: str) -> None:
                return None

            return SimpleNamespace(id=channel_id, send=_send)

        async def close(self) -> None:
            self._closed = True

        def is_closed(self) -> bool:
            return self._closed

        def run(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("discord.py is required to run the bridge")

    discord = SimpleNamespace(  # type: ignore[assignment]
        Client=_FallbackClient,
        Intents=_FallbackIntents,
        Message=object,
        abc=SimpleNamespace(Messageable=object),
    )

from discord_codex_bridge.config import Settings, load_bridge_routes
from discord_codex_bridge.ai import AiCommandRunner, AiRequestContext
from discord_codex_bridge.controller import BridgeController
from discord_codex_bridge.models import BridgeRouteConfig, DiscordRequest
from discord_codex_bridge.shortcuts import (
    ShortcutCommand,
    build_running_shortcut_help,
    build_shortcut_help_document,
    parse_shortcut_command,
)
from discord_codex_bridge.state import JsonStateStore
from discord_codex_bridge.summary import format_completion, split_discord_message, summarize_progress
from discord_codex_bridge.tmux_bridge import RUNNING_MARKER, RUNNING_PROBE_LINES, TmuxBridge, pane_indicates_running


LOGGER = logging.getLogger(__name__)
DISPATCH_STARTUP_GRACE_SEC = 3
DEFAULT_FETCH_LINES = 50
MAX_FETCH_LINES = 1000
MIN_PROGRESS_INTERVAL_SEC = 5
MAX_PROGRESS_INTERVAL_SEC = 3600
MIN_PROGRESS_CAPTURE_LINES = 20
MAX_PROGRESS_CAPTURE_LINES = 2000


@dataclass(frozen=True)
class RuntimeSnapshot:
    target: str
    latest_output: str
    running: bool


@dataclass
class BridgeRuntime:
    route: BridgeRouteConfig
    controller: BridgeController
    state_store: JsonStateStore
    channel: Any | None = None
    last_dispatch_error_at: datetime | None = None
    last_observed_at: datetime | None = None
    settling_completion_text: str | None = None
    settling_started_at: datetime | None = None
    draining: bool = False


class DiscordCodexBridge(discord.Client):
    def __init__(
        self,
        settings: Settings,
        *,
        routes: list[BridgeRouteConfig] | None = None,
        tmux_bridge: TmuxBridge | None = None,
        route_loader: Callable[[Settings], list[BridgeRouteConfig]] = load_bridge_routes,
        ai_runner: AiCommandRunner | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)

        self.settings = settings
        self.tmux = tmux_bridge or TmuxBridge(tmux_bin=settings.tmux_bin)
        self.route_loader = route_loader
        self.ai_runner = ai_runner or AiCommandRunner()
        self.monitor_task: asyncio.Task[None] | None = None
        self._routes_by_channel: dict[int, BridgeRuntime] = {}
        self._routes_by_name: dict[str, BridgeRuntime] = {}
        self._draining_runtimes: list[BridgeRuntime] = []
        self._config_mtime_ns: int | None = None

        if routes is not None:
            self.load_routes(routes)
        elif settings.bridges_config_path.exists():
            self.load_routes(self.route_loader(settings))
            self._config_mtime_ns = settings.bridges_config_path.stat().st_mtime_ns

    @property
    def controller(self) -> BridgeController:
        return self.primary_runtime.controller

    @property
    def state_store(self) -> JsonStateStore:
        return self.primary_runtime.state_store

    @property
    def primary_runtime(self) -> BridgeRuntime:
        active = list(self._routes_by_channel.values())
        if len(active) != 1:
            raise RuntimeError("primary runtime is only available when exactly one active route exists")
        return active[0]

    def route_runtime(self, channel_id: int) -> BridgeRuntime | None:
        return self._routes_by_channel.get(channel_id)

    def load_routes(self, routes: list[BridgeRouteConfig]) -> None:
        self._routes_by_channel = {}
        self._routes_by_name = {}
        self._draining_runtimes = []
        for route in routes:
            runtime = self._create_runtime(route)
            self._routes_by_channel[route.channel_id] = runtime
            self._routes_by_name[route.name] = runtime

    async def on_ready(self) -> None:
        if not self._routes_by_channel:
            await self.reload_if_config_changed(force=True)
        if self._client_is_ready():
            await self._ensure_channels_for_active_routes()
        LOGGER.info("discord bridge ready as %s", self.user)
        if self.monitor_task is None:
            self.monitor_task = asyncio.create_task(self._monitor_loop(), name="codex-multi-bridge-monitor-loop")
        for runtime in list(self._routes_by_channel.values()):
            await self._kick_idle_queue(runtime=runtime)

    async def on_message(self, message: discord.Message) -> None:
        runtime = self.route_runtime(message.channel.id)
        if runtime is None or message.author.bot:
            return

        content = _build_message_content(message)
        if not content:
            return

        now = _utcnow()
        try:
            command = parse_shortcut_command(content)
            if command and command.name == "help":
                await self._handle_help_message(runtime=runtime)
                return
            if command and command.name == "progress_settings":
                await self._handle_progress_settings_message(
                    command=command,
                    runtime=runtime,
                )
                return
            if command and command.name == "ai":
                await self._handle_ai_message(
                    message=message,
                    command=command,
                    now=now,
                    runtime=runtime,
                )
                return
            if command and command.name == "fetch":
                await self._handle_fetch_message(
                    command=command,
                    runtime=runtime,
                )
                return

            if runtime.controller.state.active is None and runtime.controller.state.queue:
                await self._kick_idle_queue(runtime=runtime, now=now)

            snapshot = await self._capture_runtime_snapshot(runtime=runtime, lines=runtime.route.completion_lines)
            snapshot = await self._reconcile_active_state(runtime=runtime, snapshot=snapshot, now=now)

            if snapshot.running or runtime.controller.state.active is not None:
                await self._handle_running_message(
                    message=message,
                    command=command,
                    snapshot=snapshot,
                    now=now,
                    runtime=runtime,
                    fallback_content=content,
                )
                return

            await self._handle_idle_message(
                message=message,
                command=command,
                fallback_content=content,
                now=now,
                runtime=runtime,
            )
        except Exception as exc:  # pragma: no cover - defensive runtime reporting
            LOGGER.exception("failed to handle incoming message")
            await self._send_runtime_message(runtime, f"处理消息失败：{type(exc).__name__}: {exc}")

    async def close(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
        await super().close()

    async def reload_if_config_changed(self, *, force: bool = False) -> bool:
        path = self.settings.bridges_config_path
        if not path.exists():
            if force:
                raise FileNotFoundError(f"Bridge config file not found: {path}")
            return False

        mtime_ns = path.stat().st_mtime_ns
        if not force and self._config_mtime_ns == mtime_ns:
            return False

        try:
            routes = self.route_loader(self.settings)
        except Exception:
            LOGGER.exception("failed to reload bridge config; keeping last known good routes")
            return False

        await self._apply_route_diff(routes)
        self._config_mtime_ns = mtime_ns
        return True

    async def _apply_route_diff(self, routes: list[BridgeRouteConfig]) -> None:
        incoming_by_name = {route.name: route for route in routes}

        for name, runtime in list(self._routes_by_name.items()):
            incoming = incoming_by_name.pop(name, None)
            if incoming is None:
                self._retire_runtime(runtime)
                continue

            if self._route_identity_changed(runtime.route, incoming):
                self._retire_runtime(runtime)
                self._install_runtime(self._create_runtime(incoming))
                continue

            self._update_runtime_route(runtime, incoming)

        for route in incoming_by_name.values():
            self._install_runtime(self._create_runtime(route))

        if self._client_is_ready():
            await self._ensure_channels_for_active_routes()

    def _install_runtime(self, runtime: BridgeRuntime) -> None:
        self._routes_by_channel[runtime.route.channel_id] = runtime
        self._routes_by_name[runtime.route.name] = runtime

    def _retire_runtime(self, runtime: BridgeRuntime) -> None:
        self._routes_by_channel.pop(runtime.route.channel_id, None)
        if self._routes_by_name.get(runtime.route.name) is runtime:
            self._routes_by_name.pop(runtime.route.name, None)
        if runtime.controller.state.active is not None or runtime.controller.state.queue:
            runtime.draining = True
            if runtime not in self._draining_runtimes:
                self._draining_runtimes.append(runtime)

    def _drop_runtime_if_drained(self, runtime: BridgeRuntime) -> None:
        if runtime.draining and runtime.controller.state.active is None and not runtime.controller.state.queue:
            runtime.draining = False
            if runtime in self._draining_runtimes:
                self._draining_runtimes.remove(runtime)

    def _update_runtime_route(self, runtime: BridgeRuntime, route: BridgeRouteConfig) -> None:
        runtime.route = route
        runtime.controller.progress_interval_sec = self._runtime_progress_interval_sec(runtime)

    def _route_identity_changed(self, current: BridgeRouteConfig, incoming: BridgeRouteConfig) -> bool:
        return (
            current.channel_id != incoming.channel_id
            or current.tmux_session != incoming.tmux_session
            or current.tmux_window != incoming.tmux_window
            or current.tmux_pane != incoming.tmux_pane
            or current.state_path != incoming.state_path
        )

    def _all_runtimes(self) -> list[BridgeRuntime]:
        return list(self._routes_by_channel.values()) + list(self._draining_runtimes)

    def _create_runtime(self, route: BridgeRouteConfig) -> BridgeRuntime:
        state_store = JsonStateStore(route.state_path)
        state = state_store.load()
        controller = BridgeController(
            progress_interval_sec=state.progress_interval_sec_override or route.progress_interval_sec,
            state=state,
        )
        return BridgeRuntime(route=route, controller=controller, state_store=state_store)

    async def _ensure_channels_for_active_routes(self) -> None:
        for runtime in list(self._routes_by_channel.values()):
            await self._ensure_runtime_channel(runtime)

    def _client_is_ready(self) -> bool:
        is_ready = getattr(self, "is_ready", None)
        if not callable(is_ready):
            return False
        return bool(is_ready())

    async def _ensure_runtime_channel(self, runtime: BridgeRuntime) -> None:
        if runtime.channel is not None:
            return
        runtime.channel = self.get_channel(runtime.route.channel_id) or await self.fetch_channel(runtime.route.channel_id)

    async def _monitor_loop(self) -> None:
        while not self.is_closed():
            try:
                await self.reload_if_config_changed()
                for runtime in list(self._all_runtimes()):
                    await self._monitor_runtime(runtime)
            except Exception:
                LOGGER.exception("monitor tick failed")
            await asyncio.sleep(self._monitor_sleep_interval())

    def _monitor_sleep_interval(self) -> int:
        intervals = [self.settings.check_interval_sec]
        intervals.extend(runtime.route.check_interval_sec for runtime in self._all_runtimes())
        return max(1, min(intervals))

    async def _monitor_runtime(self, runtime: BridgeRuntime) -> None:
        now = _utcnow()
        if runtime.last_observed_at is not None:
            elapsed = (now - runtime.last_observed_at).total_seconds()
            if elapsed < runtime.route.check_interval_sec:
                return
        runtime.last_observed_at = now

        if runtime.controller.state.active is None:
            self._reset_completion_settling(runtime)
            await self._kick_idle_queue(runtime=runtime, now=now)
            self._drop_runtime_if_drained(runtime)
            return

        target = await asyncio.to_thread(
            self.tmux.resolve_pane_target,
            runtime.route.tmux_session,
            runtime.route.tmux_window,
            runtime.route.tmux_pane,
        )
        probe = await asyncio.to_thread(self.tmux.capture_tail, target, lines=RUNNING_PROBE_LINES)
        running = pane_indicates_running(probe)

        if running:
            self._reset_completion_settling(runtime)
            progress_summary = ""
            if self._progress_due(runtime, now):
                progress_text = await asyncio.to_thread(
                    self.tmux.capture_tail,
                    target,
                    lines=self._runtime_progress_capture_lines(runtime),
                )
                progress_summary = summarize_progress(progress_text)
            effects = runtime.controller.observe(active_running=True, now=now, progress_summary=progress_summary)
            runtime.state_store.save(runtime.controller.state)
            if effects:
                await self._execute_effects(effects, runtime=runtime)
            return

        completion_text = await asyncio.to_thread(
            self.tmux.capture_tail,
            target,
            lines=runtime.route.completion_lines,
        )
        active = runtime.controller.state.active
        if active is None:
            self._drop_runtime_if_drained(runtime)
            return
        if not should_treat_task_as_completed(
            started_at=active.started_at,
            now=now,
            probe_text=probe,
            completion_text=completion_text,
            startup_grace_sec=DISPATCH_STARTUP_GRACE_SEC,
        ):
            self._reset_completion_settling(runtime)
            return

        if not self._completion_output_has_settled(runtime=runtime, completion_text=completion_text, now=now):
            return

        self._reset_completion_settling(runtime)
        effects = runtime.controller.observe(
            active_running=False,
            now=now,
            completion_excerpt=format_completion(completion_text, last_lines=runtime.route.completion_lines),
        )
        runtime.state_store.save(runtime.controller.state)
        await self._execute_effects(effects, runtime=runtime)
        self._drop_runtime_if_drained(runtime)

    async def _kick_idle_queue(self, *, runtime: BridgeRuntime, now: datetime | None = None) -> None:
        effects = runtime.controller.observe(active_running=False, now=now or _utcnow())
        if effects:
            runtime.state_store.save(runtime.controller.state)
            await self._execute_effects(effects, runtime=runtime)

    def _progress_due(self, runtime: BridgeRuntime, now: datetime) -> bool:
        active = runtime.controller.state.active
        if active is None:
            return False
        last_progress_at = datetime.fromisoformat(active.last_progress_at)
        return (now - last_progress_at).total_seconds() >= self._runtime_progress_interval_sec(runtime)

    async def _execute_effects(self, effects, *, runtime: BridgeRuntime | None = None) -> None:
        runtime = runtime or self.primary_runtime
        suppress_followup_message = False
        for effect in effects:
            if effect.kind == "dispatch" and effect.request is not None:
                try:
                    await asyncio.to_thread(
                        self.tmux.send_message,
                        runtime.route.tmux_session,
                        runtime.route.tmux_window,
                        runtime.route.tmux_pane,
                        effect.request.content,
                    )
                except Exception as exc:
                    LOGGER.exception("dispatch to tmux failed")
                    runtime.controller.rollback_failed_dispatch(effect.request.request_id)
                    runtime.state_store.save(runtime.controller.state)
                    suppress_followup_message = True
                    if self._should_notify_dispatch_error(runtime):
                        runtime.last_dispatch_error_at = _utcnow()
                        await self._send_runtime_message(
                            runtime,
                            f"转发到 tmux 失败，消息暂存未送达：{type(exc).__name__}: {exc}",
                        )
            elif effect.kind == "discord_message":
                if suppress_followup_message:
                    suppress_followup_message = False
                    continue
                await self._send_runtime_message(runtime, effect.text)

    def _should_notify_dispatch_error(self, runtime: BridgeRuntime) -> bool:
        if runtime.last_dispatch_error_at is None:
            return True
        elapsed = (_utcnow() - runtime.last_dispatch_error_at).total_seconds()
        return elapsed >= runtime.route.progress_interval_sec

    async def _send_runtime_message(self, runtime: BridgeRuntime, text: str) -> None:
        await self._ensure_runtime_channel(runtime)
        for chunk in split_discord_message(text):
            await runtime.channel.send(chunk)

    async def _handle_ai_message(
        self,
        *,
        message: discord.Message,
        command: ShortcutCommand,
        now: datetime,
        runtime: BridgeRuntime,
    ) -> None:
        if not command.argument:
            await self._send_runtime_message(runtime, "用法：`ai <text>`")
            return

        snapshot = await self._capture_ai_snapshot(runtime=runtime, now=now)
        workspace_root = await self._resolve_workspace_root(runtime=runtime)
        reply = await self.ai_runner.run(
            AiRequestContext(
                route_name=runtime.route.name,
                tmux_session=runtime.route.tmux_session,
                instruction=command.argument,
                author_name=message.author.display_name,
                workspace_root=workspace_root,
                latest_output="" if snapshot is None else snapshot.latest_output,
                running=(runtime.controller.state.active is not None) if snapshot is None else snapshot.running,
            )
        )
        await self._send_runtime_message(runtime, reply)

    async def _handle_fetch_message(
        self,
        *,
        command: ShortcutCommand,
        runtime: BridgeRuntime,
    ) -> None:
        lines = self._parse_fetch_lines(command.argument)
        if lines is None:
            await self._send_runtime_message(runtime, "用法：`f [lines]`，其中 `lines` 必须是正整数。")
            return

        snapshot = await self._capture_runtime_snapshot(runtime=runtime, lines=lines)
        text = snapshot.latest_output.strip() or "(latest tmux output is empty)"
        await self._send_runtime_message(runtime, text)

    async def _handle_help_message(self, *, runtime: BridgeRuntime) -> None:
        await self._send_runtime_message(runtime, build_shortcut_help_document())

    async def _handle_progress_settings_message(
        self,
        *,
        command: ShortcutCommand,
        runtime: BridgeRuntime,
    ) -> None:
        parsed = self._parse_progress_settings(command.argument)
        if parsed is None:
            await self._send_runtime_message(
                runtime,
                "用法：`p` 查看当前设置，或 `p <interval_sec> <lines>` 更新设置（interval 5-3600 秒，lines 20-2000 行）。",
            )
            return

        if parsed == "show":
            await self._send_runtime_message(
                runtime,
                f"当前自动抓取：每 {self._runtime_progress_interval_sec(runtime)} 秒，抓取 {self._runtime_progress_capture_lines(runtime)} 行。",
            )
            return

        interval_sec, capture_lines = parsed
        runtime.controller.state.progress_interval_sec_override = interval_sec
        runtime.controller.state.progress_capture_lines_override = capture_lines
        runtime.controller.progress_interval_sec = interval_sec
        runtime.state_store.save(runtime.controller.state)
        await self._send_runtime_message(
            runtime,
            f"已更新当前路由自动抓取：每 {interval_sec} 秒，抓取 {capture_lines} 行。",
        )

    def _parse_fetch_lines(self, raw_value: str) -> int | None:
        value = raw_value.strip()
        if not value:
            return DEFAULT_FETCH_LINES

        try:
            parsed = int(value)
        except ValueError:
            return None

        if parsed <= 0:
            return None
        return min(parsed, MAX_FETCH_LINES)

    def _parse_progress_settings(self, raw_value: str) -> tuple[int, int] | str | None:
        value = raw_value.strip()
        if not value:
            return "show"

        parts = value.split()
        if len(parts) != 2:
            return None

        try:
            interval_sec = int(parts[0])
            capture_lines = int(parts[1])
        except ValueError:
            return None

        if not (MIN_PROGRESS_INTERVAL_SEC <= interval_sec <= MAX_PROGRESS_INTERVAL_SEC):
            return None
        if not (MIN_PROGRESS_CAPTURE_LINES <= capture_lines <= MAX_PROGRESS_CAPTURE_LINES):
            return None
        return interval_sec, capture_lines

    def _runtime_progress_interval_sec(self, runtime: BridgeRuntime) -> int:
        return runtime.controller.state.progress_interval_sec_override or runtime.route.progress_interval_sec

    def _runtime_progress_capture_lines(self, runtime: BridgeRuntime) -> int:
        return runtime.controller.state.progress_capture_lines_override or runtime.route.progress_capture_lines

    async def _capture_ai_snapshot(self, *, runtime: BridgeRuntime, now: datetime) -> RuntimeSnapshot | None:
        try:
            snapshot = await self._capture_runtime_snapshot(runtime=runtime, lines=runtime.route.completion_lines)
        except Exception:
            LOGGER.exception("failed to capture tmux snapshot for ai command")
            return None

        try:
            return await self._reconcile_active_state(runtime=runtime, snapshot=snapshot, now=now)
        except Exception:
            LOGGER.exception("failed to reconcile tmux snapshot for ai command")
            return snapshot

    async def _resolve_workspace_root(self, *, runtime: BridgeRuntime) -> Path | None:
        get_pane_current_path = getattr(self.tmux, "get_pane_current_path", None)
        if not callable(get_pane_current_path):
            return None
        try:
            raw_path = await asyncio.to_thread(
                get_pane_current_path,
                runtime.route.tmux_session,
                runtime.route.tmux_window,
                runtime.route.tmux_pane,
            )
        except Exception:
            LOGGER.exception("failed to resolve tmux workspace root for ai command")
            return None

        value = str(raw_path).strip()
        if not value:
            return None
        return Path(value).expanduser().resolve()

    async def _send_channel_message(self, text: str) -> None:
        await self._send_runtime_message(self.primary_runtime, text)

    async def _capture_runtime_snapshot(self, *, runtime: BridgeRuntime, lines: int) -> RuntimeSnapshot:
        target = await asyncio.to_thread(
            self.tmux.resolve_pane_target,
            runtime.route.tmux_session,
            runtime.route.tmux_window,
            runtime.route.tmux_pane,
        )
        latest_output = await asyncio.to_thread(self.tmux.capture_tail, target, lines=lines)
        return RuntimeSnapshot(
            target=target,
            latest_output=latest_output,
            running=runtime_output_indicates_running(latest_output),
        )

    async def _reconcile_active_state(
        self,
        *,
        runtime: BridgeRuntime,
        snapshot: RuntimeSnapshot,
        now: datetime,
    ) -> RuntimeSnapshot:
        active = runtime.controller.state.active
        if active is None:
            self._reset_completion_settling(runtime)
            return snapshot
        if snapshot.running:
            self._reset_completion_settling(runtime)
            return snapshot

        if not should_treat_task_as_completed(
            started_at=active.started_at,
            now=now,
            probe_text=snapshot.latest_output,
            completion_text=snapshot.latest_output,
            startup_grace_sec=DISPATCH_STARTUP_GRACE_SEC,
        ):
            self._reset_completion_settling(runtime)
            return snapshot

        if not self._completion_output_has_settled(runtime=runtime, completion_text=snapshot.latest_output, now=now):
            return snapshot

        self._reset_completion_settling(runtime)
        effects = runtime.controller.observe(
            active_running=False,
            now=now,
            completion_excerpt=format_completion(snapshot.latest_output, last_lines=runtime.route.completion_lines),
        )
        runtime.state_store.save(runtime.controller.state)
        await self._execute_effects(effects, runtime=runtime)
        self._drop_runtime_if_drained(runtime)
        return await self._capture_runtime_snapshot(runtime=runtime, lines=runtime.route.completion_lines)

    def _completion_output_has_settled(
        self,
        *,
        runtime: BridgeRuntime,
        completion_text: str,
        now: datetime,
    ) -> bool:
        if runtime.settling_completion_text != completion_text:
            runtime.settling_completion_text = completion_text
            runtime.settling_started_at = now
            return False

        if runtime.settling_started_at is None:
            runtime.settling_started_at = now
            return False

        elapsed = (now - runtime.settling_started_at).total_seconds()
        return elapsed >= max(1, runtime.route.check_interval_sec)

    def _reset_completion_settling(self, runtime: BridgeRuntime) -> None:
        runtime.settling_completion_text = None
        runtime.settling_started_at = None

    async def _handle_running_message(
        self,
        *,
        message: discord.Message,
        command: ShortcutCommand | None,
        snapshot: RuntimeSnapshot,
        now: datetime,
        runtime: BridgeRuntime | None = None,
        fallback_content: str = "",
    ) -> None:
        runtime = runtime or self._resolve_runtime_from_message(message)
        if runtime is None:
            return

        if command and command.name == "esc":
            await asyncio.to_thread(
                self.tmux.send_escape,
                runtime.route.tmux_session,
                runtime.route.tmux_window,
                runtime.route.tmux_pane,
            )
            await self._send_runtime_message(runtime, "已发送 Escape，中断当前 Codex。")
            return

        if command and command.name == "queue":
            if not command.argument:
                await self._send_runtime_message(runtime, "用法：`q <text>`")
                return
            if runtime.controller.state.active is None:
                runtime.controller.claim_active(self._build_placeholder_request(message, now=now), now=now)
            request = self._make_request(message, content=command.argument, suffix="queue")
            position = runtime.controller.queue_request(request)
            runtime.state_store.save(runtime.controller.state)
            await self._send_runtime_message(runtime, f"已加入队列第 {position} 位，当前任务结束后会自动发送。")
            return

        if command and command.name == "queue_clear":
            removed = runtime.controller.clear_queue()
            runtime.state_store.save(runtime.controller.state)
            await self._send_runtime_message(runtime, f"已清空队列，共移除 {removed} 条。")
            return

        if command and command.name == "insert":
            if not command.argument:
                await self._send_runtime_message(runtime, "用法：`i <text>`")
                return
            await asyncio.to_thread(
                self.tmux.send_message,
                runtime.route.tmux_session,
                runtime.route.tmux_window,
                runtime.route.tmux_pane,
                command.argument,
            )
            await self._send_runtime_message(runtime, "已插入到运行中的 Codex。")
            return

        if command is None and fallback_content:
            await asyncio.to_thread(
                self.tmux.send_message,
                runtime.route.tmux_session,
                runtime.route.tmux_window,
                runtime.route.tmux_pane,
                fallback_content,
            )
            await self._send_runtime_message(runtime, "已插入到运行中的 Codex。")
            return

        await self._send_runtime_message(runtime, build_running_shortcut_help(snapshot.latest_output))

    async def _handle_idle_message(
        self,
        *,
        message: discord.Message,
        command: ShortcutCommand | None,
        fallback_content: str,
        now: datetime,
        runtime: BridgeRuntime | None = None,
    ) -> None:
        runtime = runtime or self._resolve_runtime_from_message(message)
        if runtime is None:
            return

        if command is None:
            request = self._make_request(message, content=fallback_content)
            effects = runtime.controller.start_request(request, now=now)
            runtime.state_store.save(runtime.controller.state)
            await self._execute_effects(effects, runtime=runtime)
            return

        if command.name == "esc":
            await self._send_runtime_message(runtime, "Codex 已结束运行，无需中断。")
            return

        if command.name == "queue_clear":
            removed = runtime.controller.clear_queue()
            runtime.state_store.save(runtime.controller.state)
            await self._send_runtime_message(runtime, f"Codex 已结束运行；已清空队列 {removed} 条。")
            return

        if command.name in {"queue", "insert"}:
            if not command.argument:
                usage = "q <text>" if command.name == "queue" else "i <text>"
                await self._send_runtime_message(runtime, f"用法：`{usage}`")
                return
            request = self._make_request(message, content=command.argument, suffix=command.name)
            effects = runtime.controller.start_request(request, now=now)
            runtime.state_store.save(runtime.controller.state)
            await self._execute_effects(effects, runtime=runtime)
            return

        request = self._make_request(message, content=fallback_content)
        effects = runtime.controller.start_request(request, now=now)
        runtime.state_store.save(runtime.controller.state)
        await self._execute_effects(effects, runtime=runtime)

    def _resolve_runtime_from_message(self, message: discord.Message) -> BridgeRuntime | None:
        return self.route_runtime(message.channel.id)

    def _make_request(self, message: discord.Message, *, content: str, suffix: str = "direct") -> DiscordRequest:
        return DiscordRequest(
            request_id=f"{message.id}:{suffix}",
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            content=content,
            created_at=_utcnow().isoformat(),
        )

    def _build_placeholder_request(self, message: discord.Message, *, now: datetime) -> DiscordRequest:
        return DiscordRequest(
            request_id=f"external-running:{int(now.timestamp())}",
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            content="(existing running Codex task)",
            created_at=now.isoformat(),
        )



def _build_message_content(message: discord.Message) -> str:
    parts = [message.content.strip()] if message.content.strip() else []
    parts.extend(attachment.url for attachment in message.attachments)
    return "\n\n".join(parts).strip()



def _utcnow() -> datetime:
    return datetime.now(timezone.utc)



def runtime_output_indicates_running(text: str) -> bool:
    return RUNNING_MARKER in text or pane_indicates_running(text)



def should_treat_task_as_completed(
    *,
    started_at: str,
    now: datetime,
    probe_text: str,
    completion_text: str,
    startup_grace_sec: int,
) -> bool:
    if pane_indicates_running(probe_text):
        return False

    started = datetime.fromisoformat(started_at)
    if (now - started).total_seconds() < startup_grace_sec:
        return False

    if RUNNING_MARKER in completion_text:
        return False

    return True

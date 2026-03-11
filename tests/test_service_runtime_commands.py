import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from discord_codex_bridge.ai import AiRequestContext
from discord_codex_bridge.config import Settings
from discord_codex_bridge.models import BridgeRouteConfig, DiscordRequest
from discord_codex_bridge.service import DiscordCodexBridge, RuntimeSnapshot
from discord_codex_bridge.shortcuts import ShortcutCommand


class FakeTmux:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, int, int, str]] = []
        self.escape_calls: list[tuple[str, int, int]] = []
        self.current_path = '/tmp/workspace'

    def send_message(self, requested_session: str, window: int, pane: int, message: str):
        self.sent_messages.append((requested_session, window, pane, message))
        return None

    def send_escape(self, requested_session: str, window: int, pane: int):
        self.escape_calls.append((requested_session, window, pane))
        return None

    def get_pane_current_path(self, requested_session: str, window: int, pane: int) -> str:
        return self.current_path


class FakeAiRunner:
    def __init__(self, reply: str = 'AI reply') -> None:
        self.reply = reply
        self.calls: list[AiRequestContext] = []

    async def run(self, context: AiRequestContext) -> str:
        self.calls.append(context)
        return self.reply


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        discord_bot_token='token',
        tmux_bin='/tmp/tmux',
        tmux_window=0,
        tmux_pane=0,
        check_interval_sec=5,
        progress_interval_sec=300,
        progress_capture_lines=220,
        completion_lines=100,
        bridges_config_path=tmp_path / 'bridges.local.json',
    )


def make_route(tmp_path: Path, *, name: str = 'alpha', channel_id: int = 123, tmux_session: str = 'session_alpha') -> BridgeRouteConfig:
    return BridgeRouteConfig(
        name=name,
        channel_id=channel_id,
        tmux_session=tmux_session,
        state_path=tmp_path / f'{name}.json',
        tmux_window=0,
        tmux_pane=0,
        check_interval_sec=5,
        progress_interval_sec=300,
        progress_capture_lines=220,
        completion_lines=100,
    )


def make_bridge(
    tmp_path: Path,
    *,
    routes: list[BridgeRouteConfig] | None = None,
    tmux: FakeTmux | None = None,
    ai_runner: FakeAiRunner | None = None,
) -> DiscordCodexBridge:
    return DiscordCodexBridge(
        make_settings(tmp_path),
        routes=routes or [make_route(tmp_path)],
        tmux_bridge=tmux or FakeTmux(),
        ai_runner=ai_runner,
    )


def make_message(content: str, *, channel_id: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        id=111,
        content=content,
        attachments=[],
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(bot=False, id=9, display_name='tester'),
    )


def test_running_plain_message_sends_message_immediately(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]
    snapshot = RuntimeSnapshot(target='pane', latest_output='line1\nline2', running=True)

    asyncio.run(
        bridge._handle_running_message(
            message=make_message('hello'),
            command=None,
            snapshot=snapshot,
            now=datetime.now(timezone.utc),
            runtime=runtime,
            fallback_content='hello',
        )
    )

    assert len(messages) == 1
    assert tmux.sent_messages == [('session_alpha', 0, 0, 'hello')]
    assert '已插入到运行中的 Codex' in messages[0]
    assert bridge.controller.state.queue == []


def test_running_queue_command_enqueues_without_dispatch(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []
    executed = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    async def fake_execute(effects, *, runtime=None) -> None:
        executed.extend(effects)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]
    bridge._execute_effects = fake_execute  # type: ignore[method-assign]
    snapshot = RuntimeSnapshot(target='pane', latest_output='running\nesc to interrupt', running=True)
    now = datetime.now(timezone.utc)

    asyncio.run(
        bridge._handle_running_message(
            message=make_message('q follow up'),
            command=ShortcutCommand(name='queue', argument='follow up'),
            snapshot=snapshot,
            now=now,
            runtime=runtime,
        )
    )

    assert executed == []
    assert runtime.controller.state.active is not None
    assert [item.content for item in runtime.controller.state.queue] == ['follow up']
    assert '已加入队列第 1 位' in messages[0]


def test_running_i_command_sends_message_immediately(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]
    snapshot = RuntimeSnapshot(target='pane', latest_output='running\nesc to interrupt', running=True)
    now = datetime.now(timezone.utc)

    asyncio.run(
        bridge._handle_running_message(
            message=make_message('i refine it'),
            command=ShortcutCommand(name='insert', argument='refine it'),
            snapshot=snapshot,
            now=now,
            runtime=runtime,
        )
    )

    assert tmux.sent_messages == [('session_alpha', 0, 0, 'refine it')]
    assert '已插入到运行中的 Codex' in messages[0]


def test_help_command_returns_shortcut_document_when_idle(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('h')))

    assert tmux.sent_messages == []
    assert runtime.controller.state.active is None
    assert runtime.controller.state.queue == []
    assert len(messages) == 1
    assert 'h' in messages[0]
    assert 'i <text>' in messages[0]
    assert '$insert' not in messages[0]


def test_help_command_returns_shortcut_document_when_busy(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    now = datetime.now(timezone.utc)
    runtime.controller.claim_active(
        DiscordRequest(
            request_id='running-task',
            channel_id=123,
            author_id=9,
            author_name='tester',
            content='existing',
            created_at=now.isoformat(),
        ),
        now=now,
    )
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('h')))

    assert tmux.sent_messages == []
    assert runtime.controller.state.active is not None
    assert runtime.controller.state.active.request_id == 'running-task'
    assert len(messages) == 1
    assert 'h' in messages[0]
    assert 'i <text>' in messages[0]


def test_idle_queue_command_starts_immediately(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    executed = []

    async def fake_execute(effects, *, runtime=None) -> None:
        executed.extend(effects)

    async def fake_send(_runtime, text: str) -> None:
        raise AssertionError(text)

    bridge._execute_effects = fake_execute  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]
    now = datetime.now(timezone.utc)

    asyncio.run(
        bridge._handle_idle_message(
            message=make_message('q follow up'),
            command=ShortcutCommand(name='queue', argument='follow up'),
            fallback_content='q follow up',
            now=now,
            runtime=runtime,
        )
    )

    assert runtime.controller.state.active is not None
    assert runtime.controller.state.active.content == 'follow up'
    assert runtime.controller.state.queue == []
    assert [effect.kind for effect in executed] == ['dispatch', 'discord_message']


def test_route_runtime_lookup_ignores_unknown_channel(tmp_path: Path):
    bridge = make_bridge(tmp_path)

    assert bridge.route_runtime(999) is None


def test_busy_route_does_not_block_other_route(tmp_path: Path):
    alpha = make_route(tmp_path, name='alpha', channel_id=123, tmux_session='session_alpha')
    beta = make_route(tmp_path, name='beta', channel_id=222, tmux_session='session_beta')
    bridge = make_bridge(tmp_path, routes=[alpha, beta])
    alpha_runtime = bridge.route_runtime(123)
    beta_runtime = bridge.route_runtime(222)
    assert alpha_runtime is not None and beta_runtime is not None
    alpha_runtime.controller.claim_active(
        DiscordRequest(
            request_id='alpha-running',
            channel_id=123,
            author_id=1,
            author_name='alpha',
            content='existing',
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
        now=datetime.now(timezone.utc),
    )
    executed: list[tuple[str, list[str]]] = []

    async def fake_execute(effects, *, runtime=None) -> None:
        assert runtime is not None
        executed.append((runtime.route.name, [effect.kind for effect in effects]))

    bridge._execute_effects = fake_execute  # type: ignore[method-assign]

    asyncio.run(
        bridge._handle_idle_message(
            message=make_message('hello beta', channel_id=222),
            command=None,
            fallback_content='hello beta',
            now=datetime.now(timezone.utc),
            runtime=beta_runtime,
        )
    )

    assert alpha_runtime.controller.state.active is not None
    assert alpha_runtime.controller.state.active.request_id == 'alpha-running'
    assert beta_runtime.controller.state.active is not None
    assert beta_runtime.controller.state.active.content == 'hello beta'
    assert executed == [('beta', ['dispatch', 'discord_message'])]


def test_ai_command_uses_ai_runner_instead_of_tmux_when_idle(tmp_path: Path):
    tmux = FakeTmux()
    ai_runner = FakeAiRunner(reply='file content here')
    bridge = make_bridge(tmp_path, tmux=tmux, ai_runner=ai_runner)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_capture_snapshot(*, runtime, lines: int):
        return RuntimeSnapshot(target='pane', latest_output='latest output', running=False)

    async def fake_reconcile_active_state(*, runtime, snapshot, now):
        return snapshot

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._capture_runtime_snapshot = fake_capture_snapshot  # type: ignore[method-assign]
    bridge._reconcile_active_state = fake_reconcile_active_state  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('ai 把这个文件发我')))

    assert tmux.sent_messages == []
    assert runtime.controller.state.active is None
    assert runtime.controller.state.queue == []
    assert messages == ['file content here']
    assert len(ai_runner.calls) == 1
    assert ai_runner.calls[0].instruction == '把这个文件发我'
    assert ai_runner.calls[0].workspace_root == Path('/tmp/workspace')
    assert ai_runner.calls[0].latest_output == 'latest output'


def test_ai_command_bypasses_busy_tmux_state(tmp_path: Path):
    tmux = FakeTmux()
    ai_runner = FakeAiRunner(reply='ai handled it')
    bridge = make_bridge(tmp_path, tmux=tmux, ai_runner=ai_runner)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    now = datetime.now(timezone.utc)
    runtime.controller.claim_active(
        DiscordRequest(
            request_id='running-task',
            channel_id=123,
            author_id=9,
            author_name='tester',
            content='existing',
            created_at=now.isoformat(),
        ),
        now=now,
    )
    messages: list[str] = []

    async def fake_capture_snapshot(*, runtime, lines: int):
        return RuntimeSnapshot(target='pane', latest_output='busy output', running=True)

    async def fake_reconcile_active_state(*, runtime, snapshot, now):
        return snapshot

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._capture_runtime_snapshot = fake_capture_snapshot  # type: ignore[method-assign]
    bridge._reconcile_active_state = fake_reconcile_active_state  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('ai 把这个文件发我')))

    assert tmux.sent_messages == []
    assert runtime.controller.state.active is not None
    assert runtime.controller.state.active.request_id == 'running-task'
    assert messages == ['ai handled it']
    assert len(ai_runner.calls) == 1
    assert ai_runner.calls[0].running is True


def test_fetch_command_returns_last_50_lines_by_default(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []
    requested_lines: list[int] = []

    async def fake_capture_snapshot(*, runtime, lines: int):
        requested_lines.append(lines)
        return RuntimeSnapshot(target='pane', latest_output='tail output', running=False)

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._capture_runtime_snapshot = fake_capture_snapshot  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('f')))

    assert requested_lines == [50]
    assert tmux.sent_messages == []
    assert runtime.controller.state.active is None
    assert runtime.controller.state.queue == []
    assert messages == ['tail output']


def test_fetch_command_accepts_custom_line_count(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    messages: list[str] = []
    requested_lines: list[int] = []

    async def fake_capture_snapshot(*, runtime, lines: int):
        requested_lines.append(lines)
        return RuntimeSnapshot(target='pane', latest_output='custom output', running=False)

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._capture_runtime_snapshot = fake_capture_snapshot  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('f 42')))

    assert requested_lines == [42]
    assert messages == ['custom output']


def test_fetch_command_bypasses_busy_tmux_state(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    now = datetime.now(timezone.utc)
    runtime.controller.claim_active(
        DiscordRequest(
            request_id='running-task',
            channel_id=123,
            author_id=9,
            author_name='tester',
            content='existing',
            created_at=now.isoformat(),
        ),
        now=now,
    )
    messages: list[str] = []
    requested_lines: list[int] = []

    async def fake_capture_snapshot(*, runtime, lines: int):
        requested_lines.append(lines)
        return RuntimeSnapshot(target='pane', latest_output='busy tail', running=True)

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._capture_runtime_snapshot = fake_capture_snapshot  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('f 12')))

    assert requested_lines == [12]
    assert runtime.controller.state.active is not None
    assert runtime.controller.state.active.request_id == 'running-task'
    assert messages == ['busy tail']


def test_fetch_command_rejects_invalid_line_count(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('f nope')))

    assert tmux.sent_messages == []
    assert runtime.controller.state.active is None
    assert runtime.controller.state.queue == []
    assert messages == ['用法：`f [lines]`，其中 `lines` 必须是正整数。']


def test_progress_settings_command_reports_current_route_settings(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('p')))

    assert tmux.sent_messages == []
    assert messages == ['当前自动抓取：每 300 秒，抓取 220 行。']


def test_progress_settings_command_updates_runtime_and_state(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('p 60 200')))

    assert tmux.sent_messages == []
    assert runtime.controller.progress_interval_sec == 60
    assert runtime.controller.state.progress_interval_sec_override == 60
    assert runtime.controller.state.progress_capture_lines_override == 200
    assert messages == ['已更新当前路由自动抓取：每 60 秒，抓取 200 行。']


def test_progress_settings_command_rejects_invalid_values(tmp_path: Path):
    tmux = FakeTmux()
    bridge = make_bridge(tmp_path, tmux=tmux)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    messages: list[str] = []

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('p 1 10')))

    assert tmux.sent_messages == []
    assert runtime.controller.progress_interval_sec == 300
    assert runtime.controller.state.progress_interval_sec_override is None
    assert runtime.controller.state.progress_capture_lines_override is None
    assert messages == ['用法：`p` 查看当前设置，或 `p <interval_sec> <lines>` 更新设置（interval 5-3600 秒，lines 20-2000 行）。']


def test_plain_message_does_not_start_new_request_while_completion_output_is_still_settling(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    runtime = bridge.route_runtime(123)
    assert runtime is not None
    now = datetime.now(timezone.utc)
    runtime.controller.claim_active(
        DiscordRequest(
            request_id='running-task',
            channel_id=123,
            author_id=9,
            author_name='tester',
            content='existing',
            created_at=(now - timedelta(seconds=10)).isoformat(),
        ),
        now=now - timedelta(seconds=10),
    )
    messages: list[str] = []
    executed = []

    async def fake_capture_snapshot(*, runtime, lines: int):
        return RuntimeSnapshot(target='pane', latest_output='still flushing final answer', running=False)

    async def fake_send(_runtime, text: str) -> None:
        messages.append(text)

    async def fake_execute(effects, *, runtime=None) -> None:
        executed.extend(effects)

    bridge._capture_runtime_snapshot = fake_capture_snapshot  # type: ignore[method-assign]
    bridge._send_runtime_message = fake_send  # type: ignore[method-assign]
    bridge._execute_effects = fake_execute  # type: ignore[method-assign]

    asyncio.run(bridge.on_message(make_message('next message')))

    assert runtime.controller.state.active is not None
    assert runtime.controller.state.active.request_id == 'running-task'
    assert executed == []
    assert len(messages) == 1
    assert 'Codex' in messages[0]

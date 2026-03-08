import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from discord_codex_bridge.config import Settings, load_bridge_routes
from discord_codex_bridge.models import ActiveTask
from discord_codex_bridge.service import (
    DiscordCodexBridge,
    RuntimeSnapshot,
    runtime_output_indicates_running,
    should_treat_task_as_completed,
)


class FakeTmux:
    def send_message(self, requested_session: str, window: int, pane: int, message: str):
        return None

    def send_escape(self, requested_session: str, window: int, pane: int):
        return None


def make_settings(tmp_path: Path, route_file: Path | None = None) -> Settings:
    return Settings(
        discord_bot_token='token',
        tmux_bin='/tmp/tmux',
        tmux_window=0,
        tmux_pane=0,
        check_interval_sec=5,
        progress_interval_sec=300,
        progress_capture_lines=220,
        completion_lines=100,
        bridges_config_path=(route_file or (tmp_path / 'bridges.local.json')).resolve(),
    )


def write_routes(path: Path, routes: list[dict]) -> None:
    path.write_text(json.dumps({'bridges': routes}))


def make_active(channel_id: int, request_id: str) -> ActiveTask:
    now = datetime.now(timezone.utc).isoformat()
    return ActiveTask(
        request_id=request_id,
        channel_id=channel_id,
        author_id=1,
        author_name='alpha',
        content='running',
        created_at=now,
        started_at=now,
        last_progress_at=now,
    )


def test_completion_is_suppressed_during_startup_grace_window():
    started_at = datetime(2026, 3, 8, 6, 10, tzinfo=timezone.utc)
    now = started_at + timedelta(seconds=1)

    assert should_treat_task_as_completed(
        started_at=started_at.isoformat(),
        now=now,
        probe_text='no marker here',
        completion_text='no marker here either',
        startup_grace_sec=3,
    ) is False


def test_completion_is_suppressed_if_completion_excerpt_still_has_marker():
    started_at = datetime(2026, 3, 8, 6, 10, tzinfo=timezone.utc)
    now = started_at + timedelta(seconds=6)

    assert should_treat_task_as_completed(
        started_at=started_at.isoformat(),
        now=now,
        probe_text='no marker here',
        completion_text='partial output\n◦ Working (2s • esc to interrupt)',
        startup_grace_sec=3,
    ) is False


def test_completion_is_allowed_after_grace_when_no_marker_remains():
    started_at = datetime(2026, 3, 8, 6, 10, tzinfo=timezone.utc)
    now = started_at + timedelta(seconds=6)

    assert should_treat_task_as_completed(
        started_at=started_at.isoformat(),
        now=now,
        probe_text='no marker here',
        completion_text='final answer\nPONG',
        startup_grace_sec=3,
    ) is True


def test_runtime_output_indicates_running_if_marker_exists_anywhere():
    output = 'header\npartial output\n◦ Working (2s • esc to interrupt)\nfooter'

    assert runtime_output_indicates_running(output) is True


def test_hot_reload_adds_new_route_without_dropping_running_route(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    write_routes(
        route_file,
        [
            {
                'name': 'alpha',
                'enabled': True,
                'channel_id': 111,
                'tmux_session': 'session_alpha',
                'state_path': './state/alpha.json',
            }
        ],
    )
    settings = make_settings(tmp_path, route_file=route_file)
    bridge = DiscordCodexBridge(settings, tmux_bridge=FakeTmux(), route_loader=load_bridge_routes)

    alpha_runtime = bridge.route_runtime(111)
    assert alpha_runtime is not None
    alpha_runtime.controller.state.active = make_active(111, 'alpha-active')

    write_routes(
        route_file,
        [
            {
                'name': 'alpha',
                'enabled': True,
                'channel_id': 111,
                'tmux_session': 'session_alpha',
                'state_path': './state/alpha.json',
            },
            {
                'name': 'beta',
                'enabled': True,
                'channel_id': 222,
                'tmux_session': 'session_beta',
                'state_path': './state/beta.json',
            },
        ],
    )

    asyncio.run(bridge.reload_if_config_changed(force=True))

    assert bridge.route_runtime(111) is not None
    assert bridge.route_runtime(222) is not None
    assert bridge.route_runtime(111).controller.state.active.request_id == 'alpha-active'


def test_hot_reload_keeps_last_known_good_routes_when_config_is_invalid(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    write_routes(
        route_file,
        [
            {
                'name': 'alpha',
                'enabled': True,
                'channel_id': 111,
                'tmux_session': 'session_alpha',
                'state_path': './state/alpha.json',
            }
        ],
    )
    settings = make_settings(tmp_path, route_file=route_file)
    bridge = DiscordCodexBridge(settings, tmux_bridge=FakeTmux(), route_loader=load_bridge_routes)

    route_file.write_text('{not valid json')
    changed = asyncio.run(bridge.reload_if_config_changed(force=True))

    assert changed is False
    assert bridge.route_runtime(111) is not None
    assert bridge.route_runtime(111).route.name == 'alpha'


def test_removed_route_becomes_draining_when_active(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    write_routes(
        route_file,
        [
            {
                'name': 'alpha',
                'enabled': True,
                'channel_id': 111,
                'tmux_session': 'session_alpha',
                'state_path': './state/alpha.json',
            }
        ],
    )
    settings = make_settings(tmp_path, route_file=route_file)
    bridge = DiscordCodexBridge(settings, tmux_bridge=FakeTmux(), route_loader=load_bridge_routes)

    alpha_runtime = bridge.route_runtime(111)
    assert alpha_runtime is not None
    alpha_runtime.controller.state.active = make_active(111, 'alpha-active')

    write_routes(route_file, [])
    asyncio.run(bridge.reload_if_config_changed(force=True))

    assert bridge.route_runtime(111) is None
    assert len(bridge._draining_runtimes) == 1
    assert bridge._draining_runtimes[0].route.name == 'alpha'


def test_reconcile_waits_for_non_running_output_to_stabilize_before_completion(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    write_routes(
        route_file,
        [
            {
                'name': 'alpha',
                'enabled': True,
                'channel_id': 111,
                'tmux_session': 'session_alpha',
                'state_path': './state/alpha.json',
            }
        ],
    )
    settings = make_settings(tmp_path, route_file=route_file)
    bridge = DiscordCodexBridge(settings, tmux_bridge=FakeTmux(), route_loader=load_bridge_routes)
    runtime = bridge.route_runtime(111)
    assert runtime is not None
    active = make_active(111, 'alpha-active')
    now = datetime(2026, 3, 8, 10, 40, tzinfo=timezone.utc)
    active.started_at = (now - timedelta(seconds=10)).isoformat()
    runtime.controller.state.active = active
    executed = []

    async def fake_execute(effects, *, runtime=None) -> None:
        executed.extend(effects)

    async def fake_capture(*, runtime, lines: int):
        return RuntimeSnapshot(target='pane', latest_output='final answer', running=False)

    bridge._execute_effects = fake_execute  # type: ignore[method-assign]
    bridge._capture_runtime_snapshot = fake_capture  # type: ignore[method-assign]

    first = asyncio.run(
        bridge._reconcile_active_state(
            runtime=runtime,
            snapshot=RuntimeSnapshot(target='pane', latest_output='final answer', running=False),
            now=now,
        )
    )

    assert first.running is False
    assert runtime.controller.state.active is not None
    assert executed == []

    second = asyncio.run(
        bridge._reconcile_active_state(
            runtime=runtime,
            snapshot=RuntimeSnapshot(target='pane', latest_output='final answer', running=False),
            now=now + timedelta(seconds=5),
        )
    )

    assert second.latest_output == 'final answer'
    assert runtime.controller.state.active is None
    assert any(effect.kind == 'discord_message' for effect in executed)


def test_progress_setting_overrides_are_restored_after_restart(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    write_routes(
        route_file,
        [
            {
                'name': 'alpha',
                'enabled': True,
                'channel_id': 111,
                'tmux_session': 'session_alpha',
                'state_path': './state/alpha.json',
            }
        ],
    )
    settings = make_settings(tmp_path, route_file=route_file)
    bridge = DiscordCodexBridge(settings, tmux_bridge=FakeTmux(), route_loader=load_bridge_routes)
    runtime = bridge.route_runtime(111)
    assert runtime is not None
    runtime.controller.state.progress_interval_sec_override = 60
    runtime.controller.state.progress_capture_lines_override = 200
    runtime.controller.progress_interval_sec = 60
    runtime.state_store.save(runtime.controller.state)

    restarted = DiscordCodexBridge(settings, tmux_bridge=FakeTmux(), route_loader=load_bridge_routes)
    restarted_runtime = restarted.route_runtime(111)
    assert restarted_runtime is not None

    assert restarted_runtime.controller.progress_interval_sec == 60
    assert restarted_runtime.controller.state.progress_interval_sec_override == 60
    assert restarted_runtime.controller.state.progress_capture_lines_override == 200

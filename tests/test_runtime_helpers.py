import json
from pathlib import Path

import pytest

from discord_codex_bridge.ai import (
    AiCommandRunner,
    AiRequestContext,
    CodexModelConfig,
    build_responses_api_url,
    load_codex_model_config,
)
from discord_codex_bridge.config import Settings, load_bridge_routes, load_env_file
from discord_codex_bridge.state import JsonStateStore
from discord_codex_bridge.shortcuts import parse_shortcut_command
from discord_codex_bridge.summary import split_discord_message


def test_load_env_file_applies_missing_values_only(tmp_path: Path):
    env_path = tmp_path / '.env'
    env_path.write_text('A=1\nB=two words\n')

    env = {'B': 'keep'}
    load_env_file(env_path, env)

    assert env['A'] == '1'
    assert env['B'] == 'keep'


def test_split_discord_message_breaks_long_text():
    text = 'x' * 4500

    parts = split_discord_message(text, limit=1900)

    assert len(parts) == 3
    assert ''.join(parts) == text
    assert all(len(part) <= 1900 for part in parts)


def test_settings_reads_tmux_bin_from_env(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
            'TMUX_BIN': '/custom/tmux',
        },
        base_dir=tmp_path,
    )

    assert settings.tmux_bin == '/custom/tmux'


def test_settings_default_check_interval_is_5_seconds(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
        },
        base_dir=tmp_path,
    )

    assert settings.check_interval_sec == 5


def test_settings_default_completion_lines_is_50(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
        },
        base_dir=tmp_path,
    )

    assert settings.completion_lines == 50


def test_settings_default_bridges_config_path_is_local_json(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
        },
        base_dir=tmp_path,
    )

    assert settings.bridges_config_path == (tmp_path / 'bridges.local.json').resolve()


def test_settings_reads_bridges_config_path_from_env(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
            'BRIDGES_CONFIG_PATH': './runtime/bridges.private.json',
        },
        base_dir=tmp_path,
    )

    assert settings.bridges_config_path == (tmp_path / 'runtime/bridges.private.json').resolve()


def test_loads_multiple_bridge_routes_from_local_json(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    route_file.write_text(
        json.dumps(
            {
                'defaults': {
                    'tmux_window': 1,
                    'tmux_pane': 2,
                    'progress_interval_sec': 180,
                },
                'bridges': [
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
                        'tmux_pane': 4,
                    },
                ],
            }
        )
    )
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
            'BRIDGES_CONFIG_PATH': str(route_file),
        },
        base_dir=tmp_path,
    )

    routes = load_bridge_routes(settings)

    assert [route.name for route in routes] == ['alpha', 'beta']
    assert routes[0].channel_id == 111
    assert routes[0].tmux_window == 1
    assert routes[0].tmux_pane == 2
    assert routes[0].progress_interval_sec == 180
    assert routes[0].state_path == (tmp_path / 'state/alpha.json').resolve()
    assert routes[1].tmux_pane == 4


def test_load_bridge_routes_ignores_disabled_entries(tmp_path: Path):
    route_file = tmp_path / 'bridges.local.json'
    route_file.write_text(
        json.dumps(
            {
                'bridges': [
                    {
                        'name': 'alpha',
                        'enabled': False,
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
                ]
            }
        )
    )
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
            'BRIDGES_CONFIG_PATH': str(route_file),
        },
        base_dir=tmp_path,
    )

    routes = load_bridge_routes(settings)

    assert [route.name for route in routes] == ['beta']


def test_settings_requires_discord_bot_token(tmp_path: Path):
    with pytest.raises(ValueError, match='DISCORD_BOT_TOKEN is required'):
        Settings.from_env({}, base_dir=tmp_path)


def test_load_codex_model_config_reads_model_and_auth_from_codex_files(tmp_path: Path):
    codex_dir = tmp_path / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'config.toml').write_text(
        '\n'.join(
            [
                'model = "gpt-5.4"',
                'model_provider = "gmn"',
                '',
                '[model_providers.gmn]',
                'name = "gmn"',
                'base_url = "https://gmn.example.com"',
                'wire_api = "responses"',
                'requires_openai_auth = true',
            ]
        )
    )
    (codex_dir / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'secret-key'}))

    config = load_codex_model_config(
        config_path=codex_dir / 'config.toml',
        auth_path=codex_dir / 'auth.json',
    )

    assert config.model == 'gpt-5.4'
    assert config.base_url == 'https://gmn.example.com'
    assert config.responses_api_url == 'https://gmn.example.com/v1/responses'
    assert config.api_key == 'secret-key'


def test_load_codex_model_config_reads_optional_provider_headers(tmp_path: Path):
    codex_dir = tmp_path / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'config.toml').write_text(
        '\n'.join(
            [
                'model = "gpt-5.4"',
                'model_provider = "gmn"',
                '',
                '[model_providers.gmn]',
                'name = "gmn"',
                'base_url = "https://gmn.example.com"',
                'wire_api = "responses"',
                'requires_openai_auth = true',
                '',
                '[model_providers.gmn.headers]',
                'User-Agent = "custom-agent/1.0"',
                'X-Debug = "enabled"',
            ]
        )
    )
    (codex_dir / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'secret-key'}))

    config = load_codex_model_config(
        config_path=codex_dir / 'config.toml',
        auth_path=codex_dir / 'auth.json',
    )

    assert config.extra_headers == {
        'User-Agent': 'custom-agent/1.0',
        'X-Debug': 'enabled',
    }


def test_ai_runner_sends_default_user_agent_when_provider_headers_missing(tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
        captured['url'] = url
        captured['payload'] = payload
        captured['headers'] = dict(headers)
        return {'output_text': 'ok', 'output': []}

    runner = AiCommandRunner(
        model_config=CodexModelConfig(
            model='gpt-5.4',
            base_url='https://gmn.example.com',
            responses_api_url='https://gmn.example.com/v1/responses',
            api_key='secret-key',
            extra_headers={},
        ),
        post_json=fake_post_json,
    )

    reply = runner._run_sync(
        AiRequestContext(
            route_name='alpha',
            tmux_session='session_alpha',
            instruction='read file',
            author_name='tester',
            workspace_root=tmp_path,
            latest_output='tail',
            running=False,
        )
    )

    assert reply == 'ok'
    assert captured['url'] == 'https://gmn.example.com/v1/responses'
    payload = captured['payload']
    assert isinstance(payload, dict)
    assert payload['model'] == 'gpt-5.4'
    assert isinstance(payload['tools'], list)
    assert isinstance(payload['input'], list)
    assert payload['input'][0]['role'] == 'user'
    assert payload['input'][0]['content'][0]['type'] == 'input_text'
    assert '用户请求: read file' in payload['input'][0]['content'][0]['text']
    assert captured['headers'] == {
        'Authorization': 'Bearer secret-key',
        'Content-Type': 'application/json',
        'User-Agent': 'codex-rs/1.0.7',
    }


def test_build_responses_api_url_preserves_existing_v1_suffix():
    assert build_responses_api_url('https://api.example.com/v1') == 'https://api.example.com/v1/responses'


def test_parse_shortcut_command_supports_help_and_short_insert():
    help_command = parse_shortcut_command('h')
    insert_command = parse_shortcut_command('i refine it')

    assert help_command is not None
    assert help_command.name == 'help'
    assert insert_command is not None
    assert insert_command.name == 'insert'
    assert insert_command.argument == 'refine it'


def test_parse_shortcut_command_no_longer_accepts_legacy_insert():
    assert parse_shortcut_command('$insert refine it') is None


def test_json_state_store_persists_progress_setting_overrides(tmp_path: Path):
    store = JsonStateStore(tmp_path / 'state.json')
    state = store.load()
    state.progress_interval_sec_override = 60
    state.progress_capture_lines_override = 200
    store.save(state)

    reloaded = store.load()

    assert reloaded.progress_interval_sec_override == 60
    assert reloaded.progress_capture_lines_override == 200

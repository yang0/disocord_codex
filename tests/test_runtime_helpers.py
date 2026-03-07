from pathlib import Path

from discord_codex_bridge.config import load_env_file
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

from discord_codex_bridge.config import Settings


def test_settings_reads_tmux_bin_from_env(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
            'DISCORD_CHANNEL_ID': '123',
            'TMUX_BIN': '/custom/tmux',
        },
        base_dir=tmp_path,
    )

    assert settings.tmux_bin == '/custom/tmux'


def test_settings_default_check_interval_is_5_seconds(tmp_path: Path):
    settings = Settings.from_env(
        {
            'DISCORD_BOT_TOKEN': 'token',
            'DISCORD_CHANNEL_ID': '123',
        },
        base_dir=tmp_path,
    )

    assert settings.check_interval_sec == 5

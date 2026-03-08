from discord_codex_bridge.shortcuts import (
    ShortcutCommand,
    build_running_shortcut_help,
    build_shortcut_help_document,
    parse_shortcut_command,
)


def test_parse_escape_command():
    assert parse_shortcut_command('e') == ShortcutCommand(name='esc', argument='')
    assert parse_shortcut_command('$e') == ShortcutCommand(name='esc', argument='')


def test_parse_queue_command_with_payload():
    assert parse_shortcut_command('q  ship it  ') == ShortcutCommand(name='queue', argument='ship it')
    assert parse_shortcut_command('$q  ship it  ') == ShortcutCommand(name='queue', argument='ship it')


def test_parse_progress_settings_command():
    assert parse_shortcut_command('p') == ShortcutCommand(name='progress_settings', argument='')
    assert parse_shortcut_command('p 60 200') == ShortcutCommand(name='progress_settings', argument='60 200')
    assert parse_shortcut_command('$p 60 200') == ShortcutCommand(name='progress_settings', argument='60 200')


def test_parse_queue_clear_command():
    assert parse_shortcut_command('qx') == ShortcutCommand(name='queue_clear', argument='')
    assert parse_shortcut_command('$qx') == ShortcutCommand(name='queue_clear', argument='')


def test_parse_help_command():
    assert parse_shortcut_command('h') == ShortcutCommand(name='help', argument='')
    assert parse_shortcut_command('$h') == ShortcutCommand(name='help', argument='')


def test_parse_insert_command_with_payload():
    assert parse_shortcut_command('i refine the last section') == ShortcutCommand(
        name='insert',
        argument='refine the last section',
    )
    assert parse_shortcut_command('$i refine the last section') == ShortcutCommand(
        name='insert',
        argument='refine the last section',
    )


def test_parse_returns_none_for_normal_message():
    assert parse_shortcut_command('hello codex') is None


def test_parse_does_not_misjudge_glued_text_as_shortcuts():
    assert parse_shortcut_command('hello') is None
    assert parse_shortcut_command('aixxx') is None
    assert parse_shortcut_command('f100') is None
    assert parse_shortcut_command('p60 200') is None
    assert parse_shortcut_command('qhello') is None
    assert parse_shortcut_command('escnow') is None


def test_running_help_includes_latest_output():
    text = build_running_shortcut_help('line1\nline2')

    assert 'h' in text
    assert 'e' in text
    assert 'q <text>' in text
    assert 'qx' in text
    assert 'i <text>' in text
    assert 'line1' in text
    assert 'line2' in text


def test_shortcut_help_document_uses_short_insert_alias():
    text = build_shortcut_help_document()

    assert 'h' in text
    assert 'i <text>' in text
    assert '$insert' not in text

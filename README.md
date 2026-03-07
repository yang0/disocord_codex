# Discord Codex Bridge

An independent sidecar service that bridges a Discord bot to a Codex session running inside tmux. The default tmux session target is `oc_backup`.

## What It Does

- Forwards messages from one configured Discord channel into tmux
- Queues later messages while Codex is still busy
- Checks for task completion every 5 seconds
- Sends a progress update every 300 seconds
- Sends the last 100 tmux lines when a task finishes
- Runs independently from OpenClaw so the bridge still works if OpenClaw is down

## Runtime Shortcuts

When Codex is already running, the bot supports these shortcuts:

- `$esc`: send `Esc` to interrupt the current Codex run
- `$q <text>`: add a message to the queue and send it after the current run finishes
- `$qx`: clear the queued messages
- `$insert <text>`: inject a message into the currently running Codex session immediately

If Codex is still running and the user sends a normal message instead of a shortcut, the bot responds with the shortcut help text plus the latest 100 tmux lines instead of silently queueing that message.

## Requirements

- Python 3.10+
- `tmux`
- A Discord bot already added to your server
- Discord `MESSAGE CONTENT INTENT` enabled for the bot
- A Codex TUI process already running in the target tmux pane

## Configuration

Copy `.env.example` to `.env` and set at least:

```env
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=your_target_channel_id
TMUX_BIN=/absolute/path/to/tmux
TMUX_SESSION=oc_backup
```

Notes:

- `TMUX_SESSION=oc_backup` resolves by exact session name first, then session group, then `oc_backup-*` prefix matches.
- `TMUX_BIN` should usually be set to an absolute path so the service still works under `systemd --user` with a restricted `PATH`.
- Running state is determined by whether the captured output still contains `esc to interrupt`.
- State is stored in the local `STATE_PATH` JSON file so active work and queued messages survive service restarts.

## Local Run

```bash
cd /path/to/disocord_codex
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python -m discord_codex_bridge --env-file .env
```

## Tests

```bash
cd /path/to/disocord_codex
pytest -q
```

## systemd

The sample unit file is `systemd/discord-codex-bridge.service` and is intended for `systemd --user`.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/discord-codex-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-codex-bridge.service
systemctl --user status discord-codex-bridge.service
```

## Design Boundaries

- The service only listens to one Discord channel to avoid forwarding unrelated traffic into tmux.
- The default behavior is request forwarding plus periodic progress and completion updates. It does not mirror every tmux line to Discord in real time.
- If the tmux target cannot be resolved, the service logs the error and retries on later monitor ticks instead of rewriting or dropping the user message.

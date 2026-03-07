# Discord Codex Bridge

独立 sidecar 服务，用 Discord bot 直接桥接到 tmux 里的 Codex，会话目标默认是 `oc_backup`。

目标能力：
- 把指定 Discord 频道里的消息原样转发到 tmux
- 如果 Codex 仍在运行，把后续消息排队
- 每 5 秒检查一次 Codex 是否结束
- 每 300 秒汇报一次进度
- 任务结束后发送最后 100 行 tmux 输出
- 不依赖 OpenClaw，可在 OpenClaw 挂掉时单独存活

## 当前默认频道

- 频道名：`dev-codex-backup`
- Channel ID：`1479951053494554736`
- Guild：`1477880687959736440`

## 运行要求

- Python 3.10+
- `tmux`
- Discord bot 已加入目标服务器
- Discord bot 已启用 `MESSAGE CONTENT INTENT`
- 目标 tmux pane 中运行着 Codex TUI

## 配置

从 `.env.example` 复制为 `.env`，至少填写：

```env
DISCORD_BOT_TOKEN=你的机器人 token
DISCORD_CHANNEL_ID=1479951053494554736
TMUX_BIN=/home/yang0/.local/bin/tmux
TMUX_SESSION=oc_backup
```

说明：
- `TMUX_SESSION=oc_backup` 会优先匹配精确 session 名；没有时再匹配 session group；还没有时匹配 `oc_backup-*`
- `TMUX_BIN` 建议直接配成 tmux 绝对路径，避免 systemd 用户服务拿到的 PATH 不完整
- Codex 运行判定只看 tmux 最后 10 行里是否出现 `esc to interrupt`
- 状态保存在 `STATE_PATH` 指向的本地 JSON 文件里，服务重启后会恢复 active task 和 queue

## 本地启动

```bash
cd /home/yang0/projectHome/disocord_codex
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python -m discord_codex_bridge --env-file .env
```

## 测试

```bash
cd /home/yang0/projectHome/disocord_codex
pytest -q
```

## systemd

示例 unit 在 `systemd/discord-codex-bridge.service`，按 `systemd --user` 部署。

安装后：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/discord-codex-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-codex-bridge.service
systemctl --user status discord-codex-bridge.service
```

## 设计边界

- 这个服务只认一个 Discord 频道，避免把别的频道噪音送进 tmux
- 服务默认只做“Discord -> tmux”和“定时进度/完成回报”；不会把 tmux 的每一行实时镜像到 Discord
- 如果 tmux 目标无法解析，当前实现会在日志里报错并在下一轮继续重试；不会自动改写或丢弃用户原消息

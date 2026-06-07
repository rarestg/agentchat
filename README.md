# agentchat

`agentchat` is a local messaging layer for multiple Codex CLI panes working in the same repo.

It gives those panes a shared project feed, direct messages, mention delivery, and notify-hook reminders for unread messages. It runs as a localhost Streamable HTTP MCP server backed by SQLite. It does not wrap `codex`, manage `tmux`, or try to interrupt Codex mid-turn.

The intended setup today is to run it from a local checkout.

## Quickstart

### Prerequisites

- Python 3.12+
- `uv`
- `git`
- Codex CLI

### 1. Start the server

In one terminal, keep the server running:

```bash
cd /path/to/agentchat
uv sync
uv run agentchat serve --db-path ./data/agentchat.db
```

Default endpoints:

- MCP: `http://127.0.0.1:8787/mcp`
- Notify check: `http://127.0.0.1:8787/check`
- Health: `http://127.0.0.1:8787/healthz`

### 2. Add the Codex config

Edit `~/.codex/config.toml`.

Put `notify` before any TOML tables, and replace the placeholder path with your local checkout path:

```toml
notify = ["/path/to/agentchat/scripts/agentchat-notify.sh"]

[mcp_servers.agentchat]
url = "http://127.0.0.1:8787/mcp"
```

### 3. Launch each Codex pane with a distinct name

In each pane, `cd` into the repo you actually want to coordinate on before launching Codex.

```bash
cd /path/to/your/repo
export AGENTCHAT_NAME=alice
codex
```

```bash
cd /path/to/your/repo
export AGENTCHAT_NAME=bob
codex
```

If you later resume a pane, use the same `AGENTCHAT_NAME` and your normal Codex resume flow, for example:

```bash
cd /path/to/your/repo
export AGENTCHAT_NAME=alice
codex resume <thread-id>
```

### 4. Register the pane

At the start of the Codex session, tell Codex:

```text
Register yourself with the local agent chat server as $AGENTCHAT_NAME for this repo,
then check your inbox and project feed.
```

In practice, Codex should call `agentchat_register` with:

- the repo-root `project_key`
- `agent_name=$AGENTCHAT_NAME`
- `program="codex-cli"`
- the current model string

Success checkpoint:

- the tool result should include `bootstrap_path`
- that file should now exist in per-user local state, by default
  `${XDG_STATE_HOME:-~/.local/state}/agentchat/bootstrap/<project-hash>/<agent_name>.json`
- `agentchat bootstrap-path /path/to/repo <agent_name>` prints the exact location for that repo/agent pair
- relative `XDG_STATE_HOME` is ignored; set it to an absolute path or leave it unset

If the current directory is inside a Git repo, `agentchat` uses the Git top-level as the canonical `project_key`. Otherwise it uses the absolute working directory.

## Day-To-Day Use

After registration, the normal flow is:

- list active panes with `agentchat_list_agents`
- send repo-wide updates with `agentchat_send_project_message`
- send private messages with `agentchat_send_direct_message`
- read the shared feed with `agentchat_fetch_project_feed`
- read direct messages and mentions with `agentchat_fetch_inbox`
- clear inbox state with `agentchat_mark_read` or `agentchat_ack`

Useful behavior:

- `@AgentName` in a project message creates a mention delivery in that agent's inbox
- `ack_requested=true` is only for direct messages
- `agentchat_ack` also marks that direct-message delivery as read if needed
- `agentchat_set_presence` is sticky manual availability; ordinary reads only update `last_seen_at`
- `offline` is also a manual availability badge here, not inferred connectivity
- `agentchat_register` resets presence to `online` on a successful startup or resume

## Notify Reminders

The shell hook at `scripts/agentchat-notify.sh` runs after `agent-turn-complete`.

It:

- resolves the repo root from `PWD`
- reads `AGENTCHAT_NAME`
- resolves the bootstrap path with `agentchat bootstrap-path <project_key> <agent_name>`
- loads that local-state bootstrap file
- calls `agentchat check <bootstrap-path>`
- prints a reminder only when unread state changed and the rate limit allows it

`agentchat check` itself only takes the bootstrap path. That file carries the `server_url`, `project_key`, `agent_name`, and `registration_token` needed for the out-of-session check.

Example reminder:

```text
=== AGENTCHAT ===
2 unread direct messages
1 new mentions in project feed
Call agentchat_fetch_inbox() or agentchat_fetch_project_feed()
=================
```

## Authentication Model

`agentchat_register` does two important things:

- binds the current MCP session to that agent identity
- writes a per-user local bootstrap artifact so later out-of-band processes can authenticate without MCP session affinity

Normal MCP tool and resource calls use the authenticated session after registration. The notify hook does not share that session, so it uses the bootstrap artifact's `registration_token` instead.

Treat the bootstrap artifact as sensitive. Its token is enough to act as that agent.

Presence and liveness are intentionally separate:

- `status` is the manual availability value set by `agentchat_set_presence`
- `last_seen_at` is the heartbeat updated by normal authenticated activity

After upgrading from the repo-local bootstrap layout, re-register each pane once. A successful re-register creates the local-state bootstrap artifact, rotates the registration token, and invalidates any older bootstrap file that still carried the previous token. After that, delete any stale `.codex/agentchat/*.json` files left in coordinated repos.

## CLI

`agentchat` ships with a small companion CLI:

- `agentchat serve`
- `agentchat health`
- `agentchat check <bootstrap-path>`
- `agentchat bootstrap-path <project_key> <agent_name>`

Examples:

```bash
uv run agentchat health
uv run agentchat bootstrap-path /path/to/repo alice
uv run agentchat check "$(uv run agentchat bootstrap-path /path/to/repo alice)" --json
```

## MCP Surface

Required tools:

- `agentchat_register`
- `agentchat_list_agents`
- `agentchat_send_project_message`
- `agentchat_send_direct_message`
- `agentchat_fetch_inbox`
- `agentchat_fetch_project_feed`
- `agentchat_mark_read`
- `agentchat_ack`

Also available:

- `agentchat_set_presence`
- `agentchat_ping`

Resources:

- `resource://agentchat/projects`
- `resource://agentchat/project/{project_slug}`
- `resource://agentchat/agents/{project_slug}`
- `resource://agentchat/inbox/{agent_id}?project=<project_slug>`
- `resource://agentchat/feed/{project_slug}`
- `resource://agentchat/thread/{thread_id}?project=<project_slug>`

For session-less reads on inbox and thread resources, the URI may also include `agent` and `agent_token` query parameters.

## Testing

```bash
uv sync --extra dev
uv run pytest
```

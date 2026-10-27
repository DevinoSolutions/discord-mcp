# Hardened local Discord MCP (Windows)

This fork runs Microck/discord.py-self-mcp with discord.py-self 2.1.0 as a
protected, always-on local service. It operates as the authenticated personal
account and inherits that account's actual Discord permissions across its
servers. It does not grant new permissions.

## Operating constraint

The service runs with a full-account session credential; Discord offers no way to
restrict what such a credential can do.

Do not test irreversible actions without the account owner's prior approval. Do not send live deletion requests (including probes with dummy IDs),
invoke destructive tools, transfer ownership, or click deletion confirmations to
verify protection. Use documentation and static inspection. Obtain explicit
authorization before any proposed destructive test; do not interpret this document
or prior setup authorization as permission for one.

`scripts/probe_oauth_scopes.py` tests whether a restricted OAuth token could replace
the session credential. It requests only `identify`, `guilds`, and
`guilds.members.read` (set `DISCORD_OAUTH_CLIENT_ID`, and optionally
`DISCORD_EXPECTED_USER_ID`) and stores the token encrypted in `oauth-probe.dpapi`.
Those scopes allow profile, server-list and member reads, but guild channel listing
returns 401 even on an owned server, so OAuth cannot provide general community
management. Sanitized results go to `oauth-probe-result.json`.

Static inspection confirms local guards for server/channel/role/message deletion,
ownership transfer, account deletion, pruning, and ban-related history deletion.
These are application restrictions, not Discord-enforced restrictions on the
full-account token. Protecting against bypass by an agent with this Windows user's
shell access would require a separate trust boundary that keeps both credentials
and policy control inaccessible to that agent.

## Connection

Persistent Streamable HTTP: `http://127.0.0.1:8085/mcp`.
The transport is stateless: no MCP session lives in this process, so a client
stays connected through idle hours and through service restarts. (A stateful
session was dropped after 30 idle minutes and on every restart, and each Claude
Code session then showed the server as disconnected until reconnected by hand.)
An independent local bearer token authenticates MCP requests.
Health: `http://127.0.0.1:8085/health` (connection status only).
Discord credentials and the local bearer token are encrypted using Windows DPAPI
for this Windows user in `credentials.dpapi`, which is excluded from Git.
The Discord token is not stored in the Codex configuration.

Windows Task Scheduler task: `Codex Discord Personal MCP`. It starts at this user's
Windows login, restarts on failure (up to 999 times, one minute apart), and runs without a console window. The service
holds an exclusive process lock to prevent multiple Discord connections. Runtime
logs are in `server.log`. This is a native Windows singleton, not the original bot
Docker container. The Codex server entry is named `discord-mcp`.

The port opens immediately at startup; the Discord login runs in the background,
and `/health` answers `503 connecting` until the gateway is READY (and again
whenever its socket drops). A supervisor logs and retries every gateway failure
with backoff (2s doubling to 60s, reset after READY). Tool calls wait up to
`DISCORD_READY_WAIT_SECONDS` (default 20) for the connection before failing. If
Discord stays disconnected for `DISCORD_WATCHDOG_SECONDS` (default 300), the
process exits with code 3 so the task's restart policy starts a fresh one; a
wrong account or rejected token exits with code 4.

Rate limits are shared by every client on the machine: writes 5/min (a 60s
cooldown when exceeded), messages 10/min, and reads (`read_messages`,
`search_messages`, `list_pings`, server overview samples) their own 30/min
budget (`RATE_LIMIT_READS_PER_MINUTE`) that never waits out a write cooldown.
Log lines from the supervisor and the rate limiter carry UTC timestamps.

## Enforced restrictions

- Only reviewed tool names are exposed and callable; new upstream tools are denied.
- Tool arguments are validated and unknown arguments rejected.
- No channel, category, thread, server, role, invite or message deletion tools.
- Native HTTP deletes are blocked except moderation kicks/unbans, removing a role
  assignment, and removing a reaction. These exceptions do not delete the objects.
- Banning a member must preserve all message history.
- No server ownership transfers or account password, email, MFA or identity changes.
- No arbitrary HTTP, shell commands, generic bot-command invocation, button clicks,
  menu submissions, or CAPTCHA solving.
- Browser cross-origin MCP calls are blocked, and the listener binds only to loopback.

Permission edits and moderation still have real consequences. Content or reactions
can trigger another bot's automation; these restrictions govern direct MCP/Discord
API operations, not downstream actions of independent bots. A full-account session
token itself cannot have its deletion powers removed. Anyone with unrestricted shell
access as this Windows user could change this local policy or decrypt its credentials.

Automating a personal account is not a Discord-supported integration and violates
Discord's self-bot policy; Discord may restrict or terminate the account.

## Install and update (pinned)

The source of truth is https://github.com/DevinoSolutions/discord-mcp. Installations
never run a branch: each one runs one reviewed commit, so nothing pushed to GitHub
reaches a machine on its own.

1. `git clone https://github.com/DevinoSolutions/discord-mcp` and
   `py -3.12 -m venv .venv` in the clone.
2. Pick the release to run from the repo's Releases page and copy its full commit SHA.
3. `.\scripts\deploy-pinned.ps1 -Commit <SHA> -Tag <tag>`. It refuses commits that
   are not on `main` and tags that point elsewhere, checks out the commit detached,
   installs `requirements-local.lock` with `--require-hashes`, and restarts the service.
4. First install only: capture a session (below), run `scripts/configure_codex.py`,
   then `scripts/install-startup.ps1`.

To update, read the new release's diff, then run step 3 with its SHA. Never `git pull`
in an installation. Propose changes as pull requests to `main`; upstream
(Microck/discord.py-self-mcp) changes come in the same way, never as a direct merge.
Do not run upstream setup tools that print the token in configuration.

## Credentials

If Discord expires/revokes the session, sign in again in the Stealth browser. Select
an authenticated `https://discord.com/api/` request from that browser's network
metadata, then run `.venv\Scripts\python.exe scripts/capture_network_session.py REQUEST_ID`.
Pass `--expected-username <name>` (or set `DISCORD_EXPECTED_USERNAME`) so the script
refuses to store a session that belongs to a different account.

The client connects with `afk=True`. A permanently connected session without it
makes Discord treat the account as active on desktop and withhold mobile push
notifications.
Reconfigure Codex after credential rotation because the local bearer token also rotates.
Use `scripts/configure_codex.py` for this, then restart the scheduled task.

To start after installation: `Start-ScheduledTask -TaskName 'Codex Discord Personal MCP'`.
To stop: `Stop-ScheduledTask -TaskName 'Codex Discord Personal MCP'`.
To verify read-only access: `.venv\Scripts\python.exe scripts/verify_connection.py`.
Restart Codex or reload MCP servers after changing its configuration.

Run checks with `.venv\Scripts\python.exe -m pytest`.

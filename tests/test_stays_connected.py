"""The fleet's Claude Code sessions stay connected across idle time and restarts.

Each Claude Code session on the machine keeps one MCP connection to this
service. They used to drop: the stateful transport forgot a session after 30
idle minutes and forgot all of them on every restart, and reads queued behind
the 5/min write budget's 60s cooldown. These pin the fixes.
"""
import asyncio
import json
import re
import time

import pytest
from starlette.testclient import TestClient

from discord_py_self_mcp import main
from discord_py_self_mcp.logging_utils import log_to_stderr
from discord_py_self_mcp.rate_limiter import RateLimitConfig, RateLimiter
from local_server import LocalAuth
from tests.test_gateway_supervisor import FakeClient, _cancel, _until, sleeps  # noqa: F401

BEARER = "x" * 48
MCP_HEADERS = {
    "authorization": f"Bearer {BEARER}",
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(main, "fatal_error", None)
    monkeypatch.setattr(main, "_token", None)
    monkeypatch.setattr(main, "gateway_task", None)
    monkeypatch.setattr(main, "watchdog_task", None)


def _rpc_result(response):
    """The JSON-RPC result from a JSON or an SSE-framed MCP response."""
    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        body = next(
            line[len("data:"):].strip()
            for line in body.splitlines()
            if line.startswith("data:")
        )
    return json.loads(body)["result"]


def test_the_http_transport_keeps_no_per_client_session():
    session_manager = main.create_streamable_http_app().routes[0].endpoint.session_manager

    assert session_manager.stateless is True


def test_a_client_holding_a_session_id_from_before_a_restart_still_gets_answers(monkeypatch):
    fake = FakeClient()
    fake.login_gate = asyncio.Event()  # Discord never needs to connect for tools/list
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    app = LocalAuth(main.create_streamable_http_app(), BEARER, main.connection_status)

    with TestClient(app, base_url="http://127.0.0.1:8085") as http:
        # No initialize first, and a session id this process never issued: exactly
        # what every Claude Code session sends after the service restarted.
        response = http.post(
            "/mcp",
            headers={**MCP_HEADERS, "mcp-session-id": "issued-by-the-previous-process"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in _rpc_result(response)["tools"]}
    assert "read_messages" in tool_names


@pytest.mark.asyncio
async def test_reads_are_not_blocked_by_a_write_cooldown(monkeypatch):
    limiter = RateLimiter(RateLimitConfig(enabled=True, actions_per_minute=5, reads_per_minute=30))
    limiter._cooldown_until = time.time() + 60  # a burst of writes just hit the limit

    await asyncio.wait_for(limiter.wait_if_needed("read"), timeout=1)

    assert limiter.get_stats()["reads_last_minute"] == 1
    assert limiter.get_stats()["actions_last_minute"] == 0


@pytest.mark.asyncio
async def test_reads_do_not_queue_behind_a_write_sleeping_out_its_cooldown(monkeypatch):
    limiter = RateLimiter(RateLimitConfig(enabled=True, actions_per_minute=1, cooldown_on_limit=60))
    await limiter.wait_if_needed("action")
    limiter._last_action_time = 0
    # The next write hits the limit and sleeps 60s while holding the write lock.
    write = asyncio.create_task(limiter.wait_if_needed("action"))
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(limiter.wait_if_needed("read"), timeout=1)
    finally:
        await _cancel(write)


@pytest.mark.asyncio
async def test_a_full_read_window_waits_for_its_oldest_read_not_a_cooldown(monkeypatch):
    limiter = RateLimiter(RateLimitConfig(enabled=True, reads_per_minute=2, cooldown_on_limit=60))
    now = time.time()
    limiter._read_timestamps = [now - 59.5, now - 1]
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)
        limiter._read_timestamps.pop(0)  # the oldest read ages out

    monkeypatch.setattr("discord_py_self_mcp.rate_limiter.asyncio.sleep", fake_sleep)

    await limiter.wait_if_needed("read")

    assert len(slept) == 1 and slept[0] <= 1
    assert limiter.get_cooldown_remaining() == 0


def test_read_tools_spend_the_read_budget_not_the_write_budget():
    from pathlib import Path

    tools = Path(main.__file__).parent / "tools"
    for module, count in (("messages.py", 2), ("inbox.py", 1), ("overview.py", 1)):
        source = (tools / module).read_text(encoding="utf-8")
        assert source.count('apply_rate_limit("read")') == count, module


def test_reads_per_minute_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_READS_PER_MINUTE", "12")

    assert RateLimiter._load_from_env().reads_per_minute == 12


@pytest.mark.asyncio
async def test_supervisor_logs_in_again_when_the_http_session_is_gone(monkeypatch, sleeps):  # noqa: F811
    fake = FakeClient([
        AttributeError("'_MissingSentinel' object has no attribute 'request'"),
        "hang",
    ])
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token", logged_in=True))
    await _until(lambda: fake.connects == 2)
    await _cancel(task)

    # Retrying connect() alone failed forever; a fresh login rebuilds the session.
    assert fake.logins == 1
    assert fake.clears == 1


@pytest.mark.asyncio
async def test_supervisor_keeps_the_login_for_other_attribute_errors(monkeypatch, sleeps):  # noqa: F811
    fake = FakeClient([AttributeError("'NoneType' object has no attribute 'id'"), "hang"])
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token", logged_in=True))
    await _until(lambda: fake.connects == 2)
    await _cancel(task)

    assert fake.logins == 0


def test_log_lines_carry_a_utc_timestamp(capsys):
    log_to_stderr("[GATEWAY] Reconnecting in 2.0s")

    line = capsys.readouterr().err
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[GATEWAY\] Reconnecting in 2\.0s\n", line)


def test_the_startup_task_keeps_restarting_the_service():
    from pathlib import Path

    script = (Path(main.__file__).parent.parent / "scripts" / "install-startup.ps1").read_text(encoding="utf-8")
    assert "-RestartCount 999 " in script


def test_a_deploy_survives_the_listener_exiting_before_it_is_stopped():
    from pathlib import Path

    script = (Path(main.__file__).parent.parent / "scripts" / "deploy-pinned.ps1").read_text(encoding="utf-8")
    assert "Stop-Process -Id $p -Force -ErrorAction SilentlyContinue" in script


@pytest.mark.asyncio
async def test_a_closed_discord_http_client_builds_fresh_sessions_after_the_reset(monkeypatch):
    # The real discord.py-self HTTPClient; only the user-agent lookup is stubbed,
    # because Headers.default fetches Discord's client build number over the network.
    from types import SimpleNamespace

    from discord import http as discord_http, utils as discord_utils

    async def offline_headers(cls, session, proxy=None, proxy_auth=None):
        return SimpleNamespace(user_agent="test-agent", super_properties={"client_build_number": 1})

    monkeypatch.setattr(discord_utils.Headers, "default", classmethod(offline_headers))
    http = discord_http.HTTPClient(loop=asyncio.get_running_loop())
    session = lambda: http._HTTPClient__session  # noqa: E731

    await http.startup()
    await http.close()
    http.clear()
    await http.startup()
    # The library's own path: startup() is a no-op after close, so the session
    # stays the MISSING sentinel and every request raises the AttributeError.
    assert session() is discord_utils.MISSING

    main._rebuild_http_on_next_request(http)
    await http.startup()
    try:
        assert session() is not discord_utils.MISSING
        assert not session()._closed
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_every_fresh_login_after_a_close_starts_from_a_rebuildable_http_client(monkeypatch, sleeps):  # noqa: F811
    fake = FakeClient(["closed-by-library", "hang"])
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token", logged_in=True))
    await _until(lambda: fake.connects == 2)
    await _cancel(task)

    # The library closed the client, so the supervisor logged in again, and
    # it reset the HTTP client first instead of reusing its dead sessions.
    assert fake.logins == 1
    assert fake.http_started_at_login == [False]

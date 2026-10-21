import asyncio
import time
from types import SimpleNamespace

import pytest
from curl_cffi import CurlError
from starlette.testclient import TestClient

from discord_py_self_mcp import main
from discord_py_self_mcp.bot import client as real_client
from local_server import LocalAuth

REAL_SLEEP = asyncio.sleep


class FakeWebSocket:
    def __init__(self, open_=True):
        self.open = open_


class FakeClient:
    """Stands in for SelfBot; each connect() consumes one scripted outcome."""

    def __init__(self, outcomes=(), user_id=123):
        self.outcomes = list(outcomes)
        self.user = SimpleNamespace(id=user_id)
        self.ws = None
        self.ready_count = 0
        self.closes = 0
        self.connects = 0
        self.logins = 0
        self.clears = 0
        self.login_gate = None
        self.login_outcomes = []
        self._ready = False
        self._closed = False

    async def login(self, token):
        self.logins += 1
        if self.login_gate is not None:
            await self.login_gate.wait()
        if self.login_outcomes:
            outcome = self.login_outcomes.pop(0)
            if outcome == "hang":
                await asyncio.Event().wait()
            raise outcome

    async def connect(self):
        self.connects += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "hang"
        if outcome == "hang":
            await asyncio.Event().wait()
        if outcome == "ready-then-drop":
            self.ready_count += 1
            raise CurlError("Failed to recv, Connection closed abruptly", 56)
        if outcome == "closed-by-library":
            self._closed = True
            raise RuntimeError("unrecoverable close code")
        raise outcome

    def is_ready(self):
        return self._ready

    def is_closed(self):
        return self._closed

    def clear(self):
        self.clears += 1
        self._closed = False

    async def _async_setup_hook(self):
        pass

    async def close(self):
        self.closes += 1
        self._closed = True


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []

    async def fake_sleep(delay, *args, **kwargs):
        recorded.append(delay)
        await REAL_SLEEP(0)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(main, "fatal_error", None)
    monkeypatch.setattr(main, "_token", None)
    monkeypatch.setattr(main, "gateway_task", None)
    monkeypatch.setattr(main, "watchdog_task", None)


async def _until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await REAL_SLEEP(0)


async def _cancel(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_supervisor_retries_after_curl_error_and_any_other_exception(
    monkeypatch, sleeps, capsys
):
    fake = FakeClient([
        CurlError("Failed to recv, Connection closed abruptly", 56),
        KeyError("unexpected"),
        "hang",
    ])
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token", logged_in=True))
    await _until(lambda: fake.connects == 3)
    await _cancel(task)

    assert task.cancelled()
    assert 2 <= sleeps[0] <= 3
    assert 4 <= sleeps[1] <= 6
    err = capsys.readouterr().err
    assert "[GATEWAY] CurlError: Failed to recv, Connection closed abruptly" in err
    assert "[GATEWAY] KeyError" in err


@pytest.mark.asyncio
async def test_supervisor_backoff_is_capped_and_resets_after_ready(monkeypatch, sleeps):
    failures = [RuntimeError("down")] * 7
    fake = FakeClient(failures + ["ready-then-drop", "hang"])
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token", logged_in=True))
    await _until(lambda: fake.connects == 9)
    await _cancel(task)

    assert all(delay <= main.BACKOFF_MAX_SECONDS for delay in sleeps)
    assert sleeps[6] == main.BACKOFF_MAX_SECONDS
    # The connection that reached READY resets the backoff to its start value.
    assert 2 <= sleeps[7] <= 3


@pytest.mark.asyncio
async def test_supervisor_logs_in_again_after_the_library_closed_the_client(
    monkeypatch, sleeps
):
    fake = FakeClient(["closed-by-library", "hang"])
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token", logged_in=True))
    await _until(lambda: fake.connects == 2)
    await _cancel(task)

    assert fake.clears == 1
    assert fake.logins == 1


@pytest.mark.asyncio
async def test_supervisor_stops_and_reports_an_error_on_account_mismatch(
    monkeypatch, sleeps, capsys
):
    fake = FakeClient(user_id=111)
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setenv("DISCORD_EXPECTED_USER_ID", "222")

    await asyncio.wait_for(main.run_gateway("token"), timeout=2)

    assert fake.connects == 0
    assert fake.is_closed()
    assert main.connection_status() == "error"
    assert "does not match the configured account" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="Discord rejected the session"):
        await main.call_tool("list_guilds", {})


@pytest.mark.asyncio
async def test_supervisor_retries_a_login_that_times_out(monkeypatch, sleeps, capsys):
    fake = FakeClient(["hang"])
    fake.login_outcomes = [TimeoutError(), ConnectionError("reset")]
    monkeypatch.setattr(main, "client", fake)

    task = asyncio.create_task(main.run_gateway("token"))
    await _until(lambda: fake.connects == 1)
    await _cancel(task)

    # Two failed logins, then a third that succeeds and reaches connect().
    assert fake.logins == 3
    assert fake.clears == 2
    assert main.fatal_error is None
    assert "[GATEWAY] TimeoutError" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_watchdog_restarts_the_connection_when_disconnected_past_the_threshold(
    monkeypatch, capsys
):
    restarts = []

    async def fake_restart(reason):
        restarts.append(reason)

    monkeypatch.setattr(main, "is_connected", lambda: False)
    monkeypatch.setattr(main, "restart_gateway", fake_restart)

    task = asyncio.create_task(main.watchdog(0.05, interval=0.01))
    await _until(lambda: len(restarts) >= 1)
    await _cancel(task)

    assert restarts[0].startswith("Discord not connected for")


@pytest.mark.asyncio
async def test_restart_gateway_starts_a_fresh_supervisor(monkeypatch, sleeps):
    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(main, "_token", "token")
    old = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(main, "gateway_task", old)

    await main.restart_gateway("test")
    try:
        assert old.cancelled()
        assert main.gateway_task is not None and main.gateway_task is not old
        await _until(lambda: fake.logins == 1)
    finally:
        await _cancel(main.gateway_task)


@pytest.mark.asyncio
async def test_watchdog_stays_quiet_while_connected(monkeypatch):
    restarts = []

    async def fake_restart(reason):
        restarts.append(reason)

    monkeypatch.setattr(main, "is_connected", lambda: True)
    monkeypatch.setattr(main, "restart_gateway", fake_restart)

    task = asyncio.create_task(main.watchdog(0.05, interval=0.01))
    await REAL_SLEEP(0.2)
    await _cancel(task)

    assert restarts == []


@pytest.mark.asyncio
async def test_call_tool_waits_for_the_connection_then_runs(monkeypatch):
    state = {"connected": False}
    monkeypatch.setattr(main, "is_connected", lambda: state["connected"])
    monkeypatch.setenv("DISCORD_READY_WAIT_SECONDS", "5")

    async def fake_call(name, arguments):
        return [name]

    monkeypatch.setattr(main.registry, "call_tool", fake_call)
    asyncio.get_running_loop().call_later(0.1, state.update, {"connected": True})

    assert await main.call_tool("list_guilds", {}) == ["list_guilds"]


@pytest.mark.asyncio
async def test_call_tool_fails_with_the_not_connected_error_after_the_wait(monkeypatch):
    monkeypatch.setattr(main, "is_connected", lambda: False)
    monkeypatch.setenv("DISCORD_READY_WAIT_SECONDS", "0.05")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="Discord is not connected yet"):
        await main.call_tool("list_guilds", {})
    assert time.monotonic() - started >= 0.05


def test_is_connected_requires_ready_and_an_open_gateway_socket(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)
    assert main.is_connected() is False

    fake._ready = True
    fake.ws = FakeWebSocket(open_=True)
    assert main.is_connected() is True

    # READY stays set after the socket dies; that alone must not count.
    fake.ws = FakeWebSocket(open_=False)
    assert main.is_connected() is False


@pytest.mark.asyncio
async def test_lifespan_does_not_wait_for_the_discord_login(monkeypatch):
    fake = FakeClient()
    fake.login_gate = asyncio.Event()  # login never completes
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setenv("DISCORD_TOKEN", "token")

    http_app = main.create_streamable_http_app()
    lifespan = http_app.router.lifespan_context(http_app)
    await asyncio.wait_for(lifespan.__aenter__(), timeout=2)
    try:
        await _until(lambda: fake.logins == 1)
        assert main.gateway_task is not None and not main.gateway_task.done()
        assert main.watchdog_task is not None
        assert main.is_connected() is False
    finally:
        await lifespan.__aexit__(None, None, None)

    assert main.gateway_task is None
    assert fake.is_closed()


def test_health_answers_connecting_while_login_is_still_pending(monkeypatch):
    fake = FakeClient()
    fake.login_gate = asyncio.Event()
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    app = LocalAuth(main.create_streamable_http_app(), "x" * 48, main.connection_status)

    with TestClient(app) as http:
        response = http.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "connecting"}


def test_health_reports_error_when_discord_rejected_the_session(monkeypatch):
    monkeypatch.setattr(main, "fatal_error", "Discord rejected the session")
    app = LocalAuth(main.create_streamable_http_app(), "x" * 48, main.connection_status)

    # No lifespan: only the /health route is under test.
    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error"}


def test_selfbot_skips_member_chunking_at_startup():
    assert real_client._connection._chunk_guilds is False

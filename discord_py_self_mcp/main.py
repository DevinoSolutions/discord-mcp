import asyncio
import argparse
from contextlib import asynccontextmanager
import ipaddress
import os
import random
import sys
import time

import discord
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from discord_py_self_mcp.bot import client
from discord_py_self_mcp.logging_utils import log_to_stderr
from discord_py_self_mcp.tools import registry

app = Server("discord-selfbot-mcp")
gateway_task = None
watchdog_task = None

BACKOFF_START_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
LOGIN_TIMEOUT_SECONDS = 60
NOT_CONNECTED_ERROR = 'Discord is not connected yet. Retry after the connection is ready.'

# Set when Discord rejects the session (wrong account or revoked token). Retrying
# cannot fix that and repeated logins with a bad token look abusive, so the
# supervisor stops, the HTTP server keeps answering, and every tool call and
# /health report this until the credentials are recaptured.
fatal_error: str | None = None
_token: str | None = None


class AccountMismatch(RuntimeError):
    """The token logged in as a different account than the one configured."""


def _env_seconds(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        log_to_stderr(f"[CONFIG] Ignoring {name}={value!r}: not a number")
        return default


def is_connected() -> bool:
    """True once READY has been reached while the gateway socket is still open."""
    if not client.is_ready():
        return False
    ws = client.ws
    try:
        return ws is not None and ws.open
    except Exception:
        return False


async def wait_until_connected(timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not is_connected():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.25, remaining))
    return True


@app.list_tools()
async def list_tools() -> list[Tool]:
    return registry.get_tool_definitions()


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[TextContent | ImageContent | EmbeddedResource]:
    if fatal_error:
        raise RuntimeError(fatal_error)
    # Ride out a reconnect or a fresh start instead of failing the first call.
    if not await wait_until_connected(_env_seconds('DISCORD_READY_WAIT_SECONDS', 20)):
        raise RuntimeError(NOT_CONNECTED_ERROR)
    return await registry.call_tool(name, arguments)


def _require_token() -> str:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.stderr.write(
            "Error: DISCORD_TOKEN is not set. Configure it in your MCP client or run "
            "`discord-py-self-mcp-setup`.\n"
        )
        raise SystemExit(1)
    return token


async def _login(token: str) -> None:
    await asyncio.wait_for(client.login(token), timeout=LOGIN_TIMEOUT_SECONDS)
    expected = os.getenv('DISCORD_EXPECTED_USER_ID')
    if expected and (not client.user or str(client.user.id) != expected):
        await client.close()
        raise AccountMismatch('Discord account does not match the configured account.')


def connection_status() -> str:
    """'ready', 'connecting', or 'error' (Discord rejected the session)."""
    if fatal_error:
        return 'error'
    return 'ready' if is_connected() else 'connecting'


def _http_session_gone(exc: BaseException) -> bool:
    """discord.py's HTTP client was torn down (its session is the MISSING sentinel)."""
    return isinstance(exc, AttributeError) and '_MissingSentinel' in str(exc)


async def run_gateway(token: str, *, logged_in: bool = False) -> None:
    """Keep the gateway connected for the life of the process.

    client.connect() only retries the errors discord.py-self knows about; anything
    else used to end the gateway task silently while the process stayed up and
    /health said "connecting" forever. Every failure, including a login that
    times out under load, is logged here and retried with capped exponential
    backoff plus jitter. A wrong account or a rejected token cannot be fixed by
    retrying, so those stop the supervisor and set fatal_error instead.
    """
    global fatal_error
    delay = BACKOFF_START_SECONDS
    while True:
        ready_before = getattr(client, 'ready_count', 0)
        try:
            if not logged_in:
                if client.is_closed():
                    # close() unbinds the event loop and login() only binds a
                    # fresh client, so reset both before logging in again.
                    client.clear()
                    await client._async_setup_hook()
                await _login(token)
                logged_in = True
            await client.connect()
            log_to_stderr("[GATEWAY] Connection loop ended")
        except (AccountMismatch, discord.LoginFailure) as exc:
            fatal_error = (
                f"Discord rejected the session ({type(exc).__name__}: {exc}). "
                "Recapture the credentials, then restart the service."
            )
            log_to_stderr(f"[GATEWAY] Fatal, not retrying: {type(exc).__name__}: {exc}")
            await client.close()
            return
        except Exception as exc:
            log_to_stderr(f"[GATEWAY] {type(exc).__name__}: {exc}")
            if logged_in and _http_session_gone(exc):
                # Seen in server.log: connect() kept failing on an HTTP session
                # that was already torn down, until the watchdog stepped in.
                # Only a fresh login rebuilds it.
                logged_in = False
            if not logged_in:
                # A half-finished login (timeout) leaves the HTTP session in an
                # unknown state; start the next attempt from a closed client.
                await client.close()
        if getattr(client, 'ready_count', 0) > ready_before:
            delay = BACKOFF_START_SECONDS
        if client.is_closed():
            # connect() closes the client on unrecoverable close codes; log in again.
            logged_in = False
        wait = min(BACKOFF_MAX_SECONDS, delay + random.uniform(0, delay / 2))
        log_to_stderr(f"[GATEWAY] Reconnecting in {wait:.1f}s")
        await asyncio.sleep(wait)
        delay = min(BACKOFF_MAX_SECONDS, delay * 2)


async def restart_gateway(reason: str) -> None:
    """Tear the Discord client down and start a fresh login, in this process.

    The HTTP listener stays up throughout, so MCP clients never see a refused
    connection. Task Scheduler does not restart a task whose process exited with
    a non-zero code, so exiting would leave the service down for good.
    """
    global gateway_task
    log_to_stderr(f"[WATCHDOG] {reason}. Restarting the Discord connection.")
    if gateway_task:
        gateway_task.cancel()
        await asyncio.gather(gateway_task, return_exceptions=True)
    await client.close()
    if fatal_error or not _token:
        gateway_task = None
        return
    gateway_task = asyncio.create_task(run_gateway(_token), name='discord-gateway')


async def watchdog(threshold: float, *, interval: float | None = None) -> None:
    """Restart the Discord connection when it stays down for threshold seconds.

    The supervisor cannot recover from every state (a socket that hangs without
    erroring, a connect() that never returns). A fresh client covers those.
    """
    if interval is None:
        interval = max(1.0, min(15.0, threshold / 10))
    down_since = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        if is_connected() or fatal_error:
            down_since = now
            continue
        if now - down_since >= threshold:
            await restart_gateway(
                f"Discord not connected for {now - down_since:.0f}s (limit {threshold:.0f}s)"
            )
            down_since = time.monotonic()


async def start_discord_client(
    *, background_login: bool = False, watchdog_seconds: float = 0
) -> None:
    """Log in and start the supervised gateway.

    With background_login the call returns at once and the login happens inside
    the supervisor, so an HTTP listener can bind before Discord answers.
    """
    global gateway_task, watchdog_task, _token
    token = _require_token()
    _token = token
    log_to_stderr("[STARTUP] Starting Discord connection")
    if not background_login:
        await _login(token)
    gateway_task = asyncio.create_task(
        run_gateway(token, logged_in=not background_login), name='discord-gateway'
    )
    if watchdog_seconds > 0:
        watchdog_task = asyncio.create_task(
            watchdog(watchdog_seconds), name='discord-watchdog'
        )


async def stop_discord_client() -> None:
    global gateway_task, watchdog_task
    # Cancel the watchdog first so it cannot start a new gateway mid-shutdown.
    for name in ('watchdog_task', 'gateway_task'):
        task = globals()[name]
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    gateway_task = watchdog_task = None
    await client.close()


async def run_stdio() -> None:
    # A stdio client launches us on demand, so the watchdog is opt-in here.
    await start_discord_client(watchdog_seconds=_env_seconds('DISCORD_WATCHDOG_SECONDS', 0))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        await stop_discord_client()


async def run_app() -> None:
    """Backward-compatible entry point for the original stdio server."""
    await run_stdio()


def _http_security_allowlist(host: str) -> tuple[list[str], list[str]]:
    """Return Host and Origin values accepted for one loopback listener."""
    normalized = host.strip().strip("[]")
    allowed_host = f"[{normalized}]" if ":" in normalized else normalized
    return (
        [allowed_host, f"{allowed_host}:*"],
        [f"http://{allowed_host}", f"http://{allowed_host}:*"],
    )


def create_streamable_http_app(host: str = "127.0.0.1"):
    """Build a Streamable HTTP MCP endpoint restricted to one loopback host."""
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Route

    if not _is_loopback_host(host):
        raise ValueError(
            "streamable-http host must be a loopback address; configure authentication before exposing it remotely"
        )

    # This server controls a user account, so defend the local endpoint against
    # DNS rebinding even when it is placed behind a local proxy or tunnel.
    allowed_hosts, allowed_origins = _http_security_allowlist(host)
    security_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    # Stateless: every request stands alone, so there is no per-client session to
    # lose. A stateful manager kept each MCP client's session in this process's
    # memory, dropped it after 30 idle minutes (the SDK's session_idle_timeout)
    # and lost all of them on every restart; the next call from each Claude Code
    # session then failed with "session not found" and the server showed as
    # disconnected until someone reconnected it by hand, fleet-wide. No tool
    # here streams progress or calls back into the client, so nothing needs one.
    session_manager = StreamableHTTPSessionManager(
        app=app, security_settings=security_settings, stateless=True
    )
    http_app = StreamableHTTPASGIApp(session_manager)

    @asynccontextmanager
    async def lifespan(_starlette_app):
        # Do not await the Discord login here: uvicorn only opens the port after
        # startup finishes, and MCP clients that connect at logon give up on a
        # refused connection. /health reports "connecting" until READY.
        await start_discord_client(
            background_login=True,
            watchdog_seconds=_env_seconds('DISCORD_WATCHDOG_SECONDS', 300),
        )
        try:
            async with session_manager.run():
                yield
        finally:
            await stop_discord_client()

    return Starlette(routes=[Route("/mcp", endpoint=http_app)], lifespan=lifespan)


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]")
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


async def run_streamable_http(host: str, port: int) -> None:
    import uvicorn

    if not _is_loopback_host(host):
        raise ValueError(
            "streamable-http host must be a loopback address; configure authentication before exposing it remotely"
        )
    server = uvicorn.Server(
        uvicorn.Config(
            create_streamable_http_app(host), host=host, port=port, log_level="info"
        )
    )
    await server.serve()


def main():
    parser = argparse.ArgumentParser(description="Discord self-account MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.getenv("MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "7781")))
    options = parser.parse_args()

    if options.transport == "streamable-http":
        if not _is_loopback_host(options.host):
            parser.error(
                "--host must be a loopback address for streamable-http; remote binding requires endpoint authentication"
            )
        asyncio.run(run_streamable_http(options.host, options.port))
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()

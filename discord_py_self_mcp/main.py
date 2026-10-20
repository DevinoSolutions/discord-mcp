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
# Non-zero so Task Scheduler's restart-on-failure policy brings the service back.
WATCHDOG_EXIT_CODE = 3
FATAL_EXIT_CODE = 4
NOT_CONNECTED_ERROR = 'Discord is not connected yet. Retry after the connection is ready.'

# Indirection so tests can observe a process exit without ending the test run.
_exit = os._exit


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


async def run_gateway(token: str, *, logged_in: bool = False) -> None:
    """Keep the gateway connected for the life of the process.

    client.connect() only retries the errors discord.py-self knows about; anything
    else used to end the gateway task silently while the process stayed up and
    /health said "connecting" forever. Every failure is logged here and retried
    with capped exponential backoff plus jitter. A wrong account or a rejected
    token cannot be fixed by retrying, so those end the process instead.
    """
    delay = BACKOFF_START_SECONDS
    while True:
        attempt_started = time.monotonic()
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
            log_to_stderr(f"[GATEWAY] Fatal: {type(exc).__name__}: {exc}. Exiting.")
            _exit(FATAL_EXIT_CODE)
            return
        except Exception as exc:
            log_to_stderr(f"[GATEWAY] {type(exc).__name__}: {exc}")
        if getattr(client, 'last_ready_at', 0.0) > attempt_started:
            delay = BACKOFF_START_SECONDS
        if client.is_closed():
            # connect() closes the client on unrecoverable close codes; log in again.
            logged_in = False
        wait = min(BACKOFF_MAX_SECONDS, delay + random.uniform(0, delay / 2))
        log_to_stderr(f"[GATEWAY] Reconnecting in {wait:.1f}s")
        await asyncio.sleep(wait)
        delay = min(BACKOFF_MAX_SECONDS, delay * 2)


async def watchdog(threshold: float, *, interval: float | None = None) -> None:
    """Exit the process when Discord stays disconnected for threshold seconds.

    The supervisor cannot recover from every state (a hung socket, a login that
    never completes). Exiting non-zero hands recovery to the service manager.
    """
    if interval is None:
        interval = max(1.0, min(15.0, threshold / 10))
    down_since = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        if is_connected():
            down_since = now
            continue
        if now - down_since >= threshold:
            log_to_stderr(
                f"[WATCHDOG] Discord not connected for {now - down_since:.0f}s "
                f"(limit {threshold:.0f}s). Exiting with code {WATCHDOG_EXIT_CODE} "
                "so the service manager restarts the process."
            )
            _exit(WATCHDOG_EXIT_CODE)
            return


async def start_discord_client(
    *, background_login: bool = False, watchdog_seconds: float = 0
) -> None:
    """Log in and start the supervised gateway.

    With background_login the call returns at once and the login happens inside
    the supervisor, so an HTTP listener can bind before Discord answers.
    """
    global gateway_task, watchdog_task
    token = _require_token()
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
    tasks = [task for task in (watchdog_task, gateway_task) if task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
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
    session_manager = StreamableHTTPSessionManager(
        app=app, security_settings=security_settings
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

import asyncio
import argparse
from contextlib import asynccontextmanager
import ipaddress
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from discord_py_self_mcp.bot import client
from discord_py_self_mcp.logging_utils import log_to_stderr
from discord_py_self_mcp.tools import registry

app = Server("discord-selfbot-mcp")
gateway_task = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    return registry.get_tool_definitions()


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[TextContent | ImageContent | EmbeddedResource]:
    if not client.is_ready():
        raise RuntimeError('Discord is not connected yet. Retry after the connection is ready.')
    return await registry.call_tool(name, arguments)


async def start_discord_client() -> None:
    global gateway_task
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.stderr.write(
            "Error: DISCORD_TOKEN is not set. Configure it in your MCP client or run "
            "`discord-py-self-mcp-setup`.\n"
        )
        raise SystemExit(1)

    log_to_stderr("[STARTUP] Starting Discord connection")
    await asyncio.wait_for(client.login(token), timeout=60)
    expected = os.getenv('DISCORD_EXPECTED_USER_ID')
    if expected and (not client.user or str(client.user.id) != expected):
        await client.close()
        raise RuntimeError('Discord account does not match the configured account.')
    gateway_task = asyncio.create_task(client.connect())


async def run_stdio() -> None:
    await start_discord_client()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


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
        await start_discord_client()
        try:
            async with session_manager.run():
                yield
        finally:
            await client.close()
            if gateway_task:
                gateway_task.cancel()

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

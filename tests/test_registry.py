import pytest
from mcp.types import TextContent

from discord_py_self_mcp.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_call_tool_rejects_unknown_name_before_dispatch():
    registry = ToolRegistry()

    async def alpha_handler(arguments):
        return [TextContent(type="text", text=str(arguments))]

    registry.register("alpha", "Alpha tool", {"type": "object"})(alpha_handler)

    with pytest.raises(PermissionError):
        await registry.call_tool("missing", {})


@pytest.mark.asyncio
async def test_blocked_tool_cannot_be_called_directly():
    registry = ToolRegistry()
    called = []

    async def handler(arguments):
        called.append(arguments)
        return []

    registry.register('delete_channel', 'Delete', {'type': 'object'})(handler)
    assert registry.get_tool_definitions() == []
    with pytest.raises(PermissionError):
        await registry.call_tool('delete_channel', {})
    assert called == []


@pytest.mark.asyncio
async def test_unexpected_arguments_do_not_reach_handler():
    registry = ToolRegistry()
    called = []

    async def handler(arguments):
        called.append(arguments)
        return []

    registry.register('list_guilds', 'List', {'type': 'object', 'properties': {}})(handler)
    with pytest.raises(ValueError):
        await registry.call_tool('list_guilds', {'arbitrary_argument': 'x'})
    assert called == []
    await registry.call_tool('list_guilds', {})
    assert called == [{}]

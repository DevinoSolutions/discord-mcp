from typing import Callable, Awaitable
from copy import deepcopy
from jsonschema import Draft202012Validator
from discord_py_self_mcp.local_safety import ALLOWED_TOOLS, check_arguments
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource

ToolHandler = Callable[[dict], Awaitable[list[TextContent | ImageContent | EmbeddedResource]]]

class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}
        self.handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, description: str, input_schema: dict):
        def decorator(func: ToolHandler):
            input_schema_local = deepcopy(input_schema)
            input_schema_local['additionalProperties'] = False
            if name == 'ban_member':
                input_schema_local['properties']['delete_message_days']['const'] = 0
            self.tools[name] = Tool(
                name=name,
                description=description,
                inputSchema=input_schema_local
            )
            self.handlers[name] = func
            return func
        return decorator

    def get_tool_definitions(self) -> list[Tool]:
        return [tool for name, tool in self.tools.items() if name in ALLOWED_TOOLS]

    async def call_tool(self, name: str, arguments: dict) -> list[TextContent | ImageContent | EmbeddedResource]:
        check_arguments(name, arguments)
        if name in self.tools:
            errors = list(Draft202012Validator(self.tools[name].inputSchema).iter_errors(arguments))
            if errors:
                raise ValueError('Invalid tool arguments: ' + errors[0].message)
        handler = self.handlers.get(name)
        if not handler:
            available_tools = ", ".join(sorted(self.handlers))
            raise ValueError(
                f"Unknown tool '{name}'. Available tools: {available_tools}"
            )
        return await handler(arguments)

registry = ToolRegistry()

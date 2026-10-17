from mcp.types import TextContent

from ..bot import client
from ..tool_utils import NOT_READY_TEXT, apply_rate_limit, format_user_display
from .registry import registry

@registry.register(
    name="list_guilds",
    description="List all guilds the user is in",
    input_schema={
        "type": "object",
        "properties": {
            "include_icons": {"type": "boolean", "description": "Append each server's icon URL"},
        },
    }
)
async def list_guilds(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]

    def line(g):
        if not arguments.get("include_icons"):
            return f"{g.name} ({g.id})"
        icon = g.icon.with_size(512).url if g.icon else "no icon"
        return f"{g.name} ({g.id}) {icon}"

    guilds = [line(g) for g in client.guilds]
    return [TextContent(type="text", text="\n".join(guilds))]

@registry.register(
    name="get_user_info",
    description="Get information about the current user",
    input_schema={
        "type": "object",
        "properties": {},
    }
)
async def get_user_info(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]

    user = client.user
    return [
        TextContent(
            type="text", text=f"User: {format_user_display(user)} ({user.id})"
        )
    ]


_ICON_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}
_ICON_MAX_BYTES = 10 * 1024 * 1024


def _read_icon(icon_path):
    """Read a local PNG/JPG/GIF for a guild icon; ``None`` when no path was given."""
    if icon_path is None:
        return None
    from pathlib import Path

    path = Path(icon_path)
    if not path.is_absolute():
        raise ValueError("icon_path must be an absolute path")
    if path.suffix.lower() not in _ICON_SUFFIXES:
        raise ValueError("icon_path must be a .png, .jpg, .jpeg or .gif file")
    data = path.read_bytes()
    if len(data) > _ICON_MAX_BYTES:
        raise ValueError("icon file is larger than 10 MB")
    return data


@registry.register(
    name="create_guild",
    description="Create a new server owned by the current user, optionally with an icon from a local PNG/JPG/GIF.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 2, "maxLength": 100},
            "icon_path": {"type": "string", "description": "Absolute path to a local PNG/JPG/GIF"},
        },
        "required": ["name"],
    }
)
async def create_guild(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]

    icon = _read_icon(arguments.get("icon_path"))
    await apply_rate_limit("guild_create")
    kwargs = {"name": arguments["name"]}
    if icon is not None:
        kwargs["icon"] = icon
    guild = await client.create_guild(**kwargs)
    return [TextContent(type="text", text=f"Created guild {guild.name} ({guild.id})")]


@registry.register(
    name="edit_guild",
    description="Edit a server's name, description or icon (local PNG/JPG/GIF). Only the fields you pass are changed.",
    input_schema={
        "type": "object",
        "properties": {
            "guild_id": {"type": "string"},
            "name": {"type": "string", "minLength": 2, "maxLength": 100},
            "description": {"type": "string", "maxLength": 120},
            "icon_path": {"type": "string", "description": "Absolute path to a local PNG/JPG/GIF"},
        },
        "required": ["guild_id"],
    }
)
async def edit_guild(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]

    guild = client.get_guild(int(arguments["guild_id"]))
    if guild is None:
        return [TextContent(type="text", text=f"Guild {arguments['guild_id']} not found")]

    changes = {key: arguments[key] for key in ("name", "description") if key in arguments}
    icon = _read_icon(arguments.get("icon_path"))
    if icon is not None:
        changes["icon"] = icon
    if not changes:
        raise ValueError("Pass at least one of name, description or icon_path")
    await apply_rate_limit("guild_edit")
    guild = await guild.edit(**changes)
    return [TextContent(type="text", text=f"Updated guild {guild.name} ({guild.id}): {', '.join(sorted(changes))}")]

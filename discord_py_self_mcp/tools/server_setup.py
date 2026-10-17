"""Server-readiness tools: read a server's whole setup (safety settings, AutoMod, bots, roles,
channels), and the write side used to bring a new server up to that standard."""
import datetime
import json

import discord
from mcp.types import TextContent

from ..bot import client
from ..tool_utils import NOT_READY_TEXT, apply_rate_limit
from .registry import registry


def _guild(arguments):
    guild = client.get_guild(int(arguments["guild_id"]))
    if guild is None:
        raise ValueError(f"Guild {arguments['guild_id']} not found")
    return guild


def _channel_ref(channel):
    return None if channel is None else {"id": str(channel.id), "name": channel.name}


def _overwrite_rows(channel):
    rows = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        rows.append({
            "target": getattr(target, "name", str(target.id)),
            "allow": [name for name, value in allow if value],
            "deny": [name for name, value in deny if value],
        })
    return rows


@registry.register(
    name="get_guild_setup",
    description=(
        "Read-only snapshot of how a server is set up: safety settings (verification level, content "
        "filter, notifications, MFA), community features and system/rules channels, AutoMod rules, "
        "installed bots, roles, and every channel with its topic, slowmode and permission overwrites."
    ),
    input_schema={
        "type": "object",
        "properties": {"guild_id": {"type": "string"}},
        "required": ["guild_id"],
    },
)
async def get_guild_setup(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]
    guild = _guild(arguments)
    out = {
        "id": str(guild.id),
        "name": guild.name,
        "description": guild.description,
        "member_count": guild.member_count,
        "verification_level": str(guild.verification_level),
        "explicit_content_filter": str(guild.explicit_content_filter),
        "default_notifications": str(guild.default_notifications),
        "mfa_level": str(guild.mfa_level),
        "features": sorted(guild.features),
        "system_channel": _channel_ref(guild.system_channel),
        "rules_channel": _channel_ref(guild.rules_channel),
        "public_updates_channel": _channel_ref(guild.public_updates_channel),
        "safety_alerts_channel": _channel_ref(getattr(guild, "safety_alerts_channel", None)),
    }
    try:
        out["automod_rules"] = [
            {
                "name": rule.name,
                "enabled": rule.enabled,
                "trigger": str(rule.trigger.type),
                "presets": [name for name, on in rule.trigger.presets if on] if rule.trigger.presets else None,
                "keywords": rule.trigger.keyword_filter or None,
                "mention_limit": rule.trigger.mention_limit or None,
                "actions": [
                    {k: v for k, v in {
                        "type": str(action.type),
                        "channel_id": str(action.channel_id) if action.channel_id else None,
                        "duration_s": action.duration.total_seconds() if action.duration else None,
                        "custom_message": action.custom_message,
                    }.items() if v}
                    for action in rule.actions
                ],
            }
            for rule in await guild.automod_rules()
        ]
    except discord.HTTPException as e:
        out["automod_rules"] = f"unavailable: {e}"
    try:
        out["integrations"] = [
            {
                "name": integration.name,
                "type": integration.type,
                "application_id": str(integration.application.id) if getattr(integration, "application", None) else None,
            }
            for integration in await guild.integrations()
        ]
    except discord.HTTPException as e:
        out["integrations"] = f"unavailable: {e}"
    out["roles"] = [
        {
            "name": role.name,
            "color": str(role.colour),
            "hoist": role.hoist,
            "managed": role.managed,
            "admin": role.permissions.administrator,
            "members": len(role.members),
        }
        for role in sorted(guild.roles, key=lambda r: -r.position)
    ]
    out["channels"] = []
    for channel in sorted(guild.channels, key=lambda c: (c.position, c.id)):
        row = {
            "id": str(channel.id),
            "name": channel.name,
            "type": str(channel.type),
            "category": channel.category.name if getattr(channel, "category", None) else None,
            "position": channel.position,
        }
        if getattr(channel, "topic", None):
            row["topic"] = channel.topic
        if getattr(channel, "slowmode_delay", 0):
            row["slowmode_s"] = channel.slowmode_delay
        overwrites = _overwrite_rows(channel)
        if overwrites:
            row["overwrites"] = overwrites
            row["synced"] = getattr(channel, "permissions_synced", None)
        out["channels"].append(row)
    return [TextContent(type="text", text=json.dumps(out, indent=1, default=str))]


_ENUM_ARGS = {
    "verification_level": discord.VerificationLevel,
    "explicit_content_filter": discord.ContentFilter,
    "default_notifications": discord.NotificationLevel,
}
_CHANNEL_ARGS = ("system_channel", "rules_channel", "public_updates_channel", "safety_alerts_channel")


@registry.register(
    name="edit_guild_settings",
    description=(
        "Change a server's safety and community settings. Only the fields you pass change. "
        "verification_level: none|low|medium|high|highest; explicit_content_filter: "
        "disabled|no_role|all_members; default_notifications: all_messages|only_mentions; "
        "*_channel_id: a text channel id, or '' to clear; community: turn the Community feature on/off "
        "(needs rules + public updates channels and verification >= low)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "guild_id": {"type": "string"},
            "verification_level": {"type": "string", "enum": ["none", "low", "medium", "high", "highest"]},
            "explicit_content_filter": {"type": "string", "enum": ["disabled", "no_role", "all_members"]},
            "default_notifications": {"type": "string", "enum": ["all_messages", "only_mentions"]},
            "system_channel_id": {"type": "string"},
            "rules_channel_id": {"type": "string"},
            "public_updates_channel_id": {"type": "string"},
            "safety_alerts_channel_id": {"type": "string"},
            "community": {"type": "boolean"},
            "raid_alerts_disabled": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["guild_id"],
    },
)
async def edit_guild_settings(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]
    guild = _guild(arguments)
    changes = {}
    for key, enum in _ENUM_ARGS.items():
        if key in arguments:
            changes[key] = enum[arguments[key]]
    for key in _CHANNEL_ARGS:
        if f"{key}_id" in arguments:
            raw = arguments[f"{key}_id"]
            channel = guild.get_channel(int(raw)) if raw else None
            if raw and channel is None:
                raise ValueError(f"Channel {raw} is not in this server")
            changes[key] = channel
    for key in ("community", "raid_alerts_disabled"):
        if key in arguments:
            changes[key] = bool(arguments[key])
    if not changes:
        raise ValueError("Pass at least one setting to change")
    await apply_rate_limit("guild_edit")
    await guild.edit(reason=arguments.get("reason") or "Server setup", **changes)
    return [TextContent(type="text", text=f"Updated {guild.name} ({guild.id}): {', '.join(sorted(changes))}")]


_PRESETS = ("profanity", "sexual_content", "slurs")


@registry.register(
    name="create_automod_rule",
    description=(
        "Create a Discord AutoMod rule. trigger: spam | mention_spam (mention_limit, "
        "mention_raid_protection) | keyword_preset (presets: profanity, sexual_content, slurs) | "
        "keyword (keywords, regex_patterns, allow_list). Actions: block (default true, optional "
        "custom_message), alert_channel_id, timeout_seconds (not for spam/keyword_preset)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "guild_id": {"type": "string"},
            "name": {"type": "string"},
            "trigger": {"type": "string", "enum": ["spam", "mention_spam", "keyword_preset", "keyword"]},
            "presets": {"type": "array", "items": {"type": "string", "enum": list(_PRESETS)}},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "regex_patterns": {"type": "array", "items": {"type": "string"}},
            "allow_list": {"type": "array", "items": {"type": "string"}},
            "mention_limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "mention_raid_protection": {"type": "boolean"},
            "block": {"type": "boolean", "default": True},
            "custom_message": {"type": "string", "maxLength": 150},
            "alert_channel_id": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 2419200},
            "exempt_role_ids": {"type": "array", "items": {"type": "string"}},
            "exempt_channel_ids": {"type": "array", "items": {"type": "string"}},
            "enabled": {"type": "boolean", "default": True},
        },
        "required": ["guild_id", "name", "trigger"],
    },
)
async def create_automod_rule(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]
    guild = _guild(arguments)
    kind = arguments["trigger"]
    trigger_kwargs = {"type": discord.AutoModRuleTriggerType[kind]}
    if kind == "keyword_preset":
        presets = arguments.get("presets") or list(_PRESETS)
        trigger_kwargs["presets"] = discord.AutoModPresets(**{p: True for p in presets})
        if arguments.get("allow_list"):
            trigger_kwargs["allow_list"] = arguments["allow_list"]
    elif kind == "keyword":
        if not (arguments.get("keywords") or arguments.get("regex_patterns")):
            raise ValueError("A keyword rule needs keywords or regex_patterns")
        trigger_kwargs["keyword_filter"] = arguments.get("keywords") or []
        trigger_kwargs["regex_patterns"] = arguments.get("regex_patterns") or []
        trigger_kwargs["allow_list"] = arguments.get("allow_list") or []
    elif kind == "mention_spam":
        trigger_kwargs["mention_limit"] = arguments.get("mention_limit", 5)
        trigger_kwargs["mention_raid_protection"] = arguments.get("mention_raid_protection", True)
    actions = []
    if arguments.get("block", True):
        actions.append(discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.block_message, custom_message=arguments.get("custom_message")))
    if arguments.get("alert_channel_id"):
        actions.append(discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.send_alert_message, channel_id=int(arguments["alert_channel_id"])))
    if arguments.get("timeout_seconds"):
        if kind in ("spam", "keyword_preset"):
            raise ValueError("Discord does not allow a timeout action on spam or keyword_preset rules")
        actions.append(discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.timeout,
            duration=datetime.timedelta(seconds=arguments["timeout_seconds"])))
    if not actions:
        raise ValueError("A rule needs at least one action")
    await apply_rate_limit("action")
    rule = await guild.create_automod_rule(
        name=arguments["name"],
        event_type=discord.AutoModRuleEventType.message_send,
        trigger=discord.AutoModTrigger(**trigger_kwargs),
        actions=actions,
        enabled=arguments.get("enabled", True),
        exempt_roles=[discord.Object(int(r)) for r in arguments.get("exempt_role_ids", [])],
        exempt_channels=[discord.Object(int(c)) for c in arguments.get("exempt_channel_ids", [])],
        reason="Server setup",
    )
    return [TextContent(type="text", text=f"Created AutoMod rule {rule.name} ({rule.id}) in {guild.name}")]


@registry.register(
    name="edit_automod_rule",
    description=(
        "Edit an existing AutoMod rule, found by its exact name: replace its allow_list, keywords "
        "or regex_patterns, or turn it on/off. Only passed fields change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "guild_id": {"type": "string"},
            "name": {"type": "string"},
            "allow_list": {"type": "array", "items": {"type": "string"}},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "regex_patterns": {"type": "array", "items": {"type": "string"}},
            "enabled": {"type": "boolean"},
        },
        "required": ["guild_id", "name"],
    },
)
async def edit_automod_rule(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]
    guild = _guild(arguments)
    rule = next((r for r in await guild.automod_rules() if r.name == arguments["name"]), None)
    if rule is None:
        raise ValueError(f"No AutoMod rule named {arguments['name']!r} in {guild.name}")
    changes = {}
    trigger_fields = {k: arguments[k] for k in ("allow_list", "keywords", "regex_patterns") if k in arguments}
    if trigger_fields:
        current = rule.trigger
        changes["trigger"] = discord.AutoModTrigger(
            type=current.type,
            keyword_filter=trigger_fields.get("keywords", current.keyword_filter),
            regex_patterns=trigger_fields.get("regex_patterns", current.regex_patterns),
            allow_list=trigger_fields.get("allow_list", current.allow_list),
            presets=current.presets,
            mention_limit=current.mention_limit,
        )
    if "enabled" in arguments:
        changes["enabled"] = arguments["enabled"]
    if not changes:
        raise ValueError("Pass at least one field to change")
    await apply_rate_limit("action")
    await rule.edit(reason="Server setup", **changes)
    return [TextContent(type="text", text=f"Updated AutoMod rule {rule.name} in {guild.name}: {', '.join(sorted(changes))}")]


@registry.register(
    name="edit_channel",
    description=(
        "Edit a server channel: name, topic, slowmode_seconds, position, category_id ('' = no "
        "category), sync_permissions (re-sync overwrites to its category). Only passed fields change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "channel_id": {"type": "string"},
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "topic": {"type": "string", "maxLength": 1024},
            "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
            "position": {"type": "integer", "minimum": 0},
            "category_id": {"type": "string"},
            "sync_permissions": {"type": "boolean"},
        },
        "required": ["channel_id"],
    },
)
async def edit_channel(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]
    channel = client.get_channel(int(arguments["channel_id"]))
    if channel is None or not hasattr(channel, "guild"):
        raise ValueError(f"Server channel {arguments['channel_id']} not found")
    changes = {}
    for key in ("name", "topic", "position", "sync_permissions"):
        if key in arguments:
            changes[key] = arguments[key]
    if "slowmode_seconds" in arguments:
        changes["slowmode_delay"] = arguments["slowmode_seconds"]
    if "category_id" in arguments:
        raw = arguments["category_id"]
        category = channel.guild.get_channel(int(raw)) if raw else None
        if raw and not isinstance(category, discord.CategoryChannel):
            raise ValueError(f"{raw} is not a category in this server")
        changes["category"] = category
    if not changes:
        raise ValueError("Pass at least one field to change")
    await apply_rate_limit("action")
    await channel.edit(reason="Server setup", **changes)
    return [TextContent(type="text", text=f"Updated #{channel.name} ({channel.id}): {', '.join(sorted(changes))}")]


@registry.register(
    name="authorize_bot",
    description=(
        "Add a bot application to a server you manage (the OAuth2 'Add to Server' step), e.g. "
        "RaidProtect. permissions is the bot's requested permission integer as a string."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "application_id": {"type": "string"},
            "guild_id": {"type": "string"},
            "permissions": {"type": "string", "pattern": "^[0-9]+$"},
            "scopes": {"type": "array", "items": {"type": "string"}, "default": ["bot", "applications.commands"]},
        },
        "required": ["application_id", "guild_id", "permissions"],
    },
)
async def authorize_bot(arguments: dict):
    if not client.is_ready():
        return [TextContent(type="text", text=NOT_READY_TEXT)]
    guild = _guild(arguments)
    if not guild.me.guild_permissions.manage_guild:
        raise PermissionError(f"You need Manage Server in {guild.name} to add a bot")
    scopes = arguments.get("scopes") or ["bot", "applications.commands"]
    if not set(scopes) <= {"bot", "applications.commands"}:
        raise ValueError("Only the bot and applications.commands scopes are allowed here")
    await apply_rate_limit("action")
    location = await client.create_authorization(
        int(arguments["application_id"]), scopes=scopes,
        guild=guild, permissions=discord.Permissions(int(arguments["permissions"])),
    )
    return [TextContent(type="text", text=f"Authorized application {arguments['application_id']} in {guild.name}: {location}")]

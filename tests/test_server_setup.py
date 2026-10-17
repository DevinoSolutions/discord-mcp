import json

import discord
import pytest

from discord_py_self_mcp.tools import server_setup


class FakeChannel:
    def __init__(self, channel_id, name="general"):
        self.id = channel_id
        self.name = name


class FakeRule:
    def __init__(self, rule_id=1, name="rule"):
        self.id = rule_id
        self.name = name


class FakeGuild:
    def __init__(self, guild_id=7, channels=(), manage_guild=True):
        self.id = guild_id
        self.name = "Guild"
        self._channels = {c.id: c for c in channels}
        self.edits = []
        self.rules = []
        self.me = type("Me", (), {"guild_permissions": discord.Permissions(manage_guild=manage_guild)})()

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def edit(self, **changes):
        self.edits.append(changes)
        return self

    async def create_automod_rule(self, **kwargs):
        self.rules.append(kwargs)
        return FakeRule(name=kwargs["name"])


class FakeClient:
    def __init__(self, guild):
        self._guild = guild
        self.authorizations = []

    def is_ready(self):
        return True

    def get_guild(self, guild_id):
        return self._guild if self._guild.id == guild_id else None

    async def create_authorization(self, application_id, /, *, scopes, **kwargs):
        self.authorizations.append((application_id, scopes, kwargs))
        return "https://discord.com/oauth2/authorized"


@pytest.fixture
def guild(monkeypatch):
    g = FakeGuild(channels=[FakeChannel(11, "rules"), FakeChannel(12, "mod-log")])
    monkeypatch.setattr(server_setup, "client", FakeClient(g))
    return g


@pytest.mark.asyncio
async def test_edit_guild_settings_maps_names_to_enums_and_channels(guild):
    await server_setup.edit_guild_settings({
        "guild_id": "7", "verification_level": "medium", "explicit_content_filter": "all_members",
        "default_notifications": "only_mentions", "rules_channel_id": "11",
    })

    (changes,) = guild.edits
    assert changes["verification_level"] is discord.VerificationLevel.medium
    assert changes["explicit_content_filter"] is discord.ContentFilter.all_members
    assert changes["default_notifications"] is discord.NotificationLevel.only_mentions
    assert changes["rules_channel"].id == 11


@pytest.mark.asyncio
async def test_edit_guild_settings_refuses_a_channel_from_another_server(guild):
    with pytest.raises(ValueError, match="not in this server"):
        await server_setup.edit_guild_settings({"guild_id": "7", "system_channel_id": "999"})


@pytest.mark.asyncio
async def test_edit_guild_settings_requires_a_change(guild):
    with pytest.raises(ValueError, match="at least one"):
        await server_setup.edit_guild_settings({"guild_id": "7"})


@pytest.mark.asyncio
async def test_keyword_preset_rule_blocks_and_alerts_with_all_presets_by_default(guild):
    await server_setup.create_automod_rule({
        "guild_id": "7", "name": "Slurs", "trigger": "keyword_preset", "alert_channel_id": "12",
    })

    (rule,) = guild.rules
    assert rule["trigger"].type is discord.AutoModRuleTriggerType.keyword_preset
    assert rule["trigger"].presets.profanity and rule["trigger"].presets.slurs
    assert [a.type for a in rule["actions"]] == [
        discord.AutoModRuleActionType.block_message, discord.AutoModRuleActionType.send_alert_message]
    assert rule["actions"][1].channel_id == 12
    assert rule["enabled"] is True


@pytest.mark.asyncio
async def test_timeout_is_refused_where_discord_does_not_allow_it(guild):
    with pytest.raises(ValueError, match="timeout"):
        await server_setup.create_automod_rule({
            "guild_id": "7", "name": "Spam", "trigger": "spam", "timeout_seconds": 60,
        })


@pytest.mark.asyncio
async def test_mention_spam_rule_defaults_to_raid_protection(guild):
    await server_setup.create_automod_rule({
        "guild_id": "7", "name": "Mentions", "trigger": "mention_spam", "timeout_seconds": 600,
    })

    trigger = guild.rules[0]["trigger"]
    assert trigger.mention_limit == 5 and trigger.mention_raid_protection is True


@pytest.mark.asyncio
async def test_authorize_bot_passes_guild_and_permissions(guild, monkeypatch):
    await server_setup.authorize_bot({"application_id": "42", "guild_id": "7", "permissions": "8"})

    ((app_id, scopes, kwargs),) = server_setup.client.authorizations
    assert app_id == 42 and scopes == ["bot", "applications.commands"]
    assert kwargs["guild"] is guild and kwargs["permissions"].administrator


@pytest.mark.asyncio
async def test_authorize_bot_refuses_other_scopes(guild):
    with pytest.raises(ValueError, match="scopes"):
        await server_setup.authorize_bot({
            "application_id": "42", "guild_id": "7", "permissions": "0", "scopes": ["bot", "guilds.join"],
        })


@pytest.mark.asyncio
async def test_authorize_bot_needs_manage_server(monkeypatch):
    g = FakeGuild(manage_guild=False)
    monkeypatch.setattr(server_setup, "client", FakeClient(g))
    with pytest.raises(PermissionError, match="Manage Server"):
        await server_setup.authorize_bot({"application_id": "42", "guild_id": "7", "permissions": "0"})


class FakeExistingRule:
    def __init__(self):
        self.name = "Block invites"
        self.trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword, regex_patterns=["x"], allow_list=["old"])
        self.edits = []

    async def edit(self, **changes):
        self.edits.append(changes)


@pytest.mark.asyncio
async def test_edit_automod_rule_replaces_allow_list_and_keeps_the_rest(guild):
    rule = FakeExistingRule()

    async def automod_rules():
        return [rule]

    guild.automod_rules = automod_rules
    await server_setup.edit_automod_rule({"guild_id": "7", "name": "Block invites", "allow_list": ["a", "b"]})

    (changes,) = rule.edits
    assert changes["trigger"].allow_list == ["a", "b"]
    assert changes["trigger"].regex_patterns == ["x"]
    assert changes["trigger"].type is discord.AutoModRuleTriggerType.keyword


@pytest.mark.asyncio
async def test_edit_automod_rule_needs_an_existing_rule(guild):
    async def automod_rules():
        return []

    guild.automod_rules = automod_rules
    with pytest.raises(ValueError, match="No AutoMod rule"):
        await server_setup.edit_automod_rule({"guild_id": "7", "name": "Nope", "enabled": False})

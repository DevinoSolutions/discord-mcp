import pytest

from discord_py_self_mcp.tools import channels


class FakeCategory:
    def __init__(self, name):
        self.name = name
        self.id = 321


class FakeGuild:
    id = 7

    def __init__(self):
        self.created = []

    def get_channel(self, channel_id):
        return None

    async def create_category(self, name):
        self.created.append(name)
        return FakeCategory(name)


class FakeClient:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None


@pytest.mark.asyncio
async def test_create_channel_can_create_a_category(monkeypatch):
    guild = FakeGuild()
    monkeypatch.setattr(channels, "client", FakeClient(guild))

    result = await channels.create_channel({"guild_id": "7", "name": "START HERE", "type": "category"})

    assert guild.created == ["START HERE"]
    assert "(321)" in result[0].text

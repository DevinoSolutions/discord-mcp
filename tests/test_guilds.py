import pytest

from discord_py_self_mcp.tools import guilds


class FakeUser:
    def __init__(self, *, user_id=99, name="tester", global_name=None, discriminator="0"):
        self.id = user_id
        self.name = name
        self.global_name = global_name
        self.discriminator = discriminator


class FakeGuild:
    def __init__(self, name="Guild", guild_id=1):
        self.name = name
        self.id = guild_id


class FakeClient:
    def __init__(self, *, ready=True, user=None, guild_list=None):
        self._ready = ready
        self.user = user or FakeUser()
        self.guilds = guild_list or []

    def is_ready(self):
        return self._ready


@pytest.mark.asyncio
async def test_list_guilds_returns_not_ready_text(monkeypatch):
    monkeypatch.setattr(guilds, "client", FakeClient(ready=False))

    result = await guilds.list_guilds({})

    assert result[0].text == guilds.NOT_READY_TEXT


@pytest.mark.asyncio
async def test_get_user_info_prefers_global_name(monkeypatch):
    fake_user = FakeUser(user_id=123, name="handle", global_name="Display Name")
    monkeypatch.setattr(guilds, "client", FakeClient(user=fake_user))

    result = await guilds.get_user_info({})

    assert result[0].text == "User: Display Name (@handle) (123)"


class FakeEditableGuild(FakeGuild):
    def __init__(self, name="Guild", guild_id=1):
        super().__init__(name=name, guild_id=guild_id)
        self.edits = []

    async def edit(self, **changes):
        self.edits.append(changes)
        if "name" in changes:
            self.name = changes["name"]
        return self


class FakeCreatingClient(FakeClient):
    def __init__(self, guild=None):
        super().__init__()
        self.created = []
        self._guild = guild

    async def create_guild(self, **kwargs):
        self.created.append(kwargs)
        return FakeGuild(name=kwargs["name"], guild_id=555)

    def get_guild(self, guild_id):
        return self._guild if self._guild and self._guild.id == guild_id else None


@pytest.mark.asyncio
async def test_create_guild_passes_name_and_icon_bytes(monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG fake")
    fake = FakeCreatingClient()
    monkeypatch.setattr(guilds, "client", fake)

    result = await guilds.create_guild({"name": "VoiceLabs", "icon_path": str(icon)})

    assert fake.created == [{"name": "VoiceLabs", "icon": b"\x89PNG fake"}]
    assert result[0].text == "Created guild VoiceLabs (555)"


@pytest.mark.asyncio
async def test_create_guild_refuses_a_non_image_icon(monkeypatch, tmp_path):
    bad = tmp_path / "icon.txt"
    bad.write_text("nope")
    fake = FakeCreatingClient()
    monkeypatch.setattr(guilds, "client", fake)

    with pytest.raises(ValueError, match="png"):
        await guilds.create_guild({"name": "VoiceLabs", "icon_path": str(bad)})
    assert fake.created == []


@pytest.mark.asyncio
async def test_edit_guild_changes_only_what_was_passed(monkeypatch):
    target = FakeEditableGuild(guild_id=7)
    monkeypatch.setattr(guilds, "client", FakeCreatingClient(guild=target))

    result = await guilds.edit_guild({"guild_id": "7", "description": "Hi"})

    assert target.edits == [{"description": "Hi"}]
    assert "description" in result[0].text


@pytest.mark.asyncio
async def test_edit_guild_requires_a_change(monkeypatch):
    monkeypatch.setattr(guilds, "client", FakeCreatingClient(guild=FakeEditableGuild(guild_id=7)))

    with pytest.raises(ValueError, match="at least one"):
        await guilds.edit_guild({"guild_id": "7"})

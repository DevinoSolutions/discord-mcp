from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from discord_py_self_mcp.tools import profile
from discord_py_self_mcp.tools.registry import registry


@pytest.mark.asyncio
async def test_edit_profile_schema_only_exposes_supported_fields():
    schema = registry.tools["edit_profile"].inputSchema

    assert sorted(schema["properties"]) == ["accent_color", "bio"]
    assert "password" not in schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize('color', [0, 16711680])
async def test_edit_profile_serializes_color_with_installed_discord_library(monkeypatch, color):
    user_data = {'id': '123', 'username': 'test', 'discriminator': '0', 'avatar': None}
    http = SimpleNamespace(edit_profile=AsyncMock(return_value=user_data), _token=Mock())
    user = discord.ClientUser(state=SimpleNamespace(http=http), data=user_data)
    fake_client = SimpleNamespace(user=user)

    async def noop_rate_limit(action_type):
        return None

    monkeypatch.setattr(profile, "client", fake_client)
    monkeypatch.setattr(profile, "apply_rate_limit", noop_rate_limit)

    result = await profile.edit_profile({"accent_color": color, "bio": "hello"})

    assert result[0].text == "Profile updated"
    http.edit_profile.assert_awaited_once_with({"bio": "hello", "accent_color": color})

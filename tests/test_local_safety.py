import pytest
from discord_py_self_mcp.local_safety import ALLOWED_TOOLS, check_arguments, check_request


@pytest.mark.parametrize('name', ['delete_channel', 'delete_role', 'delete_message', 'delete_invite', 'delete_server', 'run_discrawl', 'click_button', 'send_slash_command', 'submit_modal', 'future_unreviewed_tool'])
def test_dangerous_tools_are_not_allowed(name):
    assert name not in ALLOWED_TOOLS
    with pytest.raises(PermissionError):
        check_arguments(name, {})


@pytest.mark.parametrize('days', [1, -1, True, '0', None])
def test_bans_cannot_delete_message_history(days):
    with pytest.raises(PermissionError):
        check_arguments('ban_member', {'delete_message_days': days})


def test_ban_without_history_deletion_is_allowed():
    check_arguments('ban_member', {})
    check_arguments('ban_member', {'delete_message_days': 0})


@pytest.mark.parametrize(('method', 'path', 'payload'), [
    ('DELETE', '/channels/{channel_id}', {}),
    ('DELETE', '/guilds/{guild_id}', {}),
    ('POST', '/guilds/{guild_id}/delete', {}),
    ('POST', '/guilds/{guild_id}/bulk-ban', {'delete_message_seconds': 1}),
    ('POST', '/applications/{application_id}/delete', {}),
    ('DELETE', '/channels/{channel_id}/messages/{message_id}', {}),
    ('DELETE', '/guilds/{guild_id}/roles/{role_id}', {}),
    ('DELETE', '/invites/{invite_id}', {}),
    ('POST', '/channels/{channel_id}/messages/bulk-delete', {}),
    ('POST', '/guilds/{guild_id}/prune', {}),
    ('PATCH', '/guilds/{guild_id}', {'owner_id': '123'}),
    ('POST', '/users/@me/delete', {}),
    ('PATCH', '/users/@me', {'password': 'x'}),
    ('PUT', '/guilds/{guild_id}/bans/{user_id}', {'delete_message_seconds': 3600}),
    ('POST', '/interactions', {}),
])
def test_sensitive_requests_are_blocked(method, path, payload):
    with pytest.raises(PermissionError):
        check_request(method, path, payload)


@pytest.mark.parametrize(('method', 'path', 'payload'), [
    ('GET', '/guilds/{guild_id}/channels', {}),
    ('POST', '/guilds/{guild_id}/channels', {'name': 'test'}),
    ('POST', '/channels/{channel_id}/messages', {'content': 'hello'}),
    ('PUT', '/guilds/{guild_id}/bans/{user_id}', {'delete_message_seconds': 0}),
    ('DELETE', '/guilds/{guild_id}/bans/{user_id}', {}),
    ('DELETE', '/guilds/{guild_id}/members/{user_id}/roles/{role_id}', {}),
    ('DELETE', '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{reaction_type}/@me', {}),
])
def test_ordinary_management_requests_are_allowed(method, path, payload):
    check_request(method, path, payload)


@pytest.mark.asyncio
async def test_installed_library_deletion_is_blocked_before_transport(monkeypatch):
    import discord
    from discord.http import HTTPClient
    from discord_py_self_mcp.local_safety import install_http_guard
    forwarded = []

    async def transport(self, route, **kwargs):
        forwarded.append((route.method, route.path, kwargs))

    monkeypatch.setattr(HTTPClient, 'request', transport)
    client = discord.Client()
    install_http_guard(client.http)
    with pytest.raises(PermissionError):
        await client.http.delete_guild(123)
    with pytest.raises(PermissionError):
        await client.http.delete_channel(123)
    with pytest.raises(PermissionError):
        await client.http.ban(123, 456, delete_message_seconds=60)
    assert forwarded == []
    await client.http.ban(123, 456, delete_message_seconds=0)
    assert forwarded[0][2]['json']['delete_message_seconds'] == 0

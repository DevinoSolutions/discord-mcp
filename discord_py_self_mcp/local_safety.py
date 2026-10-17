"""Local installation policy. New upstream tools are denied until reviewed."""

ALLOWED_TOOLS = frozenset('''
list_guilds get_user_info list_channels list_roles get_role get_channel_permissions
inspect_effective_permissions get_server_overviews sample_server_content analyze_server_sidebar
read_messages search_messages get_message_attachments read_thread_messages list_active_threads
list_invites list_application_commands list_ephemeral_messages list_dm_channels list_unread_dms
list_pings list_friends list_server_folders create_channel create_thread archive_thread
kick_member ban_member unban_member create_role edit_role reorder_roles add_role remove_role
patch_channel_permissions set_channel_permissions set_category_permissions set_role_permissions
send_message edit_message send_thread_message add_reaction remove_reaction create_invite
mark_message_read join_voice_channel leave_voice_channel set_status set_activity edit_profile
send_friend_request add_friend create_server_folder move_server_to_folder rename_server_folder
apply_server_folder_layout reorder_server_folders create_guild edit_guild
get_guild_setup edit_guild_settings create_automod_rule edit_automod_rule edit_channel authorize_bot
'''.split())


def check_arguments(name: str, arguments: dict) -> None:
    if name not in ALLOWED_TOOLS:
        raise PermissionError('Tool blocked by the local no-sensitive-deletion policy.')
    if name == 'ban_member':
        days = arguments.get('delete_message_days', 0)
        if type(days) is not int or days != 0:
            raise PermissionError('Bans must preserve all message history (delete_message_days=0).')


def check_request(method: str, path: str, payload=None) -> None:
    """Defense in depth for native Discord HTTP calls, including hidden deletes."""
    payload = payload or {}
    if method == 'DELETE':
        allowed = {
            '/guilds/{guild_id}/bans/{user_id}',
            '/guilds/{guild_id}/members/{user_id}',  # Moderation: kick, not data deletion.
            '/guilds/{guild_id}/members/{user_id}/roles/{role_id}',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{member_id}',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{reaction_type}/@me',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{reaction_type}/{member_id}',
        }
        if path not in allowed:
            raise PermissionError('Deletion blocked by local policy.')
    if any(part in path for part in ('bulk-delete', '/prune', '/users/@me/delete', '/users/@me/disable')):
        raise PermissionError('Destructive operation blocked by local policy.')
    if method != 'GET' and (path.endswith('/delete') or path.endswith('/bulk-ban')):
        raise PermissionError('Destructive bulk/object operation blocked by local policy.')
    if path == '/interactions' and method != 'GET':
        raise PermissionError('Generic bot interactions are blocked by local policy.')
    if method == 'PATCH' and path == '/guilds/{guild_id}' and 'owner_id' in payload:
        raise PermissionError('Ownership transfer is blocked by local policy.')
    if method == 'PATCH' and path == '/users/@me':
        if set(payload) - {'bio', 'accent_color', 'accent_colour'}:
            raise PermissionError('Account security/identity changes are blocked by local policy.')
    if '/bans/' in path and method in {'PUT', 'POST'}:
        for key in ('delete_message_days', 'delete_message_seconds'):
            value = payload.get(key, 0)
            if type(value) is not int or value != 0:
                raise PermissionError('Deleting message history during a ban is blocked.')


def install_http_guard(http_client) -> None:
    original = http_client.request

    async def guarded_request(route, **kwargs):
        check_request(route.method, route.path, kwargs.get('json'))
        return await original(route, **kwargs)

    http_client.request = guarded_request

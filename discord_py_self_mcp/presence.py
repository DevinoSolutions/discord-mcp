"""Presence for headless gateway sessions that never have a human at the keyboard.

Discord shows the account with the "best" status across all connected sessions,
and online beats idle. A headless session that reports the account's saved
status (usually online) therefore keeps the account green 24/7, even after every
real client has gone idle or closed. These sessions report idle instead, which
is what the official client sends when you step away, and still follow dnd and
invisible so they never override those.
"""

import discord

_FOLLOWED = {discord.Status.dnd, discord.Status.invisible}


def headless_status(settings_status):
    """Status a headless session should report for the account's saved status."""
    if settings_status in _FOLLOWED:
        return settings_status
    return discord.Status.idle


async def apply_headless_presence(client, settings_status):
    """Send this session's presence without touching the account's saved settings."""
    ws = client.ws
    if ws is None:
        return
    status = headless_status(settings_status).value
    if ws.status == status and ws.afk:
        return
    await ws.change_presence(
        status=status,
        activities=list(ws.activities),
        afk=True,
        since=ws.idle_since,
    )

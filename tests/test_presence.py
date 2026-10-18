import asyncio

import discord

from discord_py_self_mcp.bot import client
from discord_py_self_mcp.presence import apply_headless_presence, headless_status


class FakeWs:
    def __init__(self, status="unknown", afk=True):
        self.status = status
        self.afk = afk
        self.activities = [{"type": 4, "name": "Custom Status", "state": "hi"}]
        self.idle_since = 1700000000000
        self.sent = []

    async def change_presence(self, *, status, activities, afk, since):
        self.sent.append(
            {"status": status, "activities": activities, "afk": afk, "since": since}
        )
        self.status = status
        self.afk = afk


class FakeClient:
    def __init__(self, ws):
        self.ws = ws


def test_headless_session_never_reports_online():
    for saved in (discord.Status.online, discord.Status.idle, discord.Status.unknown, None):
        assert headless_status(saved) is discord.Status.idle


def test_headless_session_follows_dnd_and_invisible():
    assert headless_status(discord.Status.dnd) is discord.Status.dnd
    assert headless_status(discord.Status.invisible) is discord.Status.invisible


def test_apply_sends_idle_and_keeps_activities_and_afk():
    ws = FakeWs()
    asyncio.run(apply_headless_presence(FakeClient(ws), discord.Status.online))
    assert ws.sent == [
        {
            "status": "idle",
            "activities": [{"type": 4, "name": "Custom Status", "state": "hi"}],
            "afk": True,
            "since": 1700000000000,
        }
    ]


def test_apply_skips_when_already_set_or_disconnected():
    ws = FakeWs(status="idle")
    asyncio.run(apply_headless_presence(FakeClient(ws), discord.Status.online))
    assert ws.sent == []
    asyncio.run(apply_headless_presence(FakeClient(None), discord.Status.online))


def test_selfbot_does_not_mirror_saved_status_onto_its_session():
    assert client._sync_presences is False

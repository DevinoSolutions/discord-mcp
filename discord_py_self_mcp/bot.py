import discord
import inspect
import importlib
import sys
from datetime import datetime, timezone
from typing import Dict, Any
from google.protobuf import json_format
from dotenv import load_dotenv
from discord_py_self_mcp.local_safety import install_http_guard
from discord_py_self_mcp.presence import apply_headless_presence
from discord_py_self_mcp.rate_limiter import (
    RateLimiter,
)
from discord_py_self_mcp.logging_utils import log_to_stderr
from discord_py_self_mcp import modal_store

load_dotenv()

if (
    "including_default_value_fields"
    not in inspect.signature(json_format.MessageToDict).parameters
):

    def _message_to_dict_compat(message, **kwargs):
        if "including_default_value_fields" in kwargs:
            kwargs["always_print_fields_with_no_presence"] = kwargs.pop(
                "including_default_value_fields"
            )
        return json_format.MessageToDict(message, **kwargs)

    discord_settings = importlib.import_module("discord.settings")
    setattr(discord_settings, "MessageToDict", _message_to_dict_compat)


rate_limiter = None


def init_rate_limiter():
    global rate_limiter
    rate_limiter = RateLimiter()
    if rate_limiter.is_enabled():
        log_to_stderr(
            "[RATE_LIMIT] Enabled with config: "
            f"{rate_limiter.config.messages_per_minute} msg/min, "
            f"{rate_limiter.config.actions_per_minute} actions/min"
        )
    return rate_limiter


captcha_solver = None


class SelfBot(discord.Client):
    def __init__(self):
        init_rate_limiter()
        # This session never has a human at the keyboard. Without afk, Discord
        # treats the account as active on desktop and withholds mobile pushes.
        # sync_presence=False stops the library from mirroring the saved status
        # (usually online) onto this session; presence.py reports idle instead.
        super().__init__(
            afk=True,
            idle_since=datetime.now(timezone.utc),
            sync_presence=False,
        )
        install_http_guard(self.http)

    async def on_internal_settings_update(self, old_settings, new_settings, /):
        await apply_headless_presence(self, new_settings.status)

    async def on_ready(self):
        await apply_headless_presence(self, getattr(self.settings, "status", None))
        user_id = self.user.id if self.user else "unknown"
        log_to_stderr(f"[READY] Logged in as {self.user} (ID: {user_id})")
        log_to_stderr(f"[READY] Guilds: {len(self.guilds)}")
        log_to_stderr(f"[READY] Private channels: {len(self.private_channels)}")

        if rate_limiter and rate_limiter.is_enabled():
            log_to_stderr(f"[RATE_LIMIT] Active - {rate_limiter.get_stats()}")

    async def on_connect(self):
        log_to_stderr("[CONNECT] Connected to Discord gateway")

    async def on_disconnect(self):
        log_to_stderr("[DISCONNECT] Disconnected from Discord gateway")

    async def on_error(self, event, *args, **kwargs):
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type and exc_value:
            log_to_stderr(
                f"[ERROR] Event: {event}, Exception: {exc_type.__name__}: {exc_value}"
            )
            return
        log_to_stderr(f"[ERROR] Event: {event}")

    async def on_resumed(self):
        log_to_stderr("[RESUMED] Session resumed")

    async def on_modal(self, modal):
        modal_store.put(modal)
        log_to_stderr(f"[MODAL] Received {modal.custom_id!r} ({modal.title!r})")

    async def on_captcha(self, data: Dict[str, Any]) -> str:
        return await solve_captcha()


async def solve_captcha() -> str:
    raise RuntimeError('Discord requires human verification. Complete it in the browser.')


client = SelfBot()

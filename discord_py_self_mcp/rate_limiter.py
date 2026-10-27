import os
import time
import asyncio
from typing import Dict, Optional, Any
from dataclasses import dataclass

from discord_py_self_mcp.logging_utils import log_to_stderr


@dataclass
class RateLimitConfig:
    enabled: bool = True
    messages_per_minute: int = 10
    messages_per_second: int = 1
    actions_per_minute: int = 5
    reads_per_minute: int = 30
    cooldown_on_limit: int = 60


class RateLimiter:
    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or self._load_from_env()

        self._message_timestamps: list = []
        self._action_timestamps: list = []
        self._read_timestamps: list = []
        # Reads have their own lock: the write lock is held while a write sleeps
        # out its cooldown, and reads must not queue behind that.
        self._read_lock = asyncio.Lock()
        self._cooldown_until: float = 0
        self._lock = asyncio.Lock()

        self._last_action_time: float = 0
        self._min_action_interval: float = 1.0

    @classmethod
    def _load_from_env(cls) -> RateLimitConfig:
        return RateLimitConfig(
            enabled=os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true",
            messages_per_minute=int(os.getenv("RATE_LIMIT_MESSAGES_PER_MINUTE", "10")),
            messages_per_second=int(os.getenv("RATE_LIMIT_MESSAGES_PER_SECOND", "1")),
            actions_per_minute=int(os.getenv("RATE_LIMIT_ACTIONS_PER_MINUTE", "5")),
            reads_per_minute=int(os.getenv("RATE_LIMIT_READS_PER_MINUTE", "30")),
            cooldown_on_limit=int(os.getenv("RATE_LIMIT_COOLDOWN", "60")),
        )

    def is_enabled(self) -> bool:
        return self.config.enabled

    def get_cooldown_remaining(self) -> int:
        remaining = self._cooldown_until - time.time()
        return max(0, int(remaining))

    async def _wait_for_read(self) -> None:
        """Pace reads within reads_per_minute without the fleet-wide cooldown.

        Every Claude Code session on the machine shares this one account. Reads
        used to spend the 5/min write budget, and one burst then froze every
        session behind a 60s cooldown, long enough for clients to give up on
        the server. A full read window now just waits for its oldest read to
        age out, and a write cooldown never blocks a read.
        """
        async with self._read_lock:
            while True:
                self._clean_timestamps(self._read_timestamps, 60)
                if len(self._read_timestamps) < self.config.reads_per_minute:
                    self._read_timestamps.append(time.time())
                    return
                await asyncio.sleep(
                    max(0.05, 60 - (time.time() - self._read_timestamps[0]))
                )

    async def wait_if_needed(self, action_type: str = "message"):
        if not self.is_enabled():
            return

        if action_type == "read":
            await self._wait_for_read()
            return

        async with self._lock:
            while True:
                now = time.time()

                if now < self._cooldown_until:
                    await asyncio.sleep(self._cooldown_until - now)
                    continue

                if action_type == "message":
                    self._clean_timestamps(self._message_timestamps, 60)

                    msg_in_minute = len(self._message_timestamps)
                    msg_in_second = sum(
                        1 for timestamp in self._message_timestamps if now - timestamp < 1
                    )

                    if msg_in_minute >= self.config.messages_per_minute:
                        self._trigger_cooldown(
                            f"Message rate limit reached ({self.config.messages_per_minute}/min)"
                        )
                        continue

                    if msg_in_second >= self.config.messages_per_second:
                        sleep_time = max(
                            0.0,
                            1.0 - (now - self._message_timestamps[-1]),
                        )
                        if sleep_time:
                            await asyncio.sleep(sleep_time)
                        continue

                    self._message_timestamps.append(time.time())
                    return

                if action_type == "action":
                    self._clean_timestamps(self._action_timestamps, 60)
                    action_in_minute = len(self._action_timestamps)

                    if action_in_minute >= self.config.actions_per_minute:
                        self._trigger_cooldown(
                            f"Action rate limit reached ({self.config.actions_per_minute}/min)"
                        )
                        continue

                    time_since_last = now - self._last_action_time
                    if time_since_last < self._min_action_interval:
                        await asyncio.sleep(self._min_action_interval - time_since_last)
                        continue

                    now = time.time()
                    self._action_timestamps.append(now)
                    self._last_action_time = now
                    return

                return

    def _clean_timestamps(self, timestamps: list, window: int):
        now = time.time()
        timestamps[:] = [t for t in timestamps if now - t < window]

    def _trigger_cooldown(self, reason: str):
        self._cooldown_until = time.time() + self.config.cooldown_on_limit
        log_to_stderr(
            f"[RATE_LIMIT] Cooldown triggered: {reason}. Cooldown for {self.config.cooldown_on_limit}s"
        )

    def reset(self):
        self._message_timestamps.clear()
        self._action_timestamps.clear()
        self._read_timestamps.clear()
        self._cooldown_until = 0

    def get_stats(self) -> Dict[str, Any]:
        self._clean_timestamps(self._message_timestamps, 60)
        self._clean_timestamps(self._action_timestamps, 60)
        self._clean_timestamps(self._read_timestamps, 60)
        return {
            "enabled": self.is_enabled(),
            "cooldown_remaining": self.get_cooldown_remaining(),
            "messages_last_minute": len(self._message_timestamps),
            "actions_last_minute": len(self._action_timestamps),
            "reads_last_minute": len(self._read_timestamps),
            "config": {
                "messages_per_minute": self.config.messages_per_minute,
                "messages_per_second": self.config.messages_per_second,
                "actions_per_minute": self.config.actions_per_minute,
                "reads_per_minute": self.config.reads_per_minute,
            },
        }


_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(config: Optional[RateLimitConfig] = None) -> RateLimiter:
    global _global_rate_limiter
    if _global_rate_limiter is None:
        _global_rate_limiter = RateLimiter(config)
    return _global_rate_limiter

import sys
from datetime import datetime, timezone


def log_to_stderr(message: str) -> None:
    # server.log had no timestamps, so disconnects could not be dated.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{stamp} {message}\n")
    sys.stderr.flush()


def mask_secret(secret: str | None) -> str:
    if not secret:
        return "<missing>"
    return "<configured>"

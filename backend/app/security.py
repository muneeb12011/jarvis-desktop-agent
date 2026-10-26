"""
Security for something that can click, type and run commands on your machine.

1. Shared-secret auth on the WebSocket and the HTTP voice endpoints. Without
   it, any local process - or a web page you happen to have open - could
   connect to 127.0.0.1:8000 and drive your mouse and keyboard.
2. A token-bucket rate limiter per connection so a bug or a message flood
   can't spin the agent into a tight loop of real clicks.
"""

from __future__ import annotations

import secrets
import time

from . import config

AUTH_TOKEN = config.AUTH_TOKEN


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def is_token_valid(presented: str | None) -> bool:
    """Constant-time comparison. Empty configured token = auth disabled
    (warned loudly at startup)."""
    if not AUTH_TOKEN:
        return True
    if not presented:
        return False
    return secrets.compare_digest(presented, AUTH_TOKEN)


class RateLimiter:
    """`capacity` messages allowed per `refill_seconds`."""

    def __init__(self, capacity: int = 10, refill_seconds: float = 5.0):
        self.capacity = capacity
        self.refill_seconds = refill_seconds
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / self.refill_seconds))
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

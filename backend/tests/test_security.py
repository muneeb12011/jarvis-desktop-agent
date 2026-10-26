"""
Tests for security.py — the auth token check and rate limiter, since a
bug in either one directly undermines the security model.
"""

import time

from app import security


def test_token_valid_when_matching(monkeypatch):
    monkeypatch.setattr(security, "AUTH_TOKEN", "secret123")
    assert security.is_token_valid("secret123") is True


def test_token_invalid_when_mismatched(monkeypatch):
    monkeypatch.setattr(security, "AUTH_TOKEN", "secret123")
    assert security.is_token_valid("wrong") is False


def test_token_invalid_when_missing(monkeypatch):
    monkeypatch.setattr(security, "AUTH_TOKEN", "secret123")
    assert security.is_token_valid(None) is False
    assert security.is_token_valid("") is False


def test_auth_disabled_when_no_token_configured(monkeypatch):
    # Explicit opt-out state: empty configured token means auth is off.
    monkeypatch.setattr(security, "AUTH_TOKEN", "")
    assert security.is_token_valid(None) is True
    assert security.is_token_valid("anything") is True


def test_rate_limiter_allows_up_to_capacity():
    limiter = security.RateLimiter(capacity=3, refill_seconds=10)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False  # 4th immediate call should be blocked


def test_rate_limiter_refills_over_time():
    limiter = security.RateLimiter(capacity=1, refill_seconds=0.1)
    assert limiter.allow() is True
    assert limiter.allow() is False
    time.sleep(0.15)
    assert limiter.allow() is True

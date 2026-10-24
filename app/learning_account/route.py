"""The learning account's route: whether its settings let it run, and the names of its states.

The route a status record or the boot snapshot shows is one of these values. The settings part
(``settings_route``) reads the settings alone, never the network or the database, so the boot
snapshot and the scorecard can call it; the account itself is checked on the learning thread
(``account.LearningAccount``): ``pending_verification`` until then.
"""

from __future__ import annotations

from typing import Any

PENDING = "pending_verification"  # configured; the account itself not checked yet
RUNNING = "running"
SETTING_OFF = "disabled:setting_off"
NOT_CONFIGURED = "not_configured"
NOT_PAPER = "disabled:not_paper"
MAIN_ACCOUNT_UNKNOWN = "disabled:main_account_unknown"
LEARNING_ACCOUNT_UNKNOWN = "disabled:learning_account_unknown"
SAME_ACCOUNT = "disabled:same_account"
SAME_KEYS = "disabled:same_keys"
ATTACH_FAILED = "disabled:attach_failed"
# What the account itself answered (the learning thread): a real, different account number;
# an empty or unreadable identity (retried after VERIFY_RETRY_SECONDS); a blocked account.
ACCOUNT_MISMATCH = "account_mismatch"
VERIFICATION_FAILED = "verification_failed"
TRADING_BLOCKED = "trading_blocked"
VERIFY_RETRY_SECONDS = 120.0  # an empty or unreadable identity
REFUSED_RETRY_SECONDS = 900.0  # every other refusal the account can answer


def _text(settings: Any, name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def settings_route(settings: Any) -> str:
    """``PENDING`` when the settings let the learning account run, else why not (the first
    reason in this order). Paper only, on another account than the main one, with other keys."""

    if not bool(getattr(settings, "learning_account_enabled", False)):
        return SETTING_OFF
    key, secret = _text(settings, "alpaca_learning_api_key"), _text(settings, "alpaca_learning_secret_key")
    if not key or not secret:
        return NOT_CONFIGURED
    if str(getattr(settings, "execution_mode", "")) != "paper" or bool(getattr(settings, "enable_real_trading", True)):
        return NOT_PAPER
    main = _text(settings, "alpaca_expected_account_number")
    if not main:
        return MAIN_ACCOUNT_UNKNOWN
    learning = _text(settings, "alpaca_learning_expected_account_number")
    if not learning:
        return LEARNING_ACCOUNT_UNKNOWN
    if learning.upper() == main.upper():
        return SAME_ACCOUNT
    if key == _text(settings, "alpaca_api_key") or secret == _text(settings, "alpaca_secret_key"):
        return SAME_KEYS
    return PENDING


def identity_route(settings: Any, identity: Any) -> str:
    """What one identity read of the learning account says: ``RUNNING`` for the expected,
    active account; ``ACCOUNT_MISMATCH`` only for a real, different number; an empty or
    unreadable one is ``VERIFICATION_FAILED``."""

    if not isinstance(identity, dict):
        return VERIFICATION_FAILED
    number = str(identity.get("account_number") or "").strip()
    if not number:
        return VERIFICATION_FAILED
    if number.upper() != _text(settings, "alpaca_learning_expected_account_number").upper():
        return ACCOUNT_MISMATCH
    if bool(identity.get("trading_blocked")) or str(identity.get("status") or "").upper() not in {"", "ACTIVE"}:
        return TRADING_BLOCKED
    return RUNNING


def retry_seconds(route: str) -> float:
    return VERIFY_RETRY_SECONDS if route == VERIFICATION_FAILED else REFUSED_RETRY_SECONDS

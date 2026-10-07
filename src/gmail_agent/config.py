"""Where gmail-agent keeps its files, and which OAuth scopes each kind of call needs."""

from __future__ import annotations

import os
from pathlib import Path

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
FULL_SCOPE = "https://mail.google.com/"

# What `gmail-agent login --scope LEVEL` asks Google for.
LEVELS: dict[str, list[str]] = {
    "readonly": [READONLY_SCOPE],
    "compose": [READONLY_SCOPE, COMPOSE_SCOPE],
    "modify": [MODIFY_SCOPE],
    "full": [FULL_SCOPE],
}
DEFAULT_LEVEL = "modify"

# Each kind of call: the scopes Google accepts for it, and the smallest level that has one.
ACCESS: dict[str, tuple[frozenset[str], str]] = {
    "read": (frozenset({READONLY_SCOPE, MODIFY_SCOPE, FULL_SCOPE}), "readonly"),
    "compose": (frozenset({COMPOSE_SCOPE, MODIFY_SCOPE, FULL_SCOPE}), "compose"),
    "modify": (frozenset({MODIFY_SCOPE, FULL_SCOPE}), "modify"),
    "delete": (frozenset({FULL_SCOPE}), "full"),
}

CONFIG_DIR_ENV = "GMAIL_AGENT_CONFIG_DIR"


def short_scope(scope: str) -> str:
    return "mail.google.com" if scope == FULL_SCOPE else scope.rsplit("/", 1)[-1]


def level_of(scopes: list[str] | None) -> str:
    """The broadest level these scopes amount to, or "none"."""
    granted = set(scopes or [])
    for level in ("full", "modify", "compose", "readonly"):
        if set(LEVELS[level]) <= granted:
            return level
    return "none"


def allows(scopes: list[str] | None, access: str) -> bool:
    return bool(ACCESS[access][0] & set(scopes or []))


def config_dir() -> Path:
    """$GMAIL_AGENT_CONFIG_DIR, else $XDG_CONFIG_HOME/gmail-agent, else ~/.config/gmail-agent."""
    if override := os.environ.get(CONFIG_DIR_ENV):
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / "gmail-agent"


def credentials_path() -> Path:
    """The OAuth client file downloaded from Google Cloud (Desktop app)."""
    return config_dir() / "credentials.json"


def token_path() -> Path:
    """The user's access and refresh token, written by `gmail-agent login`."""
    return config_dir() / "token.json"

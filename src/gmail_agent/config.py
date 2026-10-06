"""Where gmail-agent keeps its files, and which OAuth scopes it asks for."""

from __future__ import annotations

import os
from pathlib import Path

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"

CONFIG_DIR_ENV = "GMAIL_AGENT_CONFIG_DIR"


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


def scopes(allow_drafts: bool = False) -> list[str]:
    return [READONLY_SCOPE, COMPOSE_SCOPE] if allow_drafts else [READONLY_SCOPE]

"""OAuth: the one-time browser login, and loading/refreshing the saved token."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from . import config
from .errors import SetupError

SETUP_HINT = "See https://github.com/dremok/gmail-agent#google-cloud-setup"


def _missing_client_error() -> SetupError:
    return SetupError(
        f"No OAuth client file at {config.credentials_path()}. Create a Desktop OAuth client in "
        f"Google Cloud, download its JSON to that path, then run: gmail-agent login. {SETUP_HINT}"
    )


def login(allow_drafts: bool = False, open_browser: bool = True) -> Credentials:
    """Run the browser consent flow and save the token. Needs credentials.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client = config.credentials_path()
    if not client.exists():
        raise _missing_client_error()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client), config.scopes(allow_drafts))
    except (ValueError, json.JSONDecodeError) as e:
        raise SetupError(
            f"{client} is not a valid OAuth client file ({e}). Download the JSON for a client "
            f"of type 'Desktop app'. {SETUP_HINT}"
        ) from e
    # The flow prints the login URL; keep it on stderr so `--json` output stays clean.
    with contextlib.redirect_stdout(sys.stderr):
        creds = flow.run_local_server(
            port=0,
            open_browser=open_browser,
            authorization_prompt_message="Open this URL in a browser to log in:\n{url}",
            success_message="gmail-agent is logged in. You can close this tab.",
        )
    save_token(creds)
    return creds


def save_token(creds: Credentials) -> Path:
    """Write the token with owner-only permissions (0600), replacing any old one atomically."""
    path = config.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def logout() -> bool:
    """Delete the saved token. Returns False if there was none."""
    path = config.token_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def load_credentials(require_drafts: bool = False) -> Credentials:
    """Load the saved token, refreshing it if needed. Never opens a browser."""
    path = config.token_path()
    if not path.exists():
        if not config.credentials_path().exists():
            raise _missing_client_error()
        raise SetupError(f"Not logged in (no token at {path}). Run: gmail-agent login")
    try:
        creds = Credentials.from_authorized_user_file(str(path))
    except (ValueError, json.JSONDecodeError) as e:
        raise SetupError(f"Token file {path} is unreadable ({e}). Run: gmail-agent login") from e

    if require_drafts and config.COMPOSE_SCOPE not in (creds.scopes or []):
        raise SetupError(
            "Creating drafts needs the gmail.compose scope, which this login did not grant. "
            "Run: gmail-agent login --allow-drafts"
        )

    if not creds.valid:
        if not creds.refresh_token:
            raise SetupError(f"Token at {path} has no refresh token. Run: gmail-agent login")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise SetupError(
                f"Google rejected the saved token ({e}). Run: gmail-agent login. If this happens "
                "every 7 days, your OAuth app is in Testing mode; set its publishing status to "
                f"'In production'. {SETUP_HINT}"
            ) from e
        except TransportError as e:
            raise SetupError(f"Could not reach Google to refresh the token: {e}") from e
        save_token(creds)
    return creds


def build_service(creds: Credentials):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)

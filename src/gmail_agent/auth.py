"""OAuth: the one-time browser login, and loading/refreshing the saved token."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from . import config
from .errors import GmailAgentError, SetupError

SETUP_HINT = "See https://github.com/dremok/gmail-agent#google-cloud-setup"


def _missing_client_error() -> SetupError:
    return SetupError(
        f"No OAuth client file at {config.credentials_path()}. Create a Desktop OAuth client in "
        f"Google Cloud, download its JSON to that path, then run: gmail-agent login. {SETUP_HINT}"
    )


def token_rejected(e: RefreshError) -> SetupError:
    return SetupError(
        f"Google rejected the saved token ({e}). Run: gmail-agent login. If this happens "
        "every 7 days, your OAuth app is in Testing mode; set its publishing status to "
        f"'In production'. {SETUP_HINT}"
    )


def granted_scopes(creds: Credentials) -> list[str]:
    """What the user actually ticked on the consent screen (they can untick scopes)."""
    granted = creds.granted_scopes or creds.scopes or []
    return granted.split() if isinstance(granted, str) else list(granted)


def login(level: str = config.DEFAULT_LEVEL, open_browser: bool = True) -> Credentials:
    """Run the browser consent flow and save the token. Needs credentials.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if level not in config.LEVELS:
        raise GmailAgentError(
            f"Unknown scope level {level!r}. Use one of: {', '.join(config.LEVELS)}"
        )
    client = config.credentials_path()
    if not client.exists():
        raise _missing_client_error()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client), config.LEVELS[level])
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
    save_token(creds, scopes=granted_scopes(creds))
    return creds


def save_token(creds: Credentials, scopes: list[str] | None = None) -> Path:
    """Write the token with owner-only permissions (0600), replacing any old one atomically.

    Each save writes its own temporary file, so two processes refreshing at the same time
    (the CLI and an MCP server, say) cannot leave a half-written token behind.
    """
    path = config.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    data = json.loads(creds.to_json())
    if scopes is not None:
        data["scopes"] = scopes
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def logout() -> bool:
    """Delete the saved token. Returns False if there was none."""
    path = config.token_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def saved_scopes() -> list[str] | None:
    """The scopes recorded in the saved token, without touching the network. None if there is
    no readable token."""
    try:
        scopes = json.loads(config.token_path().read_text()).get("scopes")
    except (OSError, ValueError):
        return None
    return scopes.split() if isinstance(scopes, str) else scopes


def load_credentials() -> Credentials:
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

    if not creds.valid:
        if not creds.refresh_token:
            raise SetupError(f"Token at {path} has no refresh token. Run: gmail-agent login")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise token_rejected(e) from e
        except TransportError as e:
            raise SetupError(f"Could not reach Google to refresh the token: {e}") from e
        save_token(creds)
    return creds


def build_service(creds: Credentials, transport: Callable[[], Any] | None = None):
    """The Gmail API service, safe to use from several threads at once.

    httplib2 is not thread-safe, and the MCP server runs tool calls in worker threads, so
    sharing one connection crashes the process when calls overlap. Every thread gets its own
    authorized connection instead (google-api-python-client's documented requestBuilder
    pattern). `transport` makes the plain httplib2.Http for each thread; tests replace it.
    """
    import google_auth_httplib2
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpRequest, build_http

    new_transport = transport or build_http
    local = threading.local()

    def thread_http() -> google_auth_httplib2.AuthorizedHttp:
        if not hasattr(local, "http"):
            local.http = google_auth_httplib2.AuthorizedHttp(creds, http=new_transport())
        return local.http

    def request_builder(_http, *args, **kwargs) -> HttpRequest:
        return HttpRequest(thread_http(), *args, **kwargs)

    return build(
        "gmail", "v1", http=thread_http(), requestBuilder=request_builder, cache_discovery=False
    )

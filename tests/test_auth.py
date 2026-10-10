import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import ClassVar

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from gmail_agent import auth, config
from gmail_agent.errors import GmailAgentError, SetupError
from gmail_agent.gmail import Gmail

from .conftest import FakeGmailHttp


def write_token(scopes, expired=False):
    data = {
        "token": "access",
        "refresh_token": "refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "not-a-secret",
        "scopes": scopes,
        "expiry": "2000-01-01T00:00:00Z" if expired else "2999-01-01T00:00:00Z",
    }
    path = config.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_config_dir_env_override(isolated_config):
    assert config.config_dir() == isolated_config
    assert config.token_path() == isolated_config / "token.json"


def test_config_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("GMAIL_AGENT_CONFIG_DIR")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.config_dir() == tmp_path / ".config" / "gmail-agent"


def test_no_client_file_explains_setup():
    with pytest.raises(SetupError, match=r"No OAuth client file at .*credentials.json"):
        auth.load_credentials()


def test_login_without_client_file():
    with pytest.raises(SetupError, match="Desktop OAuth client"):
        auth.login()


def test_bad_client_file(isolated_config):
    isolated_config.mkdir()
    config.credentials_path().write_text("{}")
    with pytest.raises(SetupError, match="not a valid OAuth client file"):
        auth.login()


def test_client_file_but_not_logged_in(isolated_config):
    isolated_config.mkdir()
    config.credentials_path().write_text("{}")
    with pytest.raises(SetupError, match=r"Run: gmail-agent login$"):
        auth.load_credentials()


def test_valid_token_loads():
    write_token([config.READONLY_SCOPE])
    creds = auth.load_credentials()
    assert creds.token == "access"


@pytest.mark.parametrize(
    ("level", "allowed"),
    [
        ("readonly", {"read"}),
        ("compose", {"read", "compose"}),
        ("modify", {"read", "compose", "modify"}),
        ("full", {"read", "compose", "modify", "delete"}),
    ],
)
def test_levels_grant_what_they_say(level, allowed):
    scopes = config.LEVELS[level]
    assert config.level_of(scopes) == level
    assert {a for a in config.ACCESS if config.allows(scopes, a)} == allowed


def test_default_level_is_modify():
    assert config.DEFAULT_LEVEL == "modify"


class FakeFlow:
    """Stands in for InstalledAppFlow; records the requested scopes."""

    requested: ClassVar[list[str]] = []
    grant: ClassVar[list[str] | None] = None

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        cls.requested = scopes
        return cls()

    def run_local_server(self, **kwargs):
        granted = self.grant if self.grant is not None else self.requested
        return Credentials(
            token="t",
            refresh_token="r",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="id",
            client_secret="s",
            scopes=self.requested,
            granted_scopes=granted,
        )


@pytest.fixture
def fake_flow(monkeypatch, isolated_config):
    import google_auth_oauthlib.flow

    isolated_config.mkdir()
    config.credentials_path().write_text("{}")
    FakeFlow.grant = None
    monkeypatch.setattr(google_auth_oauthlib.flow, "InstalledAppFlow", FakeFlow)
    return FakeFlow


def test_login_requests_level_and_saves_granted_scopes(fake_flow):
    auth.login("full")
    assert fake_flow.requested == [config.FULL_SCOPE]
    assert json.loads(config.token_path().read_text())["scopes"] == [config.FULL_SCOPE]


def test_login_defaults_to_modify(fake_flow):
    auth.login()
    assert fake_flow.requested == [config.MODIFY_SCOPE]


def test_login_records_partial_consent(fake_flow):
    fake_flow.grant = [config.READONLY_SCOPE]  # user unticked the compose box
    auth.login("compose")
    assert json.loads(config.token_path().read_text())["scopes"] == [config.READONLY_SCOPE]


def test_login_unknown_level(fake_flow):
    with pytest.raises(GmailAgentError, match="Unknown scope level"):
        auth.login("everything")


def test_expired_token_that_google_rejects(monkeypatch):
    write_token([config.READONLY_SCOPE], expired=True)

    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refuse)
    with pytest.raises(SetupError, match=r"every 7 days.*In production"):
        auth.load_credentials()


def test_expired_token_refreshes_and_is_saved(monkeypatch):
    write_token([config.READONLY_SCOPE], expired=True)

    def refresh(self, request):
        self.token = "fresh"
        self.expiry = None

    monkeypatch.setattr(Credentials, "refresh", refresh)
    assert auth.load_credentials().token == "fresh"
    assert json.loads(config.token_path().read_text())["token"] == "fresh"


def test_saved_token_is_private():
    creds = Credentials(token="t", refresh_token="r", scopes=[config.READONLY_SCOPE])
    path = auth.save_token(creds)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_simultaneous_saves_leave_a_whole_token():
    def save(i):
        auth.save_token(Credentials(token=f"t{i}", refresh_token="r", scopes=[config.MODIFY_SCOPE]))

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(save, range(40)))  # re-raises any error from a save
    assert json.loads(config.token_path().read_text())["token"].startswith("t")
    assert [p.name for p in config.config_dir().iterdir()] == ["token.json"]


def test_logout():
    assert auth.logout() is False
    write_token([config.READONLY_SCOPE])
    assert auth.logout() is True
    assert not config.token_path().exists()


def test_built_service_reads_and_uploads_through_authorized_connections(mailbox):
    seen_auth = []

    class Transport(FakeGmailHttp):
        def request(self, uri, method="GET", body=None, headers=None, **kwargs):
            seen_auth.append((headers or {}).get("authorization"))
            return super().request(uri, method, body, headers, **kwargs)

    service = auth.build_service(Credentials(token="t"), transport=lambda: Transport(mailbox))
    gmail = Gmail(service)
    assert gmail.get_message("m1")["subject"] == "Invoice March"
    assert gmail.send(["bob@example.com"], "Hi", "Hello")["sent"] is True
    assert mailbox.sent[0]["mail"]["Subject"] == "Hi"
    assert set(seen_auth) == {"Bearer t"}


def test_token_refused_mid_session_says_to_log_in(mailbox, monkeypatch):
    # A long-running MCP server refreshes the access token inside API calls, not at startup.
    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refuse)
    expired = Credentials(
        token="old",
        refresh_token="r",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="s",
        expiry=datetime(2000, 1, 1),
    )
    gmail = Gmail(auth.build_service(expired, transport=lambda: FakeGmailHttp(mailbox)))
    with pytest.raises(SetupError, match=r"Run: gmail-agent login.*In production"):
        gmail.get_message("m1")


def test_network_failure_is_a_readable_error(monkeypatch):
    class Offline:
        def request(self, *args, **kwargs):
            raise httplib2.ServerNotFoundError("Unable to find the server at gmail.googleapis.com")

    monkeypatch.setattr(time, "sleep", lambda seconds: None)  # skip the client's retry backoff
    gmail = Gmail(auth.build_service(Credentials(token="t"), transport=Offline))
    with pytest.raises(GmailAgentError, match="Network error during message m1: Unable to find"):
        gmail.get_message("m1")

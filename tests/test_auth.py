import json
import stat

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from gmail_agent import auth, config
from gmail_agent.errors import SetupError


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


def test_drafts_need_compose_scope():
    write_token([config.READONLY_SCOPE])
    with pytest.raises(SetupError, match="login --allow-drafts"):
        auth.load_credentials(require_drafts=True)
    write_token(config.scopes(allow_drafts=True))
    assert auth.load_credentials(require_drafts=True)


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


def test_logout():
    assert auth.logout() is False
    write_token([config.READONLY_SCOPE])
    assert auth.logout() is True
    assert not config.token_path().exists()

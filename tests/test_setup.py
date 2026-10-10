"""`gmail-agent setup`, with gcloud, the browser, stdin and the login all faked.

Nothing here runs gcloud or opens a browser: subprocess.run fails the test unless a test
installs a fake, and the config and home folders are temporary (see conftest).
"""

import io
import json
import stat
import subprocess
import sys
import webbrowser
from datetime import datetime

import pytest
from google.oauth2.credentials import Credentials

from gmail_agent import auth, cli, config, setup
from gmail_agent.errors import SetupError

PROJECT = "my-project-1"


def client_json(project=PROJECT, kind="installed", **overrides):
    client = {
        "client_id": "123-abc.apps.googleusercontent.com",
        "project_id": project,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "GOCSPX-test",
        "redirect_uris": ["http://localhost"],
        **overrides,
    }
    return {kind: client}


@pytest.fixture(autouse=True)
def no_real_gcloud(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"test tried to run a real command: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **kw: urls.append(url))
    return urls


@pytest.fixture
def answers(monkeypatch):
    def give(*lines):
        monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{line}\n" for line in lines)))

    return give


@pytest.fixture
def downloads(isolated_home, monkeypatch):
    """The first wait for the client file finds `files` appearing in ~/Downloads."""
    files = {}

    def sleep(seconds):
        for name, data in files.items():
            (isolated_home / "Downloads" / name).write_text(json.dumps(data))

    monkeypatch.setattr(setup.time, "sleep", sleep)
    return files


@pytest.fixture
def login(monkeypatch, service):
    """auth.login without a browser; the account check runs against the fake Gmail."""
    calls = []

    def fake_login(level, open_browser=True):
        calls.append(level)
        creds = Credentials(token="t", refresh_token="r", scopes=config.LEVELS[level])
        auth.save_token(creds, scopes=config.LEVELS[level])
        return creds

    monkeypatch.setattr(auth, "login", fake_login)
    monkeypatch.setattr(auth, "build_service", lambda creds: service)
    return calls


def logged_in(level):
    creds = Credentials(
        token="t",
        refresh_token="r",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="123-abc.apps.googleusercontent.com",
        client_secret="GOCSPX-test",
        expiry=datetime(2999, 1, 1),
    )
    auth.save_token(creds, scopes=config.LEVELS[level])


def installed_client():
    path = config.credentials_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return json.loads(path.read_text())


# Checking client files ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (client_json(kind="web"), "Web application"),
        ({"other": {}}, "not an OAuth client file"),
        ([1, 2], "not an OAuth client file"),
        (client_json(client_id="nope"), "client_id"),
        (client_json(client_secret=""), "Google shows the secret only"),
        (client_json(project="someone-else"), "--project someone-else"),
    ],
)
def test_bad_client_files_are_refused(data, message):
    with pytest.raises(SetupError, match=message):
        setup.check_client(data, PROJECT)


def test_a_good_client_file_passes():
    assert setup.check_client(client_json(), PROJECT) == client_json()
    assert setup.check_client(client_json(project="any"), None)


# The guided flow -------------------------------------------------------------------------------


def test_console_flow_without_gcloud(answers, opened, downloads, login):
    downloads["client_secret_123-abc.apps.googleusercontent.com.json"] = client_json()
    answers("Not An ID", PROJECT, "", "", "")
    result = setup.Setup().run()
    assert result["logged_in"] and result["email"] == "user@example.com"
    assert result["project"] == PROJECT and result["level"] == "modify"
    assert installed_client() == client_json()
    assert login == ["modify"]
    assert opened[0] == "https://console.cloud.google.com/projectcreate"
    for path in (
        "/apis/enableflow;apiid=gmail.googleapis.com",
        "/auth/overview",
        "/auth/audience",
        "/auth/clients",
    ):
        assert f"https://console.cloud.google.com{path}?project={PROJECT}" in opened


def test_gcloud_creates_the_project_and_enables_the_api(
    monkeypatch, answers, opened, downloads, login
):
    calls = []
    tos_refusals = [1]

    def gcloud(cmd, **kwargs):
        calls.append((cmd[1:], kwargs["env"].get("CLOUDSDK_CORE_DISABLE_PROMPTS")))
        args = cmd[1:]
        out, code, err = "", 0, ""
        if args[:2] == ["auth", "list"]:
            out = "me@example.com\n"
        elif args[:2] == ["projects", "create"] and tos_refusals:
            tos_refusals.pop()
            code, err = 1, "FAILED_PRECONDITION: Callers must accept Terms of Service"
        elif args[:2] == ["projects", "describe"]:
            out = "ACTIVE\n"
        elif args[:2] == ["services", "list"]:
            out = "gmail.googleapis.com\n"
        return subprocess.CompletedProcess(cmd, code, out, err)

    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(subprocess, "run", gcloud)
    monkeypatch.setattr(setup.secrets, "choice", lambda chars: "q")
    project = "gmail-agent-qqqqqq"
    downloads["client_secret_1.json"] = client_json(project=project)
    answers("y", "", "", "")  # create it, retry after accepting the ToS, consent, publish

    result = setup.Setup().run()

    assert result["project"] == project
    assert installed_client() == client_json(project=project)
    assert all(flag == "1" for _, flag in calls)  # gcloud never prompts
    commands = [args for args, _ in calls]
    assert commands.count(["projects", "create", project, "--name=gmail-agent"]) == 2
    assert ["services", "enable", "gmail.googleapis.com", f"--project={project}"] in commands
    assert not any("projectcreate" in url for url in opened)
    assert f"https://console.cloud.google.com/auth/clients?project={project}" in opened


def test_declining_the_new_project_stops(monkeypatch, answers, opened):
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "me@example.com\n", ""),
    )
    answers("n")
    with pytest.raises(SetupError, match="--project ID"):
        setup.Setup().run()
    assert not config.credentials_path().exists()


def test_installed_client_and_working_login_skip_everything(opened, login, isolated_config):
    auth.write_private(config.credentials_path(), json.dumps(client_json()))
    logged_in("readonly")
    result = setup.Setup().run()
    assert result["email"] == "user@example.com" and result["level"] == "readonly"
    assert opened == [] and login == []


def test_explicit_scope_logs_in_again(opened, login):
    auth.write_private(config.credentials_path(), json.dumps(client_json()))
    logged_in("readonly")
    assert setup.Setup(level="full").run()["level"] == "full"
    assert login == ["full"]


def test_client_file_flag(tmp_path, opened, login):
    path = tmp_path / "client.json"
    path.write_text(json.dumps(client_json()))
    setup.Setup(client_file=str(path)).run()
    assert installed_client() == client_json()
    assert opened == [] and login == ["modify"]

    path.write_text(json.dumps(client_json(kind="web")))
    with pytest.raises(SetupError, match="Desktop app"):
        setup.Setup(client_file=str(path)).run()


def test_ctrl_c_then_paste_the_id_and_secret(monkeypatch, answers, opened, login):
    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(setup.time, "sleep", interrupted)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt, stream=None: "GOCSPX-pasted")
    answers(PROJECT, "", "", "", "999-xyz.apps.googleusercontent.com")
    setup.Setup().run()
    client = installed_client()["installed"]
    assert client["client_id"] == "999-xyz.apps.googleusercontent.com"
    assert client["client_secret"] == "GOCSPX-pasted"
    assert client["project_id"] == PROJECT
    assert client["redirect_uris"] == ["http://localhost"]


def test_a_client_from_another_project_is_refused(answers, opened, downloads, login):
    downloads["client_secret_1.json"] = client_json(project="other-project-9")
    answers(PROJECT, "", "", "")
    with pytest.raises(SetupError, match="--project other-project-9"):
        setup.Setup().run()
    assert not config.credentials_path().exists()


def test_setup_needs_a_terminal(answers, opened):
    answers()  # stdin at end of file
    with pytest.raises(SetupError, match="run it in a terminal"):
        setup.Setup(open_browser=False).run()


def test_cli_setup_json(capsys, opened, login):
    auth.write_private(config.credentials_path(), json.dumps(client_json()))
    logged_in("modify")
    assert cli.main(["setup", "--json"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["email"] == "user@example.com"
    assert "skipping the Google Cloud steps" in err

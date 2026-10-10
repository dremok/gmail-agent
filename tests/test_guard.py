import json
import os
from pathlib import Path

import pytest
from mcp import Client

from gmail_agent import cli, config
from gmail_agent.errors import NotAllowedError
from gmail_agent.gmail import Gmail
from gmail_agent.guard import FileGuard
from gmail_agent.server import build_server


@pytest.fixture
def home(isolated_home) -> Path:
    for d in (".ssh", ".aws", "Documents", "project", "Library/LaunchAgents"):
        (isolated_home / d).mkdir(parents=True)
    (isolated_home / ".ssh" / "id_ed25519").write_text("key")
    (isolated_home / ".aws" / "credentials").write_text("secret")
    (isolated_home / "Downloads" / "report.pdf").write_bytes(b"%PDF")
    (isolated_home / "Documents" / "notes.txt").write_text("notes")
    return isolated_home


# The MCP server's allowed folders ----------------------------------------------------------


def test_mcp_default_is_downloads_only(home):
    guard = FileGuard.for_mcp()
    assert guard.describe() == [str((home / "Downloads").resolve())]
    assert guard.writable_dir("~/Downloads/invoices") == (home / "Downloads/invoices").resolve()
    assert guard.readable_file("~/Downloads/report.pdf").name == "report.pdf"
    with pytest.raises(NotAllowedError) as e:
        guard.writable_dir("~/Documents")
    message = str(e.value)
    assert f"gmail-agent mcp --allow-dir {(home / 'Documents').resolve()}" in message
    assert "GMAIL_AGENT_ALLOWED_DIRS" in message
    with pytest.raises(NotAllowedError, match="--allow-dir"):
        guard.readable_file(home / "Documents" / "notes.txt")


def test_env_and_flag_add_folders(home, tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    monkeypatch.setenv("GMAIL_AGENT_ALLOWED_DIRS", f"{home / 'Documents'}{os.pathsep}{extra}")
    guard = FileGuard.for_mcp([str(home / "project")])
    assert len(guard.describe()) == 4
    guard.readable_file(home / "Documents" / "notes.txt")
    guard.writable_dir(extra / "new")
    guard.writable_dir(home / "project")


def test_dot_dot_cannot_leave_a_folder(home):
    guard = FileGuard.for_mcp()
    with pytest.raises(NotAllowedError, match="outside"):
        guard.writable_dir("~/Downloads/../Documents")
    with pytest.raises(NotAllowedError, match="never reads or writes"):
        guard.writable_dir("~/Downloads/../.ssh")


def test_symlinks_cannot_leave_a_folder(home, tmp_path):
    guard = FileGuard.for_mcp()
    (home / "Downloads" / "to-ssh").symlink_to(home / ".ssh")
    (home / "Downloads" / "to-docs").symlink_to(home / "Documents")
    (home / "Downloads" / "creds.txt").symlink_to(home / ".aws" / "credentials")
    with pytest.raises(NotAllowedError, match="never reads or writes"):
        guard.writable_dir(home / "Downloads" / "to-ssh")
    with pytest.raises(NotAllowedError, match="outside"):
        guard.writable_dir(home / "Downloads" / "to-docs" / "sub")
    with pytest.raises(NotAllowedError, match="never reads or writes"):
        guard.readable_file(home / "Downloads" / "creds.txt")


def test_an_allowed_folder_that_is_a_symlink_still_works(isolated_home, tmp_path):
    real = tmp_path / "real-downloads"
    real.mkdir()
    (isolated_home / "Downloads").rmdir()  # the fixture's empty one
    (isolated_home / "Downloads").symlink_to(real)
    assert FileGuard.for_mcp().writable_dir("~/Downloads/x") == real / "x"


# The block list, for the CLI and inside allowed folders ------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "~/.ssh/id_ed25519",
        "~/.SSH/id_ed25519",  # the same folder on a case-insensitive disk
        "~/.aws/credentials",
        "~/.config/gcloud/credentials.db",
        "~/project/.env",
        "~/project/.env.local",
        "~/project/server.pem",
        "~/project/tls.key",
        "~/project/cert.p12",
        "~/Documents/id_rsa",
        "~/.netrc",
        "~/.pypirc",
        "~/.git-credentials",
        "~/.zshrc",
        "~/.bash_profile",
        "~/Library/LaunchAgents/evil.plist",
        "/Library/LaunchDaemons/evil.plist",
    ],
)
def test_blocked_files_are_never_attached(home, path):
    with pytest.raises(NotAllowedError, match="never"):
        FileGuard().readable_file(path)  # the CLI: any folder, but not these
    with pytest.raises(NotAllowedError, match="never"):
        FileGuard([Path("/")]).readable_file(path)  # even inside an allowed folder


@pytest.mark.parametrize(
    "folder",
    [
        "~/.ssh",
        "~/.ssh/new",
        "~/.gnupg",
        "~/.kube",
        "~/Library/LaunchAgents",
        "~/.config/autostart",
    ],
)
def test_blocked_folders_are_never_written(home, folder):
    with pytest.raises(NotAllowedError, match="never reads or writes"):
        FileGuard().writable_dir(folder)
    with pytest.raises(NotAllowedError, match="never reads or writes"):
        FileGuard([home]).writable_dir(folder)


def test_gmail_agents_own_config_is_blocked(home, isolated_config):
    isolated_config.mkdir()
    config.token_path().write_text("{}")
    for guard in (FileGuard(), FileGuard([isolated_config.parent])):
        with pytest.raises(NotAllowedError, match="never reads or writes"):
            guard.readable_file(config.token_path())
        with pytest.raises(NotAllowedError, match="never reads or writes"):
            guard.writable_dir(isolated_config)
        with pytest.raises(NotAllowedError, match="never reads or writes"):
            guard.readable_file(home / ".config" / "gmail-agent" / "credentials.json")


def test_cli_guard_takes_any_other_folder(home, tmp_path):
    (tmp_path / "anywhere.pdf").write_bytes(b"%PDF")
    FileGuard().readable_file(tmp_path / "anywhere.pdf")
    FileGuard().writable_dir(tmp_path / "out")
    FileGuard().readable_file(home / "Documents" / "notes.txt")


# Through the tools ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tools_use_the_allowed_folders(gmail, home, tmp_path):
    server = build_server(connect=lambda: gmail)
    async with Client(server) as client:
        status = await client.call_tool("account_status", {})
        assert status.structured_content["allowed_dirs"] == [str((home / "Downloads").resolve())]
        outside = await client.call_tool(
            "download_attachments", {"message_id": "m1", "out_dir": str(tmp_path / "out")}
        )
        assert outside.is_error
        assert "gmail-agent mcp --allow-dir" in outside.content[0].text
        assert not (tmp_path / "out").exists()
        inside = await client.call_tool(
            "download_attachments", {"message_id": "m1", "out_dir": "~/Downloads/mail"}
        )
        assert not inside.is_error
        secret = await client.call_tool(
            "send_message",
            {
                "to": ["bob@example.com"],
                "subject": "x",
                "attachment_paths": [str(home / ".ssh" / "id_ed25519")],
                "dry_run": True,
            },
        )
        assert secret.is_error
        assert "never" in secret.content[0].text
        tools = {t.name: t for t in (await client.list_tools()).tools}
    folder = str((home / "Downloads").resolve())
    assert folder in tools["download_attachments"].description
    assert folder in tools["send_message"].description


def test_server_instructions_name_the_folders(home):
    server = build_server(connect=lambda: None, allowed_dirs=[str(home / "Documents")])
    assert str((home / "Documents").resolve()) in server.instructions
    assert "--allow-dir" in server.instructions


def test_cli_refuses_blocked_attachments(home, gmail, monkeypatch, capsys, mailbox):
    monkeypatch.setattr(Gmail, "connect", classmethod(lambda cls: gmail))
    argv = ["send", "--to", "a@example.com", "--attach", str(home / ".aws" / "credentials")]
    assert cli.main([*argv, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "not_allowed"
    assert mailbox.sent == []


def test_mcp_allow_dir_flag_is_repeatable():
    args = cli.build_parser().parse_args(["mcp", "--allow-dir", "/a", "--allow-dir", "/b"])
    assert args.allow_dir == ["/a", "/b"]

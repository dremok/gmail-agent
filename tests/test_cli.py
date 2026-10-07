import io
import json
import shlex
import subprocess
import sys

import pytest

from gmail_agent import cli, config
from gmail_agent.gmail import Gmail


@pytest.fixture
def connected(gmail, monkeypatch):
    monkeypatch.setattr(Gmail, "connect", classmethod(lambda cls: gmail))
    return gmail


@pytest.fixture
def connected_readonly(service, monkeypatch):
    g = Gmail(service, config.LEVELS["readonly"])
    monkeypatch.setattr(Gmail, "connect", classmethod(lambda cls: g))
    return g


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for command in (
        "search",
        "message",
        "thread",
        "labels",
        "attachments",
        "download",
        "fetch",
        "read",
        "draft",
        "send",
        "reply",
        "forward",
        "label",
        "mark-read",
        "archive",
        "trash",
        "delete",
        "mcp",
        "login",
        "status",
    ):
        assert command in out


def test_module_entry_point():
    r = subprocess.run(
        [sys.executable, "-m", "gmail_agent", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.startswith("gmail-agent ")


def test_not_logged_in_is_a_clear_setup_error(capsys):
    code, out, err = run(capsys, "search", "invoice")
    assert code == 2
    assert out == ""
    assert "error: No OAuth client file" in err


@pytest.mark.parametrize("argv", [["--json", "message", "m1"], ["message", "m1", "--json"]])
def test_json_before_or_after_the_command(connected, capsys, argv):
    code, out, _ = run(capsys, *argv)
    assert code == 0
    assert json.loads(out)["id"] == "m1"


def test_json_before_a_nested_command(connected, capsys):
    code, out, _ = run(capsys, "--json", "draft", "list")
    assert code == 0
    assert json.loads(out) == {"drafts": [], "next_page_token": None}


def test_epilog_examples_parse():
    parser = cli.build_parser()
    for line in cli.EPILOG.splitlines():
        line = line.strip()
        if line.startswith("gmail-agent "):
            parser.parse_args(shlex.split(line)[1:])


def test_not_logged_in_json(capsys):
    code, out, _ = run(capsys, "search", "invoice", "--json")
    assert code == 2
    assert json.loads(out)["code"] == "setup"


def test_status_json_when_not_set_up(capsys):
    code, out, _ = run(capsys, "status", "--json")
    assert code == 1
    status = json.loads(out)
    assert status["logged_in"] is False
    assert status["credentials_file"] is False
    assert "credentials.json" in status["problem"]


def test_search_human(connected, capsys):
    code, out, _ = run(capsys, "search", "invoice")
    assert code == 0
    assert "m1" in out and "Invoice March" in out
    assert "[1] invoice.pdf  (application/pdf" in out


def test_search_json(connected, capsys):
    code, out, _ = run(capsys, "search", "invoice", "-n", "1", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["messages"][0]["id"] == "m3"
    assert data["next_page_token"] == "1"


def test_message_human(connected, capsys):
    code, out, _ = run(capsys, "message", "m1")
    assert code == 0
    assert "Subject:   Invoice March" in out
    assert "Hi, your invoice is attached." in out


def test_missing_message_exit_code(connected, capsys):
    code, _, err = run(capsys, "message", "nope")
    assert code == 1
    assert "not found" in err


def test_download_prints_paths(connected, capsys, tmp_path):
    code, out, _ = run(capsys, "download", "m1", "-o", str(tmp_path), "--name", "invoice.pdf")
    assert code == 0
    assert out.strip() == str((tmp_path / "invoice.pdf").resolve())


def test_fetch_json(connected, capsys, tmp_path):
    code, out, _ = run(
        capsys, "fetch", "invoice", "-o", str(tmp_path), "--match", "*.pdf", "--json"
    )
    data = json.loads(out)
    assert code == 0
    assert [s["filename"] for s in data["saved"]] == ["invoice.pdf"]


def test_read(connected, capsys):
    code, out, _ = run(capsys, "read", "m1", "invoice.pdf")
    assert code == 0
    assert "Total due 120 EUR" in out


def test_labels_and_thread(connected, capsys):
    assert run(capsys, "labels")[0] == 0
    code, out, _ = run(capsys, "thread", "t1", "--json")
    assert code == 0
    assert json.loads(out)["message_count"] == 2


def test_draft_create_reads_body_from_stdin(connected, capsys, monkeypatch, mailbox):
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin"))
    code, out, _ = run(
        capsys, "draft", "create", "--to", "bob@example.com", "--subject", "Hi", "--body-file", "-"
    )
    assert code == 0
    assert "not sent" in out
    [draft] = mailbox.drafts.values()
    assert draft["mail"].get_content().strip() == "from stdin"


def test_send_json_returns_ids(connected, capsys, mailbox):
    code, out, _ = run(
        capsys, "send", "--to", "bob@example.com", "--subject", "Hi", "--body", "Hello", "--json"
    )
    data = json.loads(out)
    assert code == 0
    assert data["sent"] is True and data["id"] == mailbox.sent[0]["id"]
    assert data["thread_id"]


def test_send_dry_run_prints_mime(connected, capsys, mailbox):
    code, out, _ = run(
        capsys, "send", "--to", "bob@example.com", "--subject", "Hi", "--body", "Hello", "--dry-run"
    )
    assert code == 0
    assert "Dry run" in out and "To: bob@example.com" in out and "users.messages.send" in out
    assert mailbox.sent == []


def test_reply_all_draft(connected, capsys, mailbox):
    code, out, _ = run(capsys, "reply", "m1", "--all", "--body", "ok", "--draft", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["thread_id"] == "t1"
    assert "Carol" in data["to"]
    assert data["draft_id"] in mailbox.drafts


def test_forward(connected, capsys, mailbox):
    code, out, _ = run(capsys, "forward", "m1", "--to", "eve@example.com", "--body", "FYI")
    assert code == 0
    assert out.startswith("Sent message")
    assert mailbox.sent[0]["mail"]["Subject"] == "Fwd: Invoice March"


def test_label_and_mark_commands(connected, capsys, mailbox):
    assert run(capsys, "label", "add", "Receipts", "m1", "m2")[0] == 0
    assert "Label_1" in mailbox.messages["m2"]["labelIds"]
    assert run(capsys, "mark-read", "m1")[0] == 0
    assert "UNREAD" not in mailbox.messages["m1"]["labelIds"]
    code, out, _ = run(capsys, "archive", "t1", "--thread")
    assert code == 0 and out.strip() == "archive: 1 threads"
    code, out, _ = run(capsys, "label", "create", "Projects", "--json")
    assert json.loads(out)["created"] is True


def test_trash_dry_run_json(connected, capsys, mailbox):
    code, out, _ = run(capsys, "trash", "m1", "--dry-run", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["requests"] == [{"method": "users.messages.trash", "params": {"id": "m1"}}]
    assert mailbox.writes == []


def test_delete_needs_permanent_flag(connected, capsys, mailbox):
    code, _, err = run(capsys, "delete", "m1")
    assert code == 1
    assert "--permanent" in err
    assert "m1" in mailbox.messages
    assert run(capsys, "delete", "m1", "--permanent")[0] == 0
    assert "m1" not in mailbox.messages


def test_scope_error_exit_code_and_json(connected_readonly, capsys):
    code, out, _ = run(capsys, "trash", "m1", "--json")
    data = json.loads(out)
    assert code == 2
    assert data["code"] == "scope"
    assert "gmail-agent login --scope modify" in data["error"]


def test_login_rejects_unknown_scope(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["login", "--scope", "everything"])
    assert e.value.code == 2


def test_missing_body_file(connected, capsys):
    code, _, err = run(capsys, "send", "--to", "a@example.com", "--body-file", "/no/such/file")
    assert code == 1
    assert "/no/such/file" in err

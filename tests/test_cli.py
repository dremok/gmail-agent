import json
import subprocess
import sys

import pytest

from gmail_agent import cli
from gmail_agent.gmail import Gmail


@pytest.fixture
def connected(gmail, monkeypatch):
    monkeypatch.setattr(Gmail, "connect", classmethod(lambda cls, allow_drafts=False: gmail))
    return gmail


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


def test_draft_reads_body_from_stdin(connected, capsys, monkeypatch, mailbox):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("from stdin"))
    code, out, _ = run(
        capsys, "draft", "--to", "bob@example.com", "--subject", "Hi", "--body-file", "-"
    )
    assert code == 0
    assert "not sent" in out
    assert mailbox.drafts[0]["mail"].get_content().strip() == "from stdin"

import time
from pathlib import Path

import pytest

from gmail_agent.errors import GmailAgentError, NotFoundError, SetupError

from .conftest import response


def test_search_returns_summaries_newest_first(gmail):
    res = gmail.search("invoice")
    assert [m["id"] for m in res["messages"]] == ["m3", "m1"]
    m1 = res["messages"][1]
    assert m1["thread_id"] == "t1"
    assert m1["from"] == "Billing <billing@example.com>"
    assert m1["subject"] == "Invoice March"
    assert m1["date"].startswith("2026-")
    assert [a["filename"] for a in m1["attachments"]] == ["invoice.pdf", "logo.png"]
    assert res["next_page_token"] is None


def test_search_paginates(gmail):
    first = gmail.search("", max_results=2)
    assert len(first["messages"]) == 2
    assert first["next_page_token"] == "2"
    second = gmail.search("", max_results=2, page_token=first["next_page_token"])
    assert [m["id"] for m in second["messages"]] == ["m2"]
    assert second["next_page_token"] is None


def test_search_with_no_hits(gmail):
    assert gmail.search("nothing-matches-this")["messages"] == []


def test_attachment_metadata_flags_embedded_images(gmail):
    atts = {a["filename"]: a for a in gmail.attachments("m1")}
    assert atts["invoice.pdf"] == {
        "filename": "invoice.pdf",
        "mime_type": "application/pdf",
        "size": atts["invoice.pdf"]["size"],
        "part_id": "1",
        "attachment_id": "ATT-PDF",
        "inline": False,
    }
    assert atts["logo.png"]["inline"] is True


def test_get_message_prefers_plain_text(gmail):
    m = gmail.get_message("m1")
    assert m["body"] == "Hi, your invoice is attached."
    assert m["body_format"] == "plain"
    assert m["message_id_header"] == "<m1@mail.example.com>"
    assert not m["body_truncated"]


def test_get_message_converts_html_and_charset(gmail):
    m = gmail.get_message("m2")
    assert m["body_format"] == "html"
    assert m["body"] == "Café notes\n\nLine two & more"


def test_get_message_truncates(gmail):
    m = gmail.get_message("m1", max_body_chars=6)
    assert m["body"] == "Hi, yo"
    assert m["body_truncated"]


def test_missing_message_is_not_found(gmail):
    with pytest.raises(NotFoundError, match="not found"):
        gmail.get_message("nope")


def test_get_thread_oldest_first(gmail):
    t = gmail.get_thread("t1")
    assert t["message_count"] == 2
    assert [m["id"] for m in t["messages"]] == ["m1", "m3"]


def test_labels_system_first(gmail):
    ids = [lb["id"] for lb in gmail.labels()]
    assert ids[-1] == "Label_1"
    assert set(ids[:-1]) == {"INBOX", "UNREAD", "STARRED", "TRASH"}


def test_profile(gmail):
    assert gmail.profile()["email"] == "user@example.com"


def test_download_all(gmail, tmp_path):
    saved = gmail.download("m1", tmp_path)
    paths = [Path(s["path"]) for s in saved]
    assert [p.name for p in paths] == ["invoice.pdf", "logo.png"]
    assert paths[0].read_bytes().startswith(b"%PDF")
    assert all(p.is_absolute() for p in paths)
    assert saved[0]["message_id"] == "m1" and saved[0]["part_id"] == "1"


def test_download_skip_inline(gmail, tmp_path):
    saved = gmail.download("m1", tmp_path, skip_inline=True)
    assert [Path(s["path"]).name for s in saved] == ["invoice.pdf"]


@pytest.mark.parametrize("selector", ["invoice.pdf", "1", "ATT-PDF"])
def test_download_by_filename_part_or_attachment_id(gmail, tmp_path, selector):
    saved = gmail.download("m1", tmp_path, [selector])
    assert [Path(s["path"]).name for s in saved] == ["invoice.pdf"]


def test_download_unknown_selector_lists_available(gmail, tmp_path):
    with pytest.raises(NotFoundError, match=r"invoice\.pdf \(part 1\)"):
        gmail.download("m1", tmp_path, ["missing.pdf"])
    assert list(tmp_path.iterdir()) == []


def test_download_never_overwrites(gmail, tmp_path):
    (tmp_path / "invoice.pdf").write_bytes(b"mine")
    first = gmail.download("m1", tmp_path, ["invoice.pdf"])
    second = gmail.download("m1", tmp_path, ["invoice.pdf"])
    assert (tmp_path / "invoice.pdf").read_bytes() == b"mine"
    assert Path(first[0]["path"]).name == "invoice_1.pdf"
    assert Path(second[0]["path"]).name == "invoice_2.pdf"


def test_download_blocks_path_traversal(gmail, tmp_path):
    out = tmp_path / "out"
    saved = gmail.download("m2", out, ["../../etc/passwd"])
    path = Path(saved[0]["path"])
    assert path.parent == out.resolve()
    assert path.name == "passwd"


def test_download_matching_with_glob(gmail, tmp_path):
    res = gmail.download_matching("", tmp_path, filename_glob="*.TXT")
    assert res["query"] == "has:attachment"
    assert res["messages_scanned"] == 2
    assert [Path(s["path"]).name for s in res["saved"]] == ["notes.txt"]


def test_download_matching_respects_max_messages(gmail, tmp_path):
    res = gmail.download_matching("", tmp_path, max_messages=1)
    assert res["messages_scanned"] == 1
    assert res["more_messages"] is True
    assert {s["message_id"] for s in res["saved"]} == {"m1"}


def test_read_pdf_text(gmail):
    res = gmail.read_attachment_text("m1", "invoice.pdf")
    assert res["pages"] == 1
    assert "Total due 120 EUR" in res["text"]


def test_read_text_attachment_by_part_id(gmail):
    res = gmail.read_attachment_text("m2", "2")
    assert res["text"] == "Agenda: budget"
    assert res["pages"] is None


def test_read_unsupported_type(gmail):
    with pytest.raises(GmailAgentError, match="download it instead"):
        gmail.read_attachment_text("m2", "archive.zip")


@pytest.mark.parametrize(
    "error",
    [
        {"code": 429, "message": "Too many requests", "status": "RESOURCE_EXHAUSTED"},
        {
            "code": 403,
            "message": "User-rate limit exceeded.",
            "errors": [{"reason": "userRateLimitExceeded", "domain": "usageLimits"}],
        },
    ],
)
def test_rate_limits_say_wait_not_log_in(gmail, fake_http, monkeypatch, error):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)  # the client retries first
    monkeypatch.setattr(
        fake_http, "_messages", lambda *a: response(error["code"], {"error": error})
    )
    with pytest.raises(GmailAgentError, match="rate limit hit during message m1") as e:
        gmail.get_message("m1")
    assert not isinstance(e.value, SetupError)


def test_other_403_points_at_login(gmail, fake_http, monkeypatch):
    error = {"code": 403, "message": "Gmail API has not been used in project 1 or is disabled."}
    monkeypatch.setattr(fake_http, "_messages", lambda *a: response(403, {"error": error}))
    with pytest.raises(SetupError, match="gmail-agent login"):
        gmail.get_message("m1")

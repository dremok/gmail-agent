from email import message_from_string
from email.policy import default as default_policy

import pytest

from gmail_agent import compose
from gmail_agent.compose import Attachment
from gmail_agent.errors import GmailAgentError

from .conftest import hdrs


def payload(**headers):
    return {"headers": hdrs(**headers)}


def roundtrip(msg):
    """Serialize and parse again, the way the receiving side sees it."""
    return message_from_string(msg.as_string(), policy=default_policy)


def test_plain_message_headers_and_body():
    msg = roundtrip(
        compose.build(
            ["a@example.com", "b@example.com"],
            "Hello",
            "Body text",
            cc=["c@example.com"],
            bcc=["d@example.com"],
        )
    )
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg["Cc"] == "c@example.com"
    assert msg["Bcc"] == "d@example.com"
    assert msg["Subject"] == "Hello"
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().strip() == "Body text"


def test_html_gets_plain_alternative():
    msg = roundtrip(compose.build(["a@example.com"], "Hi", html="<p>Hello <b>there</b></p>"))
    assert msg.get_content_type() == "multipart/alternative"
    plain = msg.get_body(("plain",)).get_content().strip()
    html = msg.get_body(("html",)).get_content()
    assert plain == "Hello there"
    assert "<b>there</b>" in html


def test_attachments_make_multipart_mixed():
    atts = [
        Attachment("report.pdf", b"%PDF-1.4 x", "application/pdf"),
        Attachment("data.csv", b"a,b\n1,2", "text/csv"),
        Attachment("forwarded.eml", b"From: x\n\nhi", "message/rfc822"),
    ]
    msg = roundtrip(compose.build(["a@example.com"], "Files", "See attached", attachments=atts))
    assert msg.get_content_type() == "multipart/mixed"
    found = {a.get_filename(): a for a in msg.iter_attachments()}
    assert found["report.pdf"].get_content_type() == "application/pdf"
    assert found["report.pdf"].get_content() == b"%PDF-1.4 x"
    assert found["data.csv"].get_content_type() == "text/csv"
    # message/* cannot be base64-encoded, so it travels as octet-stream with its name.
    assert found["forwarded.eml"].get_content_type() == "application/octet-stream"
    assert found["forwarded.eml"].get_content() == b"From: x\n\nhi"


def test_non_ascii_survives():
    msg = roundtrip(compose.build(["Åsa <asa@example.com>"], "Räkning", "Hej på dig"))
    assert msg["Subject"] == "Räkning"
    assert "Åsa" in msg["To"]
    assert msg.get_content().strip() == "Hej på dig"


def test_load_attachments(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"pdf")
    [att] = compose.load_attachments([tmp_path / "a.pdf"])
    assert (att.filename, att.data, att.mime_type) == ("a.pdf", b"pdf", "application/pdf")
    with pytest.raises(GmailAgentError, match="Attachment not found"):
        compose.load_attachments([tmp_path / "missing.txt"])


@pytest.mark.parametrize(
    ("subject", "expected"),
    [("Hello", "Re: Hello"), ("Re: Hello", "Re: Hello"), ("RE: x", "RE: x"), ("", "Re:")],
)
def test_reply_subject(subject, expected):
    assert compose.reply_subject(subject) == expected


@pytest.mark.parametrize(
    ("subject", "expected"),
    [("Hello", "Fwd: Hello"), ("Fwd: Hello", "Fwd: Hello"), ("FW: x", "FW: x")],
)
def test_forward_subject(subject, expected):
    assert compose.forward_subject(subject) == expected


def test_threading_headers_first_reply():
    assert compose.threading_headers(payload(Message_ID="<a@x>")) == {
        "In-Reply-To": "<a@x>",
        "References": "<a@x>",
    }


def test_threading_headers_extend_references():
    p = payload(Message_ID="<c@x>", References="<a@x> <b@x>", In_Reply_To="<b@x>")
    assert compose.threading_headers(p)["References"] == "<a@x> <b@x> <c@x>"


def test_threading_headers_fall_back_to_in_reply_to():
    p = payload(Message_ID="<b@x>", In_Reply_To="<a@x>")
    assert compose.threading_headers(p)["References"] == "<a@x> <b@x>"


def test_threading_headers_without_message_id():
    assert compose.threading_headers(payload(Subject="x")) == {}


ORIGINAL = payload(
    From="Bob <bob@example.com>",
    To="me@example.com, Carol <carol@example.com>",
    Cc="Dave <dave@example.com>, ME@example.com",
)


def test_reply_goes_to_sender():
    assert compose.reply_recipients(ORIGINAL, "me@example.com", False) == (
        ["Bob <bob@example.com>"],
        [],
    )


def test_reply_all_adds_others_but_not_me():
    to, cc = compose.reply_recipients(ORIGINAL, "me@example.com", True)
    assert to == ["Bob <bob@example.com>", "Carol <carol@example.com>"]
    assert cc == ["Dave <dave@example.com>"]


def test_reply_prefers_reply_to():
    p = payload(From="bob@example.com", Reply_To="list@example.com", To="me@example.com")
    assert compose.reply_recipients(p, "me@example.com", False)[0] == ["list@example.com"]


def test_reply_to_my_own_message_goes_to_its_recipients():
    p = payload(From="Me <me@example.com>", To="bob@example.com")
    assert compose.reply_recipients(p, "me@example.com", False)[0] == ["bob@example.com"]


def test_reply_all_drops_duplicates():
    p = payload(From="bob@example.com", To="bob@example.com, me@example.com", Cc="bob@example.com")
    assert compose.reply_recipients(p, "me@example.com", True) == (["bob@example.com"], [])


def test_quote_text():
    quoted = compose.quote_text("line one\n\nline two", "Mon, 1 Jan", "Bob")
    assert quoted == "On Mon, 1 Jan, Bob wrote:\n> line one\n>\n> line two"


def test_forward_block_lists_original_headers():
    p = payload(From="Bob <bob@example.com>", Subject="Plans", To="me@example.com", Date="Mon")
    block = compose.forward_block_text(p, "original body")
    assert block.startswith("---------- Forwarded message ---------\nFrom: Bob <bob@example.com>")
    assert "Subject: Plans" in block
    assert block.endswith("\n\noriginal body")

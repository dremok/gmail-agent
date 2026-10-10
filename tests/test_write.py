"""Sending, drafts, labels, trash and delete, against the fake Gmail backend."""

import pytest

from gmail_agent import config
from gmail_agent.errors import GmailAgentError, NotFoundError, ScopeError

from .conftest import PDF_BYTES


def sent_mail(mailbox, i=-1):
    return mailbox.sent[i]["mail"]


# Sending ------------------------------------------------------------------------------------


def test_send_returns_ids_and_uploads_mime(gmail, mailbox, tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 report")
    res = gmail.send(
        ["bob@example.com"],
        "Report",
        "See attached.",
        cc=["carol@example.com"],
        attachments=[tmp_path / "report.pdf"],
    )
    assert res["sent"] is True
    assert res["id"].startswith("sent-")
    assert res["thread_id"]
    assert res["attachments"] == ["report.pdf"]
    mail = sent_mail(mailbox)
    assert mail["To"] == "bob@example.com"
    assert mail["Cc"] == "carol@example.com"
    att = next(mail.iter_attachments())
    assert att.get_filename() == "report.pdf"
    assert att.get_content() == b"%PDF-1.4 report"
    assert mailbox.sent[-1]["metadata"] == {}  # no threadId for a new conversation


def test_send_html(gmail, mailbox):
    gmail.send(["bob@example.com"], "Hi", html="<p>Hello <i>Bob</i></p>")
    mail = sent_mail(mailbox)
    assert mail.get_body(("plain",)).get_content().strip() == "Hello Bob"
    assert "<i>Bob</i>" in mail.get_body(("html",)).get_content()


def test_send_needs_a_recipient(gmail):
    with pytest.raises(GmailAgentError, match="at least one To, Cc or Bcc"):
        gmail.send([], "Hi", "body")


def test_send_bcc_only_is_fine(gmail, mailbox):
    gmail.send(None, "Hi", "body", bcc=["hidden@example.com"])
    assert sent_mail(mailbox)["Bcc"] == "hidden@example.com"


def test_send_as_draft(gmail, mailbox):
    res = gmail.send(["bob@example.com"], "Later", "draft body", as_draft=True)
    assert set(res) >= {"draft_id", "message_id", "thread_id"}
    assert "sent" not in res
    assert mailbox.sent == []
    assert res["draft_id"] in mailbox.drafts


def test_dry_run_sends_nothing_and_shows_mime(gmail, mailbox, tmp_path):
    (tmp_path / "a.txt").write_text("attached text")
    res = gmail.send(
        ["bob@example.com"],
        "Dry",
        "Nothing happens",
        attachments=[tmp_path / "a.txt"],
        dry_run=True,
    )
    assert res["dry_run"] is True
    assert res["requests"][0]["method"] == "users.messages.send"
    assert "To: bob@example.com" in res["mime"]
    assert "Subject: Dry" in res["mime"]
    assert 'filename="a.txt"' in res["mime"]
    assert "[13 bytes, not shown in the dry run]" in res["mime"]
    assert res["attachments"] == ["a.txt"]
    assert mailbox.sent == [] and mailbox.writes == []


# Replies --------------------------------------------------------------------------------------


def test_reply_threads_correctly(gmail, mailbox):
    res = gmail.reply("m1", "Thanks, will pay.")
    assert res["sent"] and res["thread_id"] == "t1"
    assert mailbox.sent[-1]["metadata"] == {"threadId": "t1"}
    mail = sent_mail(mailbox)
    assert mail["To"] == "Billing <billing@example.com>"
    assert mail["Subject"] == "Re: Invoice March"
    assert mail["In-Reply-To"] == "<m1@mail.example.com>"
    assert mail["References"] == "<m1@mail.example.com>"
    body = mail.get_content()
    assert body.startswith("Thanks, will pay.")
    assert "billing@example.com> wrote:\n> Hi, your invoice is attached." in body


def test_reply_all_excludes_me(gmail, mailbox):
    gmail.reply("m1", "To everyone", reply_all=True)
    mail = sent_mail(mailbox)
    assert mail["To"] == "Billing <billing@example.com>, Carol <carol@example.com>"
    assert mail["Cc"] == "Dave <dave@example.com>"
    assert "user@example.com" not in mail["To"] + mail["Cc"]


def test_reply_uses_reply_to(gmail, mailbox):
    gmail.reply("m2", "ok", quote=False)
    mail = sent_mail(mailbox)
    assert mail["To"] == "Alice Lists <alice-lists@example.com>"
    assert mail.get_content().strip() == "ok"


def test_reply_to_a_reply_extends_references(gmail, mailbox):
    gmail.reply("m3", "Follow-up", quote=False)
    mail = sent_mail(mailbox)
    assert mail["To"] == "billing@example.com"  # m3 is from me, so it goes to its recipient
    assert mail["Subject"] == "Re: Invoice March"
    assert mail["References"] == "<m1@mail.example.com> <m3@mail.example.com>"


def test_reply_html_quotes_original_html(gmail, mailbox):
    gmail.reply("m1", html="<p>Paid</p>")
    html = sent_mail(mailbox).get_body(("html",)).get_content()
    assert html.startswith("<p>Paid</p>")
    assert "<blockquote" in html and "<b>invoice</b>" in html


def test_reply_as_draft_keeps_thread(gmail, mailbox):
    res = gmail.reply("m1", "draft reply", as_draft=True)
    draft = mailbox.drafts[res["draft_id"]]
    assert draft["metadata"] == {"message": {"threadId": "t1"}}
    assert draft["mail"]["In-Reply-To"] == "<m1@mail.example.com>"


# Forwarding -----------------------------------------------------------------------------------


def test_forward_includes_original_attachments(gmail, mailbox):
    res = gmail.forward("m1", ["eve@example.com"], "FYI")
    assert res["sent"]
    # The embedded logo is left out; the real attachment goes along.
    assert res["attachments"] == ["invoice.pdf"]
    mail = sent_mail(mailbox)
    assert mail["Subject"] == "Fwd: Invoice March"
    assert mail["In-Reply-To"] is None
    att = next(mail.iter_attachments())
    assert att.get_content_type() == "application/pdf"
    assert att.get_content() == PDF_BYTES
    plain = mail.get_body(("plain",)).get_content()
    assert plain.startswith("FYI\n\n---------- Forwarded message ---------")
    assert "From: Billing <billing@example.com>" in plain
    assert "Hi, your invoice is attached." in plain
    html = mail.get_body(("html",)).get_content()
    assert "<b>invoice</b>" in html


def test_forward_without_attachments_plus_a_new_file(gmail, mailbox, tmp_path):
    (tmp_path / "extra.txt").write_text("extra")
    res = gmail.forward(
        "m1", ["eve@example.com"], include_attachments=False, attachments=[tmp_path / "extra.txt"]
    )
    assert res["attachments"] == ["extra.txt"]


def test_forward_html_only_original(gmail, mailbox):
    gmail.forward("m2", ["eve@example.com"], include_attachments=False)
    plain = sent_mail(mailbox).get_body(("plain",)).get_content()
    assert "Café notes" in plain


# Drafts ----------------------------------------------------------------------------------------


def test_draft_lifecycle(gmail, mailbox, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF a")
    (tmp_path / "b.txt").write_text("b")
    created = gmail.send(
        ["bob@example.com"], "Plan", "v1", attachments=[tmp_path / "a.pdf"], as_draft=True
    )
    did = created["draft_id"]

    listed = gmail.list_drafts()
    assert [d["draft_id"] for d in listed["drafts"]] == [did]
    assert listed["drafts"][0]["subject"] == "Plan"

    got = gmail.get_draft(did)
    assert got["body"] == "v1"
    assert [a["filename"] for a in got["attachments"]] == ["a.pdf"]

    updated = gmail.update_draft(did, text="v2", attachments=[tmp_path / "b.txt"])
    assert updated["draft_id"] == did
    mail = mailbox.drafts[did]["mail"]
    assert mail.get_body(("plain",)).get_content().strip() == "v2"
    assert mail["To"] == "bob@example.com" and mail["Subject"] == "Plan"
    names = [a.get_filename() for a in mail.iter_attachments()]
    assert names == ["a.pdf", "b.txt"]
    assert next(mail.iter_attachments()).get_content() == b"%PDF a"

    sent = gmail.send_draft(did)
    assert sent["sent"] and sent["id"] and sent["draft_id"] == did
    assert did not in mailbox.drafts


def test_update_draft_can_drop_attachments_and_keep_thread(gmail, mailbox, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    did = gmail.reply("m1", "r1", attachments=[tmp_path / "a.pdf"], as_draft=True)["draft_id"]
    gmail.update_draft(did, subject="New subject", keep_attachments=False)
    draft = mailbox.drafts[did]
    assert list(draft["mail"].iter_attachments()) == []
    assert draft["mail"]["Subject"] == "New subject"
    assert draft["mail"]["In-Reply-To"] == "<m1@mail.example.com>"
    assert draft["metadata"]["message"]["threadId"] == "t1"
    assert "r1" in draft["mail"].get_body(("plain",)).get_content()


def test_delete_draft(gmail, mailbox):
    did = gmail.send(["bob@example.com"], "x", "y", as_draft=True)["draft_id"]
    assert gmail.delete_draft(did) == {"deleted": True, "draft_id": did}
    assert mailbox.drafts == {}


def test_missing_draft(gmail):
    with pytest.raises(NotFoundError):
        gmail.get_draft("nope")


# Labels and state ------------------------------------------------------------------------------


def test_label_create_rename_delete(gmail, mailbox):
    created = gmail.create_label("Projects/Alpha")
    assert created["created"] and created["name"] == "Projects/Alpha"
    renamed = gmail.rename_label("projects/alpha", "Projects/Beta")
    assert renamed["old_name"] == "Projects/Alpha" and renamed["id"] == created["id"]
    assert gmail.delete_label("Projects/Beta")["deleted"]
    assert all(lb["id"] != created["id"] for lb in mailbox.labels)


def test_system_labels_are_protected(gmail):
    with pytest.raises(GmailAgentError, match="system label"):
        gmail.delete_label("INBOX")


def test_unknown_label(gmail):
    with pytest.raises(NotFoundError, match="label create"):
        gmail.modify_labels(["m1"], add=["Nope"])


def test_modify_labels_by_name_single_and_batch(gmail, mailbox):
    gmail.modify_labels(["m1"], add=["receipts"])
    assert "Label_1" in mailbox.messages["m1"]["labelIds"]
    assert mailbox.writes[-1][1] == "messages/m1/modify"
    gmail.modify_labels(["m1", "m2"], remove=["Receipts"], add=["STARRED"])
    assert mailbox.writes[-1][1] == "messages/batchModify"
    assert mailbox.writes[-1][2] == {
        "ids": ["m1", "m2"],
        "addLabelIds": ["STARRED"],
        "removeLabelIds": ["Label_1"],
    }


@pytest.mark.parametrize(
    ("action", "label", "present"),
    [
        ("read", "UNREAD", False),
        ("unread", "UNREAD", True),
        ("star", "STARRED", True),
        ("archive", "INBOX", False),
        ("unarchive", "INBOX", True),
    ],
)
def test_mark_actions(gmail, mailbox, action, label, present):
    res = gmail.mark(["m1"], action)
    assert res["action"] == action
    assert (label in mailbox.messages["m1"]["labelIds"]) is present


def test_mark_whole_thread(gmail, mailbox):
    gmail.mark(["t1"], "archive", threads=True)
    assert mailbox.writes[-1][1] == "threads/t1/modify"
    assert all("INBOX" not in mailbox.messages[m]["labelIds"] for m in ("m1", "m3"))


def test_trash_and_untrash(gmail, mailbox):
    assert gmail.trash(["m1", "m2"]) == {
        "action": "trash",
        "target": "messages",
        "ids": ["m1", "m2"],
    }
    assert "TRASH" in mailbox.messages["m2"]["labelIds"]
    gmail.trash(["m2"], undo=True)
    assert "TRASH" not in mailbox.messages["m2"]["labelIds"]
    gmail.trash(["t1"], threads=True)
    assert mailbox.writes[-1][1] == "threads/t1/trash"


def test_a_failed_request_in_a_series_says_what_already_happened(gmail, mailbox):
    with pytest.raises(NotFoundError, match=r"\(nope\) not found") as e:
        gmail.trash(["m1", "nope", "m2"])
    assert "The 1 request(s) before it succeeded; the other 1 were not sent." in str(e.value)
    assert "TRASH" in mailbox.messages["m1"]["labelIds"]
    assert "TRASH" not in mailbox.messages["m2"]["labelIds"]


def test_delete_permanently(gmail, mailbox):
    gmail.delete_permanently(["m1", "m2"])
    assert set(mailbox.messages) == {"m3"}
    gmail.delete_permanently(["t1"], threads=True)
    assert mailbox.messages == {}


def test_write_dry_runs_change_nothing(gmail, mailbox):
    for res in (
        gmail.trash(["m1"], dry_run=True),
        gmail.mark(["m1", "m2"], "read", dry_run=True),
        gmail.delete_permanently(["m1"], dry_run=True),
        gmail.create_label("X", dry_run=True),
        gmail.delete_label("Receipts", dry_run=True),
    ):
        assert res["dry_run"] is True
        assert res["requests"]
    assert mailbox.writes == []
    plan = gmail.mark(["m1", "m2"], "read", dry_run=True)["requests"][0]
    assert plan == {
        "method": "users.messages.batchModify",
        "params": {"body": {"ids": ["m1", "m2"], "addLabelIds": [], "removeLabelIds": ["UNREAD"]}},
    }


# Scopes ----------------------------------------------------------------------------------------

READONLY = config.LEVELS["readonly"]
COMPOSE = config.LEVELS["compose"]
MODIFY = config.LEVELS["modify"]
FULL = config.LEVELS["full"]


def test_readonly_can_read_but_not_send(gmail_with, mailbox):
    g = gmail_with(READONLY)
    assert g.search("invoice")["messages"]
    with pytest.raises(ScopeError, match=r"login --scope compose") as e:
        g.send(["bob@example.com"], "x", "y")
    assert "gmail.readonly" in str(e.value)
    assert e.value.code == "scope"
    assert mailbox.sent == []


def test_compose_can_send_but_not_label(gmail_with):
    g = gmail_with(COMPOSE)
    assert g.send(["bob@example.com"], "x", "y")["sent"]
    with pytest.raises(ScopeError, match=r"login --scope modify"):
        g.trash(["m1"])


def test_modify_cannot_delete_permanently(gmail_with, mailbox):
    g = gmail_with(MODIFY)
    g.trash(["m1"])
    g.create_label("Fine")
    with pytest.raises(ScopeError, match=r"login --scope full"):
        g.delete_permanently(["m1"])
    assert "m1" in mailbox.messages


def test_full_can_do_everything(gmail_with):
    g = gmail_with(FULL)
    g.reply("m1", "ok")
    g.delete_permanently(["m2"])


def test_dry_run_reports_missing_scope_without_failing(gmail_with):
    res = gmail_with(MODIFY).delete_permanently(["m1"], dry_run=True)
    assert res["scope_ok"] is False
    assert gmail_with(FULL).delete_permanently(["m1"], dry_run=True)["scope_ok"] is True

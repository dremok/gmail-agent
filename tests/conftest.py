"""A fake Gmail backend at the HTTP layer.

Tests build the real googleapiclient service from its bundled discovery document and route its
HTTP requests here, so method names, parameters and URLs are checked against the actual API.
"""

from __future__ import annotations

import base64
import json
from email.parser import BytesParser
from email.policy import default as default_policy
from urllib.parse import parse_qs, unquote, urlparse

import httplib2
import pytest
from googleapiclient.discovery import build

from gmail_agent.gmail import Gmail


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, built by hand so tests need no PDF writer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def hdrs(**kw: str) -> list[dict]:
    return [{"name": k.replace("_", "-"), "value": v} for k, v in kw.items()]


def text_part(part_id: str, text: str, mime: str = "text/plain", charset: str = "utf-8") -> dict:
    data = text.encode(charset)
    return {
        "partId": part_id,
        "mimeType": mime,
        "filename": "",
        "headers": hdrs(Content_Type=f"{mime}; charset={charset}"),
        "body": {"size": len(data), "data": b64(data)},
    }


class Mailbox:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.attachment_data: dict[tuple[str, str], bytes] = {}
        self.drafts: list[dict] = []

    def add(
        self,
        msg_id,
        thread_id,
        subject,
        sender,
        parts,
        date_ms,
        labels=("INBOX",),
        to="user@example.com",
        extra_headers=None,
    ):
        headers = hdrs(From=sender, To=to, Subject=subject, Date="Thu, 1 Jan 2026 10:00:00 +0000")
        headers += hdrs(Message_ID=f"<{msg_id}@mail.example.com>")
        headers += extra_headers or []
        payload = {
            "partId": "",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": headers,
            "body": {"size": 0},
            "parts": parts,
        }
        self.messages[msg_id] = {
            "id": msg_id,
            "threadId": thread_id,
            "labelIds": list(labels),
            "snippet": subject.lower(),
            "internalDate": str(date_ms),
            "payload": payload,
        }

    def attachment(self, msg_id, part_id, filename, data, mime, att_id, **headers):
        self.attachment_data[(msg_id, att_id)] = data
        return {
            "partId": part_id,
            "mimeType": mime,
            "filename": filename,
            "headers": hdrs(Content_Type=mime, **headers),
            "body": {"attachmentId": att_id, "size": len(data)},
        }

    def search(self, q: str) -> list[dict]:
        terms = q.lower().split()
        hits = []
        for m in sorted(self.messages.values(), key=lambda m: -int(m["internalDate"])):
            text = " ".join(h["value"] for h in m["payload"]["headers"]).lower()
            has_att = any(p.get("filename") for p in m["payload"]["parts"])
            ok = all(has_att if t == "has:attachment" else t.split(":")[-1] in text for t in terms)
            if ok:
                hits.append({"id": m["id"], "threadId": m["threadId"]})
        return hits


def response(status: int, payload) -> tuple:
    body = json.dumps(payload).encode()
    return httplib2.Response({"status": str(status), "content-type": "application/json"}), body


class FakeGmailHttp:
    """Plays the Gmail REST API for the googleapiclient service."""

    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.calls: list[tuple[str, str]] = []

    def request(
        self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None
    ):
        url = urlparse(uri)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = unquote(url.path)
        self.calls.append((method, path))
        box = self.mailbox
        prefix = "/gmail/v1/users/me/"
        if path.startswith("/upload" + prefix + "drafts") and method == "POST":
            return self._create_draft(body, headers)
        if not path.startswith(prefix):
            return response(404, {"error": {"code": 404, "message": "Not Found"}})
        parts = path[len(prefix) :].split("/")

        if parts == ["profile"]:
            return response(
                200,
                {
                    "emailAddress": "user@example.com",
                    "messagesTotal": len(box.messages),
                    "threadsTotal": 2,
                },
            )
        if parts == ["labels"]:
            return response(
                200,
                {
                    "labels": [
                        {"id": "Label_1", "name": "Receipts", "type": "user"},
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                    ]
                },
            )
        if parts == ["messages"]:
            hits = box.search(params.get("q", ""))
            start = int(params.get("pageToken", 0))
            size = int(params.get("maxResults", 100))
            page = hits[start : start + size]
            out = {"resultSizeEstimate": len(hits)}
            if page:
                out["messages"] = page
            if start + size < len(hits):
                out["nextPageToken"] = str(start + size)
            return response(200, out)
        if len(parts) == 2 and parts[0] == "messages":
            msg = box.messages.get(parts[1])
            return (
                response(200, msg)
                if msg
                else response(
                    404, {"error": {"code": 404, "message": "Requested entity was not found."}}
                )
            )
        if len(parts) == 4 and parts[0] == "messages" and parts[2] == "attachments":
            data = box.attachment_data.get((parts[1], parts[3]))
            if data is None:
                return response(
                    400, {"error": {"code": 400, "message": "Invalid attachment token"}}
                )
            return response(200, {"size": len(data), "data": b64(data)})
        if len(parts) == 2 and parts[0] == "threads":
            msgs = sorted(
                (m for m in box.messages.values() if m["threadId"] == parts[1]),
                key=lambda m: int(m["internalDate"]),
            )
            if not msgs:
                return response(
                    404, {"error": {"code": 404, "message": "Requested entity was not found."}}
                )
            return response(200, {"id": parts[1], "messages": msgs})
        return response(404, {"error": {"code": 404, "message": "Not Found"}})

    def _create_draft(self, body, headers):
        ctype = next(v for k, v in headers.items() if k.lower() == "content-type")
        raw = body if isinstance(body, bytes) else body.encode()
        multipart = BytesParser(policy=default_policy).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw
        )
        meta_part, mail_part = list(multipart.iter_parts())
        metadata = json.loads(meta_part.get_content())
        assert mail_part.get_content_type() == "message/rfc822"
        mail = mail_part.get_payload(0)
        self.mailbox.drafts.append({"metadata": metadata, "mail": mail})
        thread = metadata.get("message", {}).get("threadId", "t-new")
        return response(200, {"id": "d1", "message": {"id": "m-draft", "threadId": thread}})

    def close(self):
        return None


PDF_BYTES = minimal_pdf("Total due 120 EUR")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "config"
    monkeypatch.setenv("GMAIL_AGENT_CONFIG_DIR", str(path))
    return path


@pytest.fixture
def mailbox() -> Mailbox:
    box = Mailbox()
    box.add(
        "m1",
        "t1",
        "Invoice March",
        "Billing <billing@example.com>",
        [
            {
                "partId": "0",
                "mimeType": "multipart/alternative",
                "filename": "",
                "headers": [],
                "body": {"size": 0},
                "parts": [
                    text_part("0.0", "Hi, your invoice is attached."),
                    text_part("0.1", "<p>Hi, your <b>invoice</b> is attached.</p>", "text/html"),
                ],
            },
            box.attachment(
                "m1",
                "1",
                "invoice.pdf",
                PDF_BYTES,
                "application/pdf",
                "ATT-PDF",
                Content_Disposition='attachment; filename="invoice.pdf"',
            ),
            box.attachment(
                "m1",
                "2",
                "logo.png",
                b"\x89PNG fake",
                "image/png",
                "ATT-LOGO",
                Content_ID="<logo@example.com>",
                Content_Disposition="inline",
            ),
        ],
        date_ms=1_772_000_000_000,
    )
    box.add(
        "m2",
        "t2",
        "Notes from Alice",
        "Alice <alice@example.com>",
        [
            text_part(
                "0",
                "<html><head><style>p{}</style></head><body><p>Café notes</p>"
                "<p>Line two &amp; more</p></body></html>",
                "text/html",
                "iso-8859-1",
            ),
            box.attachment("m2", "1", "../../etc/passwd", b"not really", "text/plain", "ATT-EVIL"),
            box.attachment("m2", "2", "notes.txt", b"Agenda: budget", "text/plain", "ATT-TXT"),
            box.attachment("m2", "3", "archive.zip", b"PK\x03\x04", "application/zip", "ATT-ZIP"),
        ],
        date_ms=1_771_000_000_000,
    )
    box.add(
        "m3",
        "t1",
        "Re: Invoice March",
        "User <user@example.com>",
        [text_part("0", "Thanks, paid.")],
        date_ms=1_773_000_000_000,
        to="billing@example.com",
        extra_headers=hdrs(References="<m1@mail.example.com>"),
    )
    return box


@pytest.fixture
def fake_http(mailbox) -> FakeGmailHttp:
    return FakeGmailHttp(mailbox)


@pytest.fixture
def gmail(fake_http) -> Gmail:
    service = build("gmail", "v1", http=fake_http, static_discovery=True)
    return Gmail(service)

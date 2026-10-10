"""A fake Gmail backend at the HTTP layer.

Tests build the real googleapiclient service from its bundled discovery document and route its
HTTP requests here, so method names, parameters and URLs are checked against the actual API.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from urllib.parse import parse_qs, unquote, urlparse

import httplib2
import pytest
from googleapiclient.discovery import build

from gmail_agent.gmail import Gmail

ME = "user@example.com"


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


def response(status: int, payload=None) -> tuple:
    if status == 204:
        return httplib2.Response({"status": "204"}), b""
    body = json.dumps(payload).encode()
    return httplib2.Response({"status": str(status), "content-type": "application/json"}), body


def not_found(what: str = "Requested entity was not found.") -> tuple:
    return response(404, {"error": {"code": 404, "message": what}})


class Mailbox:
    """The state behind the fake API, plus a log of every write for assertions."""

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.attachment_data: dict[tuple[str, str], bytes] = {}
        self.labels: list[dict] = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "UNREAD", "name": "UNREAD", "type": "system"},
            {"id": "STARRED", "name": "STARRED", "type": "system"},
            {"id": "TRASH", "name": "TRASH", "type": "system"},
            {"id": "Label_1", "name": "Receipts", "type": "user"},
        ]
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.writes: list[tuple[str, str, dict | None]] = []
        self._counter = 100

    def next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def add(
        self,
        msg_id,
        thread_id,
        subject,
        sender,
        parts,
        date_ms,
        labels=("INBOX",),
        to=ME,
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

    def payload_from_email(self, part: EmailMessage, msg_id: str, part_id: str = "") -> dict:
        """Turn an uploaded MIME message into the MessagePart tree Gmail would return."""
        node = {
            "partId": part_id,
            "mimeType": part.get_content_type(),
            "filename": part.get_filename() or "",
            "headers": [{"name": k, "value": str(v)} for k, v in part.items()],
        }
        if part.is_multipart():
            node["body"] = {"size": 0}
            node["parts"] = [
                self.payload_from_email(child, msg_id, f"{part_id}.{i}" if part_id else str(i))
                for i, child in enumerate(part.iter_parts())
            ]
        else:
            data = part.get_payload(decode=True) or b""
            if node["filename"]:
                att_id = f"ATT-{msg_id}-{part_id}"
                self.attachment_data[(msg_id, att_id)] = data
                node["body"] = {"attachmentId": att_id, "size": len(data)}
            else:
                node["body"] = {"size": len(data), "data": b64(data)}
        return node

    def message_from_email(self, mail, msg_id, thread_id, labels) -> dict:
        return {
            "id": msg_id,
            "threadId": thread_id,
            "labelIds": labels,
            "snippet": "",
            "internalDate": "1774000000000",
            "payload": self.payload_from_email(mail, msg_id),
        }

    def search(self, q: str) -> list[dict]:
        terms = q.lower().split()
        hits = []
        for m in sorted(self.messages.values(), key=lambda m: -int(m["internalDate"])):
            text = " ".join(h["value"] for h in m["payload"]["headers"]).lower()
            has_att = any(p.get("filename") for p in m["payload"].get("parts", []))
            ok = all(has_att if t == "has:attachment" else t.split(":")[-1] in text for t in terms)
            if ok:
                hits.append({"id": m["id"], "threadId": m["threadId"]})
        return hits

    def relabel(self, msg: dict, add: list[str], remove: list[str]) -> None:
        labels = [lb for lb in msg["labelIds"] if lb not in remove]
        msg["labelIds"] = labels + [lb for lb in add if lb not in labels]


class FakeGmailHttp:
    """Plays the Gmail REST API for the googleapiclient service."""

    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.calls: list[tuple[str, str]] = []
        self.uploads: dict[str, tuple[str, list[str], dict]] = {}

    def request(
        self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None
    ):
        url = urlparse(uri)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = unquote(url.path)
        self.calls.append((method, path))
        upload = path.startswith("/upload/")
        path = path.removeprefix("/upload")
        prefix = "/gmail/v1/users/me/"
        if not path.startswith(prefix):
            return not_found("Not Found")
        parts = path[len(prefix) :].split("/")
        if method != "GET":
            data = None if upload or not body else json.loads(body)
            self.mailbox.writes.append((method, "/".join(parts), data))
        if upload:
            return self._resumable(uri, method, parts, params, body, headers)
        handler = getattr(self, f"_{parts[0]}", None)
        if handler is None:
            return not_found("Not Found")
        return handler(method, parts[1:], params, json.loads(body) if body else {})

    # /profile
    def _profile(self, method, rest, params, data):
        box = self.mailbox
        return response(200, {"emailAddress": ME, "messagesTotal": len(box.messages)})

    # /labels
    def _labels(self, method, rest, params, data):
        box = self.mailbox
        if not rest:
            if method == "GET":
                return response(200, {"labels": box.labels})
            label = {"id": box.next_id("Label_"), "name": data["name"], "type": "user"}
            box.labels.append(label)
            return response(200, label)
        label = next((lb for lb in box.labels if lb["id"] == rest[0]), None)
        if label is None:
            return not_found()
        if method == "PATCH":
            label["name"] = data["name"]
            return response(200, label)
        if method == "DELETE":
            box.labels.remove(label)
            return response(204)
        return response(200, label)

    # /messages
    def _messages(self, method, rest, params, data):
        box = self.mailbox
        if not rest:
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
        if rest == ["batchModify"]:
            for mid in data["ids"]:
                box.relabel(
                    box.messages[mid], data.get("addLabelIds", []), data.get("removeLabelIds", [])
                )
            return response(204)
        if rest == ["batchDelete"]:
            for mid in data["ids"]:
                box.messages.pop(mid, None)
            return response(204)
        msg = box.messages.get(rest[0])
        if len(rest) == 3 and rest[1] == "attachments":
            blob = box.attachment_data.get((rest[0], rest[2]))
            if blob is None:
                return response(
                    400, {"error": {"code": 400, "message": "Invalid attachment token"}}
                )
            return response(200, {"size": len(blob), "data": b64(blob)})
        if msg is None:
            return not_found()
        action = rest[1] if len(rest) > 1 else None
        if action == "modify":
            box.relabel(msg, data.get("addLabelIds", []), data.get("removeLabelIds", []))
        elif action == "trash":
            box.relabel(msg, ["TRASH"], ["INBOX"])
        elif action == "untrash":
            box.relabel(msg, [], ["TRASH"])
        elif method == "DELETE":
            del box.messages[rest[0]]
            return response(204)
        return response(200, msg)

    # /threads
    def _threads(self, method, rest, params, data):
        box = self.mailbox
        msgs = sorted(
            (m for m in box.messages.values() if m["threadId"] == rest[0]),
            key=lambda m: int(m["internalDate"]),
        )
        if not msgs:
            return not_found()
        action = rest[1] if len(rest) > 1 else None
        for msg in msgs:
            if action == "modify":
                box.relabel(msg, data.get("addLabelIds", []), data.get("removeLabelIds", []))
            elif action == "trash":
                box.relabel(msg, ["TRASH"], ["INBOX"])
            elif action == "untrash":
                box.relabel(msg, [], ["TRASH"])
            elif method == "DELETE":
                del box.messages[msg["id"]]
        if method == "DELETE":
            return response(204)
        return response(200, {"id": rest[0], "messages": msgs})

    # /drafts
    def _drafts(self, method, rest, params, data):
        box = self.mailbox
        if not rest:
            items = [
                {"id": d["id"], "message": {"id": d["message"]["id"]}} for d in box.drafts.values()
            ]
            return response(200, {"drafts": items} if items else {})
        if rest == ["send"]:
            draft = box.drafts.pop(data["id"], None)
            if draft is None:
                return not_found()
            box.sent.append({"metadata": data, "mail": draft["mail"], "id": draft["message"]["id"]})
            msg = draft["message"]
            return response(
                200, {"id": msg["id"], "threadId": msg["threadId"], "labelIds": ["SENT"]}
            )
        draft = box.drafts.get(rest[0])
        if draft is None:
            return not_found()
        if method == "DELETE":
            del box.drafts[rest[0]]
            return response(204)
        return response(200, {"id": draft["id"], "message": draft["message"]})

    def _resumable(self, uri, method, parts, params, body, headers):
        """Resumable media upload: a start request with the JSON metadata, then a PUT with
        the bytes to the session URI it handed out."""
        if params.get("uploadType") == "resumable":
            assert headers["X-Upload-Content-Type"] == "message/rfc822"
            upload_id = f"up{len(self.uploads)}"
            self.uploads[upload_id] = (method, parts, json.loads(body) if body else {})
            location = f"{uri.split('?')[0]}?upload_id={upload_id}"
            return httplib2.Response({"status": "200", "location": location}), b""
        method, parts, metadata = self.uploads.pop(params["upload_id"])
        raw = body.read() if hasattr(body, "read") else body
        mail = BytesParser(policy=default_policy).parsebytes(raw)
        return self._upload(method, parts, metadata, mail)

    def _upload(self, method, parts, metadata, mail):
        box = self.mailbox
        meta_msg = metadata.get("message", metadata)
        thread_id = meta_msg.get("threadId") or box.next_id("t-new-")
        if parts == ["messages", "send"]:
            msg_id = box.next_id("sent-")
            box.sent.append({"metadata": metadata, "mail": mail, "id": msg_id})
            return response(200, {"id": msg_id, "threadId": thread_id, "labelIds": ["SENT"]})
        if parts == ["drafts"] and method == "POST":
            draft_id = box.next_id("d")
        elif parts[0] == "drafts" and method == "PUT" and parts[1] in box.drafts:
            draft_id = parts[1]
        else:
            return not_found()
        msg = box.message_from_email(mail, box.next_id("m-draft-"), thread_id, ["DRAFT"])
        box.drafts[draft_id] = {"id": draft_id, "message": msg, "mail": mail, "metadata": metadata}
        return response(200, {"id": draft_id, "message": {"id": msg["id"], "threadId": thread_id}})

    def close(self):
        return None


class Connection(FakeGmailHttp):
    """One connection, like an httplib2.Http, that notices when two threads use it at once.

    With a `together` barrier, the first requests wait for each other, so they are in flight
    at the same moment whether or not they share a connection.
    """

    def __init__(
        self, mailbox, overlaps: list[str], together: threading.Barrier | None = None
    ) -> None:
        super().__init__(mailbox)
        self.together = together
        self.overlaps = overlaps
        self.busy = threading.Lock()

    def request(self, *args, **kwargs):
        mine = self.busy.acquire(blocking=False)
        if not mine:
            self.overlaps.append(threading.current_thread().name)
        try:
            if self.together and not self.together.broken:
                self.together.wait()
            else:
                time.sleep(0.01)  # stay busy long enough for a shared connection to show
            return super().request(*args, **kwargs)
        finally:
            if mine:
                self.busy.release()


PDF_BYTES = minimal_pdf("Total due 120 EUR")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if anything tries to reach Google for real."""

    def refuse(*args, **kwargs):
        raise AssertionError("test tried to use the network")

    monkeypatch.setattr(httplib2.Http, "request", refuse)
    monkeypatch.setattr("google.auth.transport.requests.Request.__call__", refuse)


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
        labels=("INBOX", "UNREAD"),
        to=f"{ME}, Carol <carol@example.com>",
        extra_headers=hdrs(Cc="Dave <dave@example.com>"),
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
        extra_headers=hdrs(Reply_To="Alice Lists <alice-lists@example.com>"),
    )
    box.add(
        "m3",
        "t1",
        "Re: Invoice March",
        f"User <{ME}>",
        [text_part("0", "Thanks, paid.")],
        date_ms=1_773_000_000_000,
        to="billing@example.com",
        extra_headers=hdrs(References="<m1@mail.example.com>", In_Reply_To="<m1@mail.example.com>"),
    )
    return box


@pytest.fixture
def fake_http(mailbox) -> FakeGmailHttp:
    return FakeGmailHttp(mailbox)


@pytest.fixture
def service(fake_http):
    return build("gmail", "v1", http=fake_http, static_discovery=True)


@pytest.fixture
def gmail(service) -> Gmail:
    return Gmail(service)


@pytest.fixture
def gmail_with(service):
    """A Gmail client that believes the login granted exactly these scopes."""
    return lambda scopes: Gmail(service, scopes)

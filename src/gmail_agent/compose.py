"""Building outgoing MIME messages: new mail, replies and forwards."""

from __future__ import annotations

import html as html_lib
import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path

from . import mime
from .errors import GmailAgentError


@dataclass
class Attachment:
    filename: str
    data: bytes
    mime_type: str = "application/octet-stream"


def load_attachments(paths: list[str | Path] | None) -> list[Attachment]:
    out = []
    for item in paths or []:
        path = Path(item).expanduser()
        if not path.is_file():
            raise GmailAgentError(f"Attachment not found: {path}")
        ctype, _ = mimetypes.guess_type(path.name)
        out.append(Attachment(path.name, path.read_bytes(), ctype or "application/octet-stream"))
    return out


def build(
    to: list[str] | None = None,
    subject: str = "",
    text: str | None = None,
    html: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[Attachment] | None = None,
    headers: dict[str, str] | None = None,
) -> EmailMessage:
    """A complete RFC 5322 message. Gmail fills in From and Date when it sends.

    With `html` the body is multipart/alternative; the plain part is derived from the HTML
    when `text` is not given.
    """
    if text is None:
        text = mime.html_to_text(html) if html else ""
    msg = EmailMessage()
    for name, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        values = [v for v in values or [] if v.strip()]
        if values:
            msg[name] = ", ".join(values)
    msg["Subject"] = subject or ""
    for name, value in (headers or {}).items():
        if value:
            msg[name] = value
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        maintype, _, subtype = att.mime_type.partition("/")
        if maintype in ("message", "multipart") or not subtype:
            # message/* and multipart/* may not be base64-encoded (RFC 2046), so attach the
            # bytes under a neutral type instead. The filename keeps its extension.
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.filename)
    return msg


def recipient_count(msg: EmailMessage) -> int:
    return len(getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", [])))


def _prefixed(subject: str, prefix: str, also: tuple[str, ...] = ()) -> str:
    lowered = subject.lower()
    if any(lowered.startswith(p.lower()) for p in (prefix, *also)):
        return subject
    return f"{prefix} {subject}".strip()


def reply_subject(subject: str) -> str:
    return _prefixed(subject, "Re:")


def forward_subject(subject: str) -> str:
    return _prefixed(subject, "Fwd:", ("Fw:",))


def threading_headers(payload: dict) -> dict[str, str]:
    """In-Reply-To and References for a reply to the message with this payload."""
    message_id = mime.header(payload, "message-id")
    if not message_id:
        return {}
    refs = mime.header(payload, "references") or mime.header(payload, "in-reply-to")
    return {"In-Reply-To": message_id, "References": f"{refs} {message_id}".strip()}


def _addresses(*values: str) -> list[tuple[str, str]]:
    return [(name, addr) for name, addr in getaddresses(list(values)) if addr]


def reply_recipients(payload: dict, me: str, reply_all: bool) -> tuple[list[str], list[str]]:
    """(to, cc) for a reply, the way mail clients do it.

    Reply goes to Reply-To, else From. If the original was sent by me, it goes to the
    original recipients instead. Reply-all adds the other To and Cc addresses. My own
    address and duplicates are dropped.
    """
    me = me.lower()
    sender = mime.header(payload, "reply-to") or mime.header(payload, "from")
    orig_to = mime.header(payload, "to")
    orig_cc = mime.header(payload, "cc")
    from_me = any(addr.lower() == me for _, addr in _addresses(mime.header(payload, "from")))

    to = _addresses(orig_to) if from_me else _addresses(sender)
    cc: list[tuple[str, str]] = []
    if reply_all:
        if not from_me:
            to += _addresses(orig_to)
        cc = _addresses(orig_cc)

    seen = {me}
    out: list[list[str]] = [[], []]
    for i, group in enumerate((to, cc)):
        for name, addr in group:
            if addr.lower() in seen:
                continue
            seen.add(addr.lower())
            out[i].append(formataddr((name, addr)))
    return out[0], out[1]


def quote_text(text: str, date: str, sender: str) -> str:
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    return f"On {date}, {sender} wrote:\n{quoted}"


def quote_html(original_html: str | None, original_text: str, date: str, sender: str) -> str:
    inner = original_html or f"<pre>{html_lib.escape(original_text)}</pre>"
    return (
        f"<div>On {html_lib.escape(date)}, {html_lib.escape(sender)} wrote:</div>"
        f'<blockquote style="margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex">'
        f"{inner}</blockquote>"
    )


FORWARD_HEADERS = ("from", "date", "subject", "to", "cc")


def forward_block_text(payload: dict, body: str) -> str:
    lines = ["---------- Forwarded message ---------"]
    for name in FORWARD_HEADERS:
        if value := mime.header(payload, name):
            lines.append(f"{name.title()}: {value}")
    return "\n".join(lines) + "\n\n" + body


def forward_block_html(payload: dict, original_html: str) -> str:
    rows = "".join(
        f"{name.title()}: {html_lib.escape(value)}<br>"
        for name in FORWARD_HEADERS
        if (value := mime.header(payload, name))
    )
    return f"<div>---------- Forwarded message ---------<br>{rows}</div><br>{original_html}"


def text_to_html(text: str) -> str:
    return html_lib.escape(text).replace("\n", "<br>\n")

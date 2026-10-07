"""Helpers for the MessagePart trees that the Gmail API returns."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from typing import Any, ClassVar


def b64decode(data: str) -> bytes:
    """Gmail uses unpadded base64url."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def header(part: dict, name: str) -> str:
    name = name.lower()
    return next(
        (h["value"] for h in part.get("headers", []) if h["name"].lower() == name),
        "",
    )


def walk(part: dict) -> Iterator[dict]:
    """Depth-first, in document order."""
    yield part
    for child in part.get("parts", []):
        yield from walk(child)


def is_attachment(part: dict) -> bool:
    body = part.get("body", {})
    return bool(part.get("filename")) and bool(body.get("attachmentId") or body.get("data"))


def attachment_info(part: dict) -> dict[str, Any]:
    mime = part.get("mimeType", "application/octet-stream")
    disposition = header(part, "content-disposition").lower()
    # Images embedded in the HTML body by Content-ID: logos, signatures, tracking pixels.
    # Disposition alone is not enough, since some mail clients send real PDFs as "inline".
    inline = (
        mime.startswith("image/")
        and bool(header(part, "content-id"))
        and not disposition.startswith("attachment")
    )
    body = part.get("body", {})
    return {
        "filename": part["filename"],
        "mime_type": mime,
        "size": body.get("size", 0),
        "part_id": part.get("partId", ""),
        "attachment_id": body.get("attachmentId", ""),
        "inline": inline,
    }


def attachments(payload: dict) -> list[dict[str, Any]]:
    return [attachment_info(p) for p in walk(payload) if is_attachment(p)]


def find_part(payload: dict, part_id: str) -> dict | None:
    return next((p for p in walk(payload) if p.get("partId") == part_id), None)


def charset(part: dict) -> str:
    msg = Message()
    msg["Content-Type"] = header(part, "content-type") or "text/plain"
    return msg.get_content_charset() or "utf-8"


def decode_text(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:  # unknown charset name
        return data.decode("utf-8", errors="replace")


def body_parts(payload: dict) -> tuple[str | None, str | None]:
    """The plain-text and HTML bodies as written, each None if the message has none."""
    plain, html = [], []
    for part in walk(payload):
        data = part.get("body", {}).get("data")
        if part.get("filename") or not data:
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(decode_text(b64decode(data), charset(part)))
        elif mime == "text/html":
            html.append(decode_text(b64decode(data), charset(part)))
    return (
        "\n\n".join(t.strip() for t in plain) if plain else None,
        "\n".join(html) if html else None,
    )


def body_text(payload: dict) -> tuple[str, str]:
    """The readable body and where it came from: "plain", "html" (converted) or "none"."""
    plain, html = body_parts(payload)
    if plain is not None:
        return plain, "plain"
    if html is not None:
        return html_to_text(html), "html"
    return "", "none"


class _TextExtractor(HTMLParser):
    BLOCK: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "hr",
    }
    SKIP: ClassVar[set[str]] = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [
        re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(parser.out).split("\n")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def internal_date(msg: dict) -> str:
    """Gmail's internalDate (epoch ms, when Google received it) as local ISO 8601."""
    ms = msg.get("internalDate")
    if not ms:
        return ""
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=UTC).astimezone()
    return dt.isoformat(timespec="seconds")

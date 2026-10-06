"""The Gmail operations shared by the CLI and the MCP server."""

from __future__ import annotations

import fnmatch
import io
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from . import auth, mime
from .errors import GmailAgentError, NotFoundError, SetupError
from .files import write_new_file

DEFAULT_BODY_CHARS = 20_000
DEFAULT_TEXT_CHARS = 50_000
TEXT_SUFFIXES = {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".log", ".ics", ".vcf"}


def _http_error(e: Exception, what: str) -> GmailAgentError:
    status = getattr(getattr(e, "resp", None), "status", None)
    reason = getattr(e, "reason", None) or str(e)
    if status == 404:
        return NotFoundError(f"{what} not found (Gmail returned 404). Check the id.")
    if status in (401, 403):
        return SetupError(
            f"Gmail refused access to {what} ({status}: {reason}). If this mentions scopes or "
            "credentials, run: gmail-agent login"
        )
    if status == 429:
        return GmailAgentError(f"Gmail rate limit hit while fetching {what}. Wait and retry.")
    return GmailAgentError(f"Gmail API error for {what} ({status}): {reason}")


def _execute(request, what: str) -> dict:
    from googleapiclient.errors import HttpError

    try:
        return request.execute(num_retries=2)
    except HttpError as e:
        raise _http_error(e, what) from e
    except OSError as e:
        raise GmailAgentError(f"Network error while fetching {what}: {e}") from e


def _truncate(text: str, limit: int | None) -> tuple[str, bool]:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


class Gmail:
    """Thin wrapper around a googleapiclient Gmail service. Pass any object with the same shape
    (tests use a fake)."""

    def __init__(self, service: Any) -> None:
        self._svc = service
        self._users = service.users()

    @classmethod
    def connect(cls, allow_drafts: bool = False) -> Gmail:
        creds = auth.load_credentials(require_drafts=allow_drafts)
        return cls(auth.build_service(creds))

    # Reading -----------------------------------------------------------------------------

    def profile(self) -> dict[str, Any]:
        p = _execute(self._users.getProfile(userId="me"), "profile")
        return {
            "email": p.get("emailAddress", ""),
            "messages_total": p.get("messagesTotal", 0),
            "threads_total": p.get("threadsTotal", 0),
        }

    def _raw_message(self, message_id: str) -> dict:
        return _execute(
            self._users.messages().get(userId="me", id=message_id, format="full"),
            f"message {message_id}",
        )

    @staticmethod
    def _summary(msg: dict) -> dict[str, Any]:
        payload = msg.get("payload", {})
        return {
            "id": msg["id"],
            "thread_id": msg.get("threadId", ""),
            "date": mime.internal_date(msg),
            "from": mime.header(payload, "from"),
            "to": mime.header(payload, "to"),
            "subject": mime.header(payload, "subject"),
            "snippet": msg.get("snippet", ""),
            "labels": msg.get("labelIds", []),
            "attachments": mime.attachments(payload),
        }

    def _detail(self, msg: dict, max_body_chars: int | None) -> dict[str, Any]:
        payload = msg.get("payload", {})
        text, source = mime.body_text(payload)
        text, truncated = _truncate(text, max_body_chars)
        return {
            **self._summary(msg),
            "cc": mime.header(payload, "cc"),
            "reply_to": mime.header(payload, "reply-to"),
            "date_header": mime.header(payload, "date"),
            "message_id_header": mime.header(payload, "message-id"),
            "body": text,
            "body_format": source,
            "body_truncated": truncated,
        }

    def search(
        self,
        query: str,
        max_results: int = 20,
        page_token: str | None = None,
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """One page of messages matching a Gmail query, newest first."""
        max_results = max(1, min(int(max_results), 500))
        resp = _execute(
            self._users.messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
                pageToken=page_token or None,
                includeSpamTrash=include_spam_trash,
            ),
            f"search {query!r}",
        )
        messages = [self._summary(self._raw_message(m["id"])) for m in resp.get("messages", [])]
        return {
            "query": query,
            "messages": messages,
            "next_page_token": resp.get("nextPageToken"),
            "result_size_estimate": resp.get("resultSizeEstimate", len(messages)),
        }

    def get_message(
        self, message_id: str, max_body_chars: int | None = DEFAULT_BODY_CHARS
    ) -> dict[str, Any]:
        return self._detail(self._raw_message(message_id), max_body_chars)

    def get_thread(
        self, thread_id: str, max_body_chars: int | None = DEFAULT_BODY_CHARS
    ) -> dict[str, Any]:
        thread = _execute(
            self._users.threads().get(userId="me", id=thread_id, format="full"),
            f"thread {thread_id}",
        )
        messages = [self._detail(m, max_body_chars) for m in thread.get("messages", [])]
        return {"id": thread["id"], "message_count": len(messages), "messages": messages}

    def labels(self) -> list[dict[str, Any]]:
        resp = _execute(self._users.labels().list(userId="me"), "labels")
        labels = [
            {"id": lb["id"], "name": lb["name"], "type": lb.get("type", "user")}
            for lb in resp.get("labels", [])
        ]
        return sorted(labels, key=lambda lb: (lb["type"] != "system", lb["name"].lower()))

    # Attachments -------------------------------------------------------------------------

    def attachments(self, message_id: str) -> list[dict[str, Any]]:
        return mime.attachments(self._raw_message(message_id).get("payload", {}))

    def _attachment_bytes(self, message_id: str, part: dict) -> bytes:
        body = part.get("body", {})
        if body.get("data"):
            return mime.b64decode(body["data"])
        resp = _execute(
            self._users.messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=body["attachmentId"]),
            f"attachment {part.get('filename')!r} of message {message_id}",
        )
        return mime.b64decode(resp.get("data", ""))

    @staticmethod
    def _select(payload: dict, message_id: str, selectors: list[str] | None) -> list[dict]:
        """Attachment parts matching any selector: a filename, part id, or attachment id."""
        parts = [p for p in mime.walk(payload) if mime.is_attachment(p)]
        if not selectors:
            return parts
        chosen, missing = [], []
        for sel in selectors:
            hits = [
                p
                for p in parts
                if sel in (p["filename"], p.get("partId"), p.get("body", {}).get("attachmentId"))
            ]
            if not hits:
                missing.append(sel)
            chosen += [p for p in hits if p not in chosen]
        if missing:
            available = ", ".join(f"{p['filename']} (part {p.get('partId')})" for p in parts)
            raise NotFoundError(
                f"No attachment matching {', '.join(map(repr, missing))} in message {message_id}. "
                f"Available: {available or 'none'}. Select by filename or part id; Gmail "
                "attachment ids can change between requests."
            )
        return chosen

    def _save(self, message_id: str, part: dict, out_dir: Path) -> dict[str, Any]:
        data = self._attachment_bytes(message_id, part)
        path = write_new_file(out_dir, part["filename"], data)
        info = mime.attachment_info(part)
        return {
            "path": str(path),
            "filename": info["filename"],
            "mime_type": info["mime_type"],
            "size": len(data),
            "message_id": message_id,
            "part_id": info["part_id"],
        }

    def download(
        self,
        message_id: str,
        out_dir: str | Path,
        select: list[str] | None = None,
        skip_inline: bool = False,
    ) -> list[dict[str, Any]]:
        """Save a message's attachments (all, or those matching `select`). Never overwrites."""
        payload = self._raw_message(message_id).get("payload", {})
        parts = self._select(payload, message_id, select)
        if skip_inline and not select:
            parts = [p for p in parts if not mime.attachment_info(p)["inline"]]
        return [self._save(message_id, p, Path(out_dir)) for p in parts]

    def download_matching(
        self,
        query: str,
        out_dir: str | Path,
        filename_glob: str | None = None,
        max_messages: int = 50,
        skip_inline: bool = False,
    ) -> dict[str, Any]:
        """Save every attachment of every message matching `query` (up to `max_messages`)."""
        q = query if "has:attachment" in query else f"{query} has:attachment".strip()
        ids: list[str] = []
        token = None
        while len(ids) < max_messages:
            resp = _execute(
                self._users.messages().list(
                    userId="me", q=q, maxResults=min(500, max_messages - len(ids)), pageToken=token
                ),
                f"search {q!r}",
            )
            ids += [m["id"] for m in resp.get("messages", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        saved = []
        for mid in ids[:max_messages]:
            payload = self._raw_message(mid).get("payload", {})
            for part in self._select(payload, mid, None):
                info = mime.attachment_info(part)
                if skip_inline and info["inline"]:
                    continue
                if filename_glob and not fnmatch.fnmatch(
                    info["filename"].lower(), filename_glob.lower()
                ):
                    continue
                saved.append(self._save(mid, part, Path(out_dir)))
        return {
            "query": q,
            "messages_scanned": len(ids[:max_messages]),
            "more_messages": bool(token),
            "saved": saved,
        }

    def read_attachment_text(
        self, message_id: str, attachment: str, max_chars: int | None = DEFAULT_TEXT_CHARS
    ) -> dict[str, Any]:
        """Extract text from a PDF or text attachment without saving it."""
        payload = self._raw_message(message_id).get("payload", {})
        part = self._select(payload, message_id, [attachment])[0]
        info = mime.attachment_info(part)
        suffix = Path(info["filename"]).suffix.lower()
        mtype = info["mime_type"]
        data = self._attachment_bytes(message_id, part)
        pages = None
        if mtype == "application/pdf" or suffix == ".pdf":
            text, pages = _pdf_text(data, info["filename"])
        elif mtype == "text/html" or suffix in (".html", ".htm"):
            text = mime.html_to_text(mime.decode_text(data, mime.charset(part)))
        elif (
            mtype.startswith("text/")
            or suffix in TEXT_SUFFIXES
            or mtype
            in (
                "application/json",
                "application/xml",
            )
        ):
            text = mime.decode_text(data, mime.charset(part))
        else:
            raise GmailAgentError(
                f"Cannot extract text from {info['filename']!r} ({mtype}). Only PDF and text "
                "files are supported; download it instead."
            )
        text, truncated = _truncate(text, max_chars)
        return {**info, "pages": pages, "text": text, "truncated": truncated}

    # Writing (opt-in) --------------------------------------------------------------------

    def create_draft(
        self,
        to: list[str],
        subject: str | None,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str | Path] | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a draft in the user's Drafts folder. Never sends it."""
        from googleapiclient.http import MediaIoBaseUpload

        msg = EmailMessage()
        thread_id = None
        if reply_to_message_id:
            orig = self._raw_message(reply_to_message_id)
            payload = orig.get("payload", {})
            thread_id = orig.get("threadId")
            orig_id = mime.header(payload, "message-id")
            if orig_id:
                msg["In-Reply-To"] = orig_id
                refs = mime.header(payload, "references")
                msg["References"] = f"{refs} {orig_id}".strip()
            if subject is None:
                orig_subject = mime.header(payload, "subject")
                subject = (
                    orig_subject
                    if orig_subject.lower().startswith("re:")
                    else f"Re: {orig_subject}"
                )
            if not to:
                to = [mime.header(payload, "reply-to") or mime.header(payload, "from")]
        if not to or not any(to):
            raise GmailAgentError("A draft needs at least one recipient (to).")
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject or ""
        msg.set_content(body)
        for item in attachments or []:
            path = Path(item).expanduser()
            if not path.is_file():
                raise GmailAgentError(f"Attachment not found: {path}")
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
            )

        media = MediaIoBaseUpload(
            io.BytesIO(msg.as_bytes()), mimetype="message/rfc822", resumable=False
        )
        draft_body: dict[str, Any] = {"message": {"threadId": thread_id} if thread_id else {}}
        resp = _execute(
            self._users.drafts().create(userId="me", body=draft_body, media_body=media),
            "new draft",
        )
        message = resp.get("message", {})
        return {
            "draft_id": resp.get("id", ""),
            "message_id": message.get("id", ""),
            "thread_id": message.get("threadId", thread_id or ""),
            "to": msg["To"],
            "subject": msg["Subject"],
            "attachments": [Path(a).name for a in attachments or []],
        }


def _pdf_text(data: bytes, name: str) -> tuple[str, int]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError) as e:
        raise GmailAgentError(f"Could not read PDF {name!r}: {e}") from e
    text = "\n\n".join(f"[page {i}]\n{t.strip()}" for i, t in enumerate(pages, 1))
    return text, len(pages)

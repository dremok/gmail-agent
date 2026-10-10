"""The Gmail operations shared by the CLI and the MCP server."""

from __future__ import annotations

import fnmatch
import io
import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from . import auth, compose, config, mime
from .compose import Attachment
from .errors import GmailAgentError, NotFoundError, ScopeError, SetupError
from .files import write_new_file

DEFAULT_BODY_CHARS = 20_000
THREAD_BODY_CHARS = 5_000  # per message; replies tend to quote the whole thread again
DEFAULT_TEXT_CHARS = 50_000
TEXT_SUFFIXES = {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".log", ".ics", ".vcf"}
BATCH_LIMIT = 1000  # Gmail's cap for batchModify and batchDelete

# Shortcuts for common label changes: action -> (labels to add, labels to remove).
MARK_ACTIONS: dict[str, tuple[list[str], list[str]]] = {
    "read": ([], ["UNREAD"]),
    "unread": (["UNREAD"], []),
    "star": (["STARRED"], []),
    "unstar": ([], ["STARRED"]),
    "archive": ([], ["INBOX"]),
    "unarchive": (["INBOX"], []),
}

ACCESS_LABELS = {
    "read": "read access",
    "compose": "send/draft access",
    "modify": "modify access",
    "delete": "full access (permanent delete)",
}


# Error reasons Google uses for quota and rate limits, which Gmail reports as 403 or 429.
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}


def _error_reasons(e: Exception) -> set[str]:
    """The machine-readable reasons in a Google API error body (errors[] and details[])."""
    try:
        error = json.loads(getattr(e, "content", b"") or b"{}")["error"]
    except (ValueError, KeyError, TypeError):
        return set()
    items = [*error.get("errors", []), *error.get("details", [])]
    return {item["reason"] for item in items if isinstance(item, dict) and "reason" in item}


def _http_error(e: Exception, what: str) -> GmailAgentError:
    status = getattr(getattr(e, "resp", None), "status", None)
    reason = getattr(e, "reason", None) or str(e)
    if status == 429 or (status == 403 and _error_reasons(e) & RATE_LIMIT_REASONS):
        return GmailAgentError(f"Gmail rate limit hit during {what}. Wait and retry.")
    if status == 404:
        return NotFoundError(f"{what} not found (Gmail returned 404). Check the id.")
    if status == 403 and "insufficient" in str(reason).lower():
        return ScopeError(
            f"Gmail refused {what}: the login lacks a scope it needs ({reason}). Log in again "
            "with a broader level: gmail-agent login --scope readonly|compose|modify|full"
        )
    if status in (401, 403):
        return SetupError(
            f"Gmail refused {what} ({status}: {reason}). If this is about credentials, "
            "run: gmail-agent login"
        )
    return GmailAgentError(f"Gmail API error during {what} ({status}): {reason}")


def _execute(request, what: str) -> Any:
    from http.client import HTTPException

    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error

    try:
        return request.execute(num_retries=2)
    except HttpError as e:
        raise _http_error(e, what) from e
    except RefreshError as e:  # the access token expired and Google refused a new one
        raise auth.token_rejected(e) from e
    except (OSError, HTTPException, HttpLib2Error, TransportError) as e:
        raise GmailAgentError(f"Network error during {what}: {e}") from e


def _truncate(text: str, limit: int | None) -> tuple[str, bool]:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _chunks(items: list[str], size: int = BATCH_LIMIT) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class Gmail:
    """Wrapper around a googleapiclient Gmail service.

    `scopes` are the scopes the login granted; each call checks them first so a missing scope
    gives a clear message instead of a 403. None skips the check (tests, custom services).
    """

    def __init__(self, service: Any, scopes: list[str] | None = None) -> None:
        self._svc = service
        self._users = service.users()
        self.scopes = scopes
        self._me: str | None = None

    @classmethod
    def connect(cls) -> Gmail:
        creds = auth.load_credentials()
        return cls(auth.build_service(creds), list(creds.scopes or []))

    def _require(self, access: str, what: str) -> None:
        if self.scopes is None or config.allows(self.scopes, access):
            return
        have = ", ".join(config.short_scope(s) for s in self.scopes) or "no scopes"
        level = config.ACCESS[access][1]
        what = what[:1].upper() + what[1:]
        raise ScopeError(
            f"{what} needs {ACCESS_LABELS[access]}, but this login only granted {have}. "
            f"Run: gmail-agent login --scope {level}"
        )

    def _scope_ok(self, access: str) -> bool | None:
        return None if self.scopes is None else config.allows(self.scopes, access)

    def _resource(self, method: str):
        """`messages.attachments.get` -> self._users.messages().attachments().get"""
        *path, name = method.split(".")
        resource = self._users
        for part in path:
            resource = getattr(resource, part)()
        return getattr(resource, name)

    def _call(self, access: str, what: str, method: str, **params) -> Any:
        self._require(access, what)
        return _execute(self._resource(method)(userId="me", **params), what)

    def _write(
        self, access: str, what: str, plans: list[dict], dry_run: bool
    ) -> list[Any] | dict[str, Any]:
        """Run planned write requests, or describe them when `dry_run` is set."""
        if dry_run:
            return {"dry_run": True, "scope_ok": self._scope_ok(access), "requests": plans}
        self._require(access, what)
        results = []
        for done, p in enumerate(plans):
            target = f"{what} ({p['params']['id']})" if "id" in p["params"] else what
            request = self._resource(p["method"].removeprefix("users."))(userId="me", **p["params"])
            try:
                results.append(_execute(request, target))
            except GmailAgentError as e:
                if not done:
                    raise
                # Say what already happened, so the caller neither redoes it nor assumes
                # that nothing changed.
                raise type(e)(
                    f"{e} The {done} request(s) before it succeeded; the other "
                    f"{len(plans) - done - 1} were not sent."
                ) from e
        return results

    @staticmethod
    def _plan(method: str, **params) -> dict[str, Any]:
        return {"method": f"users.{method}", "params": params}

    # Reading -----------------------------------------------------------------------------

    def profile(self) -> dict[str, Any]:
        p = self._call("read", "reading the profile", "getProfile")
        self._me = p.get("emailAddress", "")
        return {
            "email": self._me,
            "messages_total": p.get("messagesTotal", 0),
            "threads_total": p.get("threadsTotal", 0),
        }

    def my_address(self) -> str:
        if self._me is None:
            self.profile()
        return self._me or ""

    def _raw_message(self, message_id: str) -> dict:
        return self._call(
            "read", f"message {message_id}", "messages.get", id=message_id, format="full"
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
            "bcc": mime.header(payload, "bcc"),
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
        resp = self._call(
            "read",
            f"search {query!r}",
            "messages.list",
            q=query,
            maxResults=max(1, min(int(max_results), 500)),
            pageToken=page_token or None,
            includeSpamTrash=include_spam_trash,
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
        self, thread_id: str, max_body_chars: int | None = THREAD_BODY_CHARS
    ) -> dict[str, Any]:
        thread = self._call(
            "read", f"thread {thread_id}", "threads.get", id=thread_id, format="full"
        )
        messages = [self._detail(m, max_body_chars) for m in thread.get("messages", [])]
        return {"id": thread["id"], "message_count": len(messages), "messages": messages}

    def labels(self) -> list[dict[str, Any]]:
        resp = self._call("read", "listing labels", "labels.list")
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
        resp = self._call(
            "read",
            f"attachment {part.get('filename')!r} of message {message_id}",
            "messages.attachments.get",
            messageId=message_id,
            id=body["attachmentId"],
        )
        return mime.b64decode(resp.get("data", ""))

    def _attachments_of(self, msg: dict, skip_inline: bool = False) -> list[Attachment]:
        """The attachments of a fetched message as bytes, for forwarding or redrafting."""
        out = []
        for part in self._select(msg.get("payload", {}), msg["id"], None):
            info = mime.attachment_info(part)
            if skip_inline and info["inline"]:
                continue
            data = self._attachment_bytes(msg["id"], part)
            out.append(Attachment(info["filename"], data, info["mime_type"]))
        return out

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
            resp = self._call(
                "read",
                f"search {q!r}",
                "messages.list",
                q=q,
                maxResults=min(500, max_messages - len(ids)),
                pageToken=token,
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
            or mtype in ("application/json", "application/xml")
        ):
            text = mime.decode_text(data, mime.charset(part))
        else:
            raise GmailAgentError(
                f"Cannot extract text from {info['filename']!r} ({mtype}). Only PDF and text "
                "files are supported; download it instead."
            )
        text, truncated = _truncate(text, max_chars)
        return {**info, "pages": pages, "text": text, "truncated": truncated}

    # Sending and drafts ------------------------------------------------------------------

    def _deliver(
        self,
        msg: EmailMessage,
        thread_id: str | None,
        as_draft: bool,
        dry_run: bool,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        """Send `msg`, save it as a new draft, or replace draft `draft_id` with it."""
        from googleapiclient.http import MediaIoBaseUpload

        message_meta = {"threadId": thread_id} if thread_id else {}
        if draft_id:
            method, params = (
                "drafts.update",
                {"id": draft_id, "body": {"id": draft_id, "message": message_meta}},
            )
        elif as_draft:
            method, params = "drafts.create", {"body": {"message": message_meta}}
        else:
            if not compose.recipient_count(msg):
                raise GmailAgentError("Nothing to send: give at least one To, Cc or Bcc recipient.")
            method, params = "messages.send", {"body": message_meta}
        summary = {
            "to": msg.get("To", ""),
            "cc": msg.get("Cc", ""),
            "bcc": msg.get("Bcc", ""),
            "subject": msg.get("Subject", ""),
            "thread_id": thread_id,
            "attachments": [a.get_filename() for a in msg.iter_attachments()],
        }
        what = "updating a draft" if draft_id else "creating a draft" if as_draft else "sending"
        if dry_run:
            plan = self._plan(method, **params, media_mime_type="message/rfc822")
            return {
                "dry_run": True,
                "scope_ok": self._scope_ok("compose"),
                "requests": [plan],
                **summary,
                "mime": compose.preview(msg),
            }
        self._require("compose", what.capitalize())
        media = MediaIoBaseUpload(
            io.BytesIO(msg.as_bytes()), mimetype="message/rfc822", resumable=True
        )
        resp = _execute(self._resource(method)(userId="me", media_body=media, **params), what)
        if method == "messages.send":
            return {
                "sent": True,
                "id": resp.get("id", ""),
                **summary,
                "thread_id": resp.get("threadId", thread_id),
                "label_ids": resp.get("labelIds", []),
            }
        message = resp.get("message", {})
        return {
            "draft_id": resp.get("id", ""),
            "message_id": message.get("id", ""),
            **summary,
            "thread_id": message.get("threadId", thread_id),
        }

    def send(
        self,
        to: list[str] | None,
        subject: str,
        text: str | None = None,
        html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str | Path] | None = None,
        as_draft: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Send a new message (or save it as a draft)."""
        msg = compose.build(to, subject, text, html, cc, bcc, compose.load_attachments(attachments))
        return self._deliver(msg, None, as_draft, dry_run)

    def reply(
        self,
        message_id: str,
        text: str | None = None,
        html: str | None = None,
        reply_all: bool = False,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str | Path] | None = None,
        quote: bool = True,
        as_draft: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reply in the same thread, with In-Reply-To and References set. Quotes the original
        below the new text unless `quote` is False."""
        orig = self._raw_message(message_id)
        payload = orig.get("payload", {})
        to, auto_cc = compose.reply_recipients(payload, self.my_address(), reply_all)
        if not to and not auto_cc and not cc and not bcc:
            raise GmailAgentError(f"Message {message_id} has no one to reply to.")
        new_text = text if text is not None else (mime.html_to_text(html) if html else "")
        if quote:
            orig_plain, orig_html = mime.body_parts(payload)
            orig_text = orig_plain if orig_plain is not None else mime.html_to_text(orig_html or "")
            date = mime.header(payload, "date") or mime.internal_date(orig)
            sender = mime.header(payload, "from")
            new_text = f"{new_text}\n\n{compose.quote_text(orig_text, date, sender)}"
            if html:
                html = html + compose.quote_html(orig_html, orig_text, date, sender)
        msg = compose.build(
            to,
            compose.reply_subject(mime.header(payload, "subject")),
            new_text,
            html,
            auto_cc + (cc or []),
            bcc,
            compose.load_attachments(attachments),
            compose.threading_headers(payload),
        )
        return self._deliver(msg, orig.get("threadId"), as_draft, dry_run)

    def forward(
        self,
        message_id: str,
        to: list[str] | None,
        text: str | None = None,
        html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str | Path] | None = None,
        include_attachments: bool = True,
        as_draft: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Forward a message with its attachments (images embedded in its HTML are left out)."""
        orig = self._raw_message(message_id)
        payload = orig.get("payload", {})
        orig_plain, orig_html = mime.body_parts(payload)
        orig_text = orig_plain if orig_plain is not None else mime.html_to_text(orig_html or "")
        note = text if text is not None else (mime.html_to_text(html) if html else "")
        new_text = f"{note}\n\n{compose.forward_block_text(payload, orig_text)}".lstrip("\n")
        new_html = None
        if html or orig_html:
            lead = html or compose.text_to_html(note)
            original = orig_html or compose.text_to_html(orig_text)
            new_html = f"{lead}<br><br>{compose.forward_block_html(payload, original)}"
        atts = self._attachments_of(orig, skip_inline=True) if include_attachments else []
        atts += compose.load_attachments(attachments)
        msg = compose.build(
            to,
            compose.forward_subject(mime.header(payload, "subject")),
            new_text,
            new_html,
            cc,
            bcc,
            atts,
        )
        return self._deliver(msg, None, as_draft, dry_run)

    def list_drafts(
        self, max_results: int = 20, page_token: str | None = None, query: str | None = None
    ) -> dict[str, Any]:
        resp = self._call(
            "read",
            "listing drafts",
            "drafts.list",
            maxResults=max(1, min(int(max_results), 500)),
            pageToken=page_token or None,
            q=query or None,
        )
        drafts = []
        for d in resp.get("drafts", []):
            full = self._raw_draft(d["id"])
            drafts.append({"draft_id": full["id"], **self._summary(full["message"])})
        return {"drafts": drafts, "next_page_token": resp.get("nextPageToken")}

    def _raw_draft(self, draft_id: str) -> dict:
        return self._call("read", f"draft {draft_id}", "drafts.get", id=draft_id, format="full")

    def get_draft(
        self, draft_id: str, max_body_chars: int | None = DEFAULT_BODY_CHARS
    ) -> dict[str, Any]:
        d = self._raw_draft(draft_id)
        return {"draft_id": d["id"], **self._detail(d["message"], max_body_chars)}

    def update_draft(
        self,
        draft_id: str,
        to: list[str] | None = None,
        subject: str | None = None,
        text: str | None = None,
        html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str | Path] | None = None,
        keep_attachments: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Change a draft. Fields left as None keep their current value; the body is replaced
        only if `text` or `html` is given. New attachments are added to the existing ones
        unless `keep_attachments` is False. The thread and reply headers are kept."""
        current = self._raw_draft(draft_id)
        msg_now = current["message"]
        payload = msg_now.get("payload", {})
        old_text, old_html = mime.body_parts(payload)
        if text is None and html is None:
            text, html = old_text, old_html

        def keep(new: list[str] | None, name: str) -> list[str] | None:
            return new if new is not None else ([v] if (v := mime.header(payload, name)) else None)

        atts = self._attachments_of(msg_now) if keep_attachments else []
        atts += compose.load_attachments(attachments)
        headers = {
            name: mime.header(payload, name.lower()) for name in ("In-Reply-To", "References")
        }
        msg = compose.build(
            keep(to, "to"),
            subject if subject is not None else mime.header(payload, "subject"),
            text,
            html,
            keep(cc, "cc"),
            keep(bcc, "bcc"),
            atts,
            headers,
        )
        return self._deliver(msg, msg_now.get("threadId"), True, dry_run, draft_id=draft_id)

    def send_draft(self, draft_id: str, dry_run: bool = False) -> dict[str, Any]:
        plan = self._plan("drafts.send", body={"id": draft_id})
        result = self._write("compose", f"sending draft {draft_id}", [plan], dry_run)
        if isinstance(result, dict):
            return result
        resp = result[0]
        return {
            "sent": True,
            "draft_id": draft_id,
            "id": resp.get("id", ""),
            "thread_id": resp.get("threadId", ""),
            "label_ids": resp.get("labelIds", []),
        }

    def delete_draft(self, draft_id: str, dry_run: bool = False) -> dict[str, Any]:
        plan = self._plan("drafts.delete", id=draft_id)
        result = self._write("compose", f"deleting draft {draft_id}", [plan], dry_run)
        return result if isinstance(result, dict) else {"deleted": True, "draft_id": draft_id}

    # Labels and message state -------------------------------------------------------------

    def _label_ids(self, labels: list[str]) -> list[str]:
        """Label ids for names or ids (case-insensitive names; system ids like STARRED work)."""
        if not labels:
            return []
        known = self.labels()
        by_key = {lb["id"]: lb["id"] for lb in known}
        by_key |= {lb["name"].lower(): lb["id"] for lb in known}
        by_key |= {k: k for k in ("UNREAD", "STARRED", "INBOX", "IMPORTANT", "SPAM", "TRASH")}
        out = []
        for label in labels:
            label_id = by_key.get(label) or by_key.get(label.lower())
            if not label_id:
                raise NotFoundError(
                    f"No label named {label!r}. Create it with: gmail-agent label create {label!r}"
                )
            out.append(label_id)
        return out

    def _label(self, label: str) -> dict[str, Any]:
        label_id = self._label_ids([label])[0]
        return next(lb for lb in self.labels() if lb["id"] == label_id)

    def create_label(self, name: str, dry_run: bool = False) -> dict[str, Any]:
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        result = self._write(
            "modify", f"creating label {name!r}", [self._plan("labels.create", body=body)], dry_run
        )
        if isinstance(result, dict):
            return result
        return {"created": True, "id": result[0].get("id", ""), "name": result[0].get("name", name)}

    def rename_label(self, label: str, new_name: str, dry_run: bool = False) -> dict[str, Any]:
        current = self._label(label)
        if current["type"] == "system":
            raise GmailAgentError(f"{current['name']} is a system label and cannot be renamed.")
        plan = self._plan("labels.patch", id=current["id"], body={"name": new_name})
        result = self._write("modify", f"renaming label {label!r}", [plan], dry_run)
        if isinstance(result, dict):
            return result
        return {"renamed": True, "id": current["id"], "old_name": current["name"], "name": new_name}

    def delete_label(self, label: str, dry_run: bool = False) -> dict[str, Any]:
        current = self._label(label)
        if current["type"] == "system":
            raise GmailAgentError(f"{current['name']} is a system label and cannot be deleted.")
        plan = self._plan("labels.delete", id=current["id"])
        result = self._write("modify", f"deleting label {label!r}", [plan], dry_run)
        if isinstance(result, dict):
            return result
        return {"deleted": True, "id": current["id"], "name": current["name"]}

    def modify_labels(
        self,
        ids: list[str],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        threads: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add and remove labels on messages (or whole threads with `threads=True`)."""
        if not ids:
            raise GmailAgentError("Give at least one message or thread id.")
        add_ids, remove_ids = self._label_ids(add or []), self._label_ids(remove or [])
        if not add_ids and not remove_ids:
            raise GmailAgentError("Nothing to change: give labels to add or remove.")
        change = {"addLabelIds": add_ids, "removeLabelIds": remove_ids}
        if threads:
            plans = [self._plan("threads.modify", id=i, body=change) for i in ids]
        elif len(ids) == 1:
            plans = [self._plan("messages.modify", id=ids[0], body=change)]
        else:
            plans = [
                self._plan("messages.batchModify", body={"ids": chunk, **change})
                for chunk in _chunks(ids)
            ]
        result = self._write("modify", "changing labels", plans, dry_run)
        if isinstance(result, dict):
            return result
        return {
            "target": "threads" if threads else "messages",
            "ids": ids,
            "added": add_ids,
            "removed": remove_ids,
        }

    def mark(
        self, ids: list[str], action: str, threads: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Shortcuts: read, unread, star, unstar, archive, unarchive."""
        if action not in MARK_ACTIONS:
            raise GmailAgentError(
                f"Unknown action {action!r}. Use one of: {', '.join(MARK_ACTIONS)}"
            )
        add, remove = MARK_ACTIONS[action]
        result = self.modify_labels(ids, add, remove, threads, dry_run)
        return result if result.get("dry_run") else {"action": action, **result}

    def trash(
        self, ids: list[str], threads: bool = False, undo: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Move to Trash (Gmail empties it after 30 days), or back out with `undo=True`."""
        if not ids:
            raise GmailAgentError("Give at least one message or thread id.")
        verb = "untrash" if undo else "trash"
        kind = "threads" if threads else "messages"
        plans = [self._plan(f"{kind}.{verb}", id=i) for i in ids]
        result = self._write("modify", f"{verb} {kind}", plans, dry_run)
        if isinstance(result, dict):
            return result
        return {"action": verb, "target": kind, "ids": ids}

    def delete_permanently(
        self, ids: list[str], threads: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Delete immediately, skipping Trash. Cannot be undone. Needs the full scope."""
        if not ids:
            raise GmailAgentError("Give at least one message or thread id.")
        if threads:
            plans = [self._plan("threads.delete", id=i) for i in ids]
        else:
            plans = [self._plan("messages.batchDelete", body={"ids": c}) for c in _chunks(ids)]
        result = self._write("delete", "permanently deleting mail", plans, dry_run)
        if isinstance(result, dict):
            return result
        return {"action": "delete", "target": "threads" if threads else "messages", "ids": ids}


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

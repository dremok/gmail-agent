"""MCP server over stdio. Same operations as the CLI, exposed as tools."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import config
from .errors import GmailAgentError
from .gmail import DEFAULT_BODY_CHARS, DEFAULT_TEXT_CHARS, Gmail

INSTRUCTIONS = """\
Read access to the user's Gmail. Queries use Gmail search syntax (from:, to:, subject:,
has:attachment, filename:pdf, after:2026/01/31, newer_than:7d, label:, in:anywhere).
Typical flow: search_messages -> get_message or list_attachments -> download_attachments.
Attachments are picked by filename or part_id (e.g. "1.2"); attachment_id values can change
between calls. Downloads never overwrite: a taken name gets a _1, _2 suffix, and the returned
paths are the real ones. If a tool returns a setup error, tell the user the exact command it
mentions (usually `gmail-agent login`); do not try to work around it."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Downloads write local files but change nothing in Gmail.
WRITES_LOCAL = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
DRAFT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def build_server(
    allow_drafts: bool = False,
    connect: Callable[[bool], Gmail] | None = None,
) -> MCPServer:
    """`connect(needs_drafts)` returns a Gmail client; defaults to the saved login. The
    connection is made on the first tool call, so the server starts even before login."""
    connect = connect or (lambda needs_drafts: Gmail.connect(allow_drafts=needs_drafts))
    clients: dict[bool, Gmail] = {}

    def gmail(needs_drafts: bool = False) -> Gmail:
        if needs_drafts not in clients:
            clients[needs_drafts] = connect(needs_drafts)
        return clients[needs_drafts]

    mcp = MCPServer("gmail-agent", instructions=INSTRUCTIONS)

    def tool(annotations: ToolAnnotations):
        """Register a tool whose GmailAgentErrors reach the model as readable tool errors
        (the SDK hides the text of any other exception)."""

        def register(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except GmailAgentError as e:
                    raise ToolError(str(e)) from e

            return mcp.tool(annotations=annotations)(wrapper)

        return register

    @tool(READ_ONLY)
    def account_status() -> dict[str, Any]:
        """The logged-in Gmail address and message counts. Use it to check setup."""
        return {
            **gmail().profile(),
            "config_dir": str(config.config_dir()),
            "drafts_enabled": allow_drafts,
        }

    @tool(READ_ONLY)
    def search_messages(
        query: str,
        max_results: int = 20,
        page_token: str | None = None,
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        """Search messages with a Gmail query, newest first. Returns id, thread_id, date, from,
        to, subject, snippet, labels and attachments for each message, plus next_page_token
        when there are more results (pass it back as page_token)."""
        return gmail().search(query, max_results, page_token, include_spam_trash)

    @tool(READ_ONLY)
    def get_message(message_id: str, max_body_chars: int = DEFAULT_BODY_CHARS) -> dict[str, Any]:
        """One message with headers, plain-text body (HTML is converted) and attachment list.
        body_truncated is true when the body was cut at max_body_chars (0 = no limit)."""
        return gmail().get_message(message_id, max_body_chars or None)

    @tool(READ_ONLY)
    def get_thread(thread_id: str, max_body_chars: int = DEFAULT_BODY_CHARS) -> dict[str, Any]:
        """Every message in a thread, oldest first, each like get_message."""
        return gmail().get_thread(thread_id, max_body_chars or None)

    @tool(READ_ONLY)
    def list_labels() -> dict[str, Any]:
        """All labels (system and user) with their ids, for use in label: queries."""
        return {"labels": gmail().labels()}

    @tool(READ_ONLY)
    def list_attachments(message_id: str) -> dict[str, Any]:
        """Attachments of one message: filename, mime_type, size, part_id, attachment_id, and
        inline (true for images embedded in the HTML body, such as logos)."""
        return {"message_id": message_id, "attachments": gmail().attachments(message_id)}

    @tool(WRITES_LOCAL)
    def download_attachments(
        message_id: str,
        out_dir: str,
        attachments: list[str] | None = None,
        skip_inline: bool = False,
    ) -> dict[str, Any]:
        """Save a message's attachments into out_dir (created if missing; ~ is expanded).
        attachments: filenames or part ids to save; omit to save all. Existing files are
        never overwritten. Returns the absolute path of every saved file."""
        return {"saved": gmail().download(message_id, out_dir, attachments, skip_inline)}

    @tool(WRITES_LOCAL)
    def download_matching_attachments(
        query: str,
        out_dir: str,
        filename_glob: str | None = None,
        max_messages: int = 50,
        skip_inline: bool = False,
    ) -> dict[str, Any]:
        """Save every attachment from messages matching a Gmail query (has:attachment is added
        if missing). filename_glob filters names, e.g. "*.pdf". more_messages is true when
        more than max_messages matched."""
        return gmail().download_matching(query, out_dir, filename_glob, max_messages, skip_inline)

    @tool(READ_ONLY)
    def read_attachment_text(
        message_id: str, attachment: str, max_chars: int = DEFAULT_TEXT_CHARS
    ) -> dict[str, Any]:
        """Extract the text of a PDF or text attachment (picked by filename or part id) without
        saving it. Scanned PDFs have no text layer and come back empty."""
        return gmail().read_attachment_text(message_id, attachment, max_chars or None)

    if allow_drafts:

        @tool(DRAFT)
        def create_draft(
            to: list[str],
            body: str,
            subject: str | None = None,
            cc: list[str] | None = None,
            bcc: list[str] | None = None,
            attachment_paths: list[str] | None = None,
            reply_to_message_id: str | None = None,
        ) -> dict[str, Any]:
            """Create a draft in the user's Drafts folder. It is NOT sent; the user reviews
            and sends it in Gmail. attachment_paths are local files. With
            reply_to_message_id the draft joins that thread and the subject defaults to
            "Re: ..."; an empty `to` then replies to the original sender."""
            return gmail(needs_drafts=True).create_draft(
                to, subject, body, cc, bcc, attachment_paths, reply_to_message_id
            )

    return mcp

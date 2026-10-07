"""Command line interface. Human-readable output by default, `--json` for scripts and agents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__, auth, config
from .errors import GmailAgentError, SetupError
from .gmail import DEFAULT_BODY_CHARS, DEFAULT_TEXT_CHARS, MARK_ACTIONS, Gmail

EPILOG = """\
examples:
  gmail-agent search "from:billing@example.com newer_than:30d" -n 5
  gmail-agent download 18c2f0a1b2c3d4e5 -o ~/Downloads/invoices --name invoice.pdf
  gmail-agent fetch "subject:invoice after:2026/01/01" -o ./invoices --match "*.pdf"
  gmail-agent send --to alice@example.com --subject Hi --body "Hello" --attach report.pdf
  gmail-agent reply 18c2f0a1b2c3d4e5 --all --body "Thanks!" --dry-run
  gmail-agent message 18c2f0a1b2c3d4e5 --json

Every write command takes --dry-run (show the exact request, change nothing).
Queries use Gmail search syntax: https://support.google.com/mail/answer/7190
"""

LEVEL_HELP = {
    "readonly": "read and download only",
    "compose": "read, send, drafts",
    "modify": "read, send, drafts, labels, archive, trash (default)",
    "full": "everything, including permanent delete",
}


# Output -----------------------------------------------------------------------------------


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _attachment_lines(atts: list[dict], indent: str = "    ") -> list[str]:
    lines = []
    for a in atts:
        meta = f"{a['mime_type']}, {_size(a['size'])}" + (", inline" if a.get("inline") else "")
        lines.append(f"{indent}[{a['part_id']}] {a['filename']}  ({meta})")
    return lines


def _print_summary(m: dict) -> None:
    prefix = f"draft {m['draft_id']}  " if m.get("draft_id") else ""
    print(f"{prefix}{m['id']}  {m['date']}  {m['from'] or m['to']}")
    print(f"    {m['subject'] or '(no subject)'}")
    for line in _attachment_lines(m["attachments"]):
        print(line)


def _print_detail(m: dict) -> None:
    if m.get("draft_id"):
        print(f"{'Draft:':<10} {m['draft_id']}")
    for key in ("from", "to", "cc", "bcc", "reply_to", "subject"):
        if m.get(key):
            print(f"{key.replace('_', '-').title() + ':':<10} {m[key]}")
    print(f"{'Date:':<10} {m['date']}")
    print(f"{'Id:':<10} {m['id']}  (thread {m['thread_id']})")
    if m["labels"]:
        print(f"{'Labels:':<10} {', '.join(m['labels'])}")
    if m["attachments"]:
        print("Attachments:")
        for line in _attachment_lines(m["attachments"], "  "):
            print(line)
    print()
    print(m["body"] or "(no text body)")
    if m["body_truncated"]:
        print("\n[body truncated; use --max-chars 0 for all of it]")


def _print_saved(saved: list[dict]) -> None:
    for s in saved:
        print(s["path"])
    if not saved:
        print("No attachments saved.", file=sys.stderr)


def _print_write(res: dict) -> None:
    if res.get("dry_run"):
        print("Dry run, nothing was changed.")
        if res.get("scope_ok") is False:
            print("Note: the current login lacks the scope for this; the real call would fail.")
        for req in res["requests"]:
            print(json.dumps(req, indent=2, ensure_ascii=False))
        if "mime" in res:
            print("\n--- MIME message ---")
            print(res["mime"])
    elif res.get("sent"):
        print(f"Sent message {res['id']} (thread {res['thread_id']}).")
    elif "draft_id" in res and "message_id" in res:
        print(f"Draft {res['draft_id']} saved (message {res['message_id']}). It was not sent.")
    elif res.get("deleted") and "draft_id" in res:
        print(f"Deleted draft {res['draft_id']}.")
    elif "action" in res:
        print(f"{res['action']}: {len(res['ids'])} {res['target']}")
    elif "added" in res:
        print(
            f"Labels on {len(res['ids'])} {res['target']}: "
            f"+{','.join(res['added']) or '-'} -{','.join(res['removed']) or '-'}"
        )
    else:
        print(json.dumps(res, ensure_ascii=False))


# Helpers ----------------------------------------------------------------------------------


def _read_arg(value: str | None, file: str | None) -> str | None:
    if file:
        return sys.stdin.read() if file == "-" else Path(file).read_text()
    return value


def _bodies(args) -> tuple[str | None, str | None]:
    return _read_arg(args.body, args.body_file), _read_arg(args.html, args.html_file)


def _limit(n: int) -> int | None:
    return None if n <= 0 else n


# Commands ---------------------------------------------------------------------------------


def cmd_login(args) -> Any:
    creds = auth.login(level=args.scope, open_browser=not args.no_browser)
    granted = auth.granted_scopes(creds)
    email = Gmail(auth.build_service(creds), granted).profile()["email"]
    level = config.level_of(granted)
    result = {
        "logged_in": True,
        "email": email,
        "level": level,
        "scopes": granted,
        "token": str(config.token_path()),
    }
    if not args.json:
        print(f"Logged in as {email} with {level} access. Token saved to {config.token_path()}")
    if level != args.scope:
        print(
            f"Warning: you asked for {args.scope} but Google granted {level}. Some boxes on the "
            "consent screen were probably unticked. Run login again to fix it.",
            file=sys.stderr,
        )
    return result


def cmd_logout(args) -> Any:
    removed = auth.logout()
    if not args.json:
        print(f"Removed {config.token_path()}" if removed else "Not logged in.")
        print("To revoke access entirely: https://myaccount.google.com/permissions")
    return {"removed": removed, "token": str(config.token_path())}


class _StatusFailed(Exception):
    def __init__(self, status: dict) -> None:
        self.status = status


def cmd_status(args) -> Any:
    status: dict[str, Any] = {
        "version": __version__,
        "config_dir": str(config.config_dir()),
        "credentials_file": config.credentials_path().exists(),
        "token_file": config.token_path().exists(),
        "logged_in": False,
        "email": None,
        "level": None,
        "scopes": [],
        "problem": None,
    }
    try:
        creds = auth.load_credentials()
        status["scopes"] = list(creds.scopes or [])
        status["level"] = config.level_of(status["scopes"])
        status["email"] = Gmail(auth.build_service(creds), status["scopes"]).profile()["email"]
        status["logged_in"] = True
    except GmailAgentError as e:
        status["problem"] = str(e)
    if not args.json:
        yes = {True: "yes", False: "no"}
        print(f"gmail-agent {status['version']}")
        print(f"config dir:   {status['config_dir']}")
        print(f"OAuth client: {yes[status['credentials_file']]} ({config.credentials_path()})")
        print(f"token:        {yes[status['token_file']]} ({config.token_path()})")
        print(f"logged in:    {status['email'] or 'no'}")
        if status["level"]:
            print(f"access:       {status['level']} ({LEVEL_HELP.get(status['level'], '')})")
        if status["problem"]:
            print(f"\nproblem: {status['problem']}")
    if not status["logged_in"]:
        # Exit non-zero so `gmail-agent status` works as a setup check in scripts.
        raise _StatusFailed(status)
    return status


def cmd_search(args) -> Any:
    res = Gmail.connect().search(
        args.query, args.max_results, args.page_token, args.include_spam_trash
    )
    if not args.json:
        for m in res["messages"]:
            _print_summary(m)
        if not res["messages"]:
            print("No messages found.", file=sys.stderr)
        if res["next_page_token"]:
            print(f"\nMore results: --page-token {res['next_page_token']}")
    return res


def cmd_message(args) -> Any:
    m = Gmail.connect().get_message(args.message_id, _limit(args.max_chars))
    if not args.json:
        _print_detail(m)
    return m


def cmd_thread(args) -> Any:
    t = Gmail.connect().get_thread(args.thread_id, _limit(args.max_chars))
    if not args.json:
        for i, m in enumerate(t["messages"]):
            if i:
                print("\n" + "-" * 72 + "\n")
            _print_detail(m)
    return t


def cmd_attachments(args) -> Any:
    atts = Gmail.connect().attachments(args.message_id)
    if not args.json:
        for line in _attachment_lines(atts, ""):
            print(line)
        if not atts:
            print("No attachments.", file=sys.stderr)
    return {"message_id": args.message_id, "attachments": atts}


def cmd_download(args) -> Any:
    saved = Gmail.connect().download(args.message_id, args.out_dir, args.name, args.skip_inline)
    if not args.json:
        _print_saved(saved)
    return {"saved": saved}


def cmd_fetch(args) -> Any:
    res = Gmail.connect().download_matching(
        args.query, args.out_dir, args.match, args.max_messages, args.skip_inline
    )
    if not args.json:
        _print_saved(res["saved"])
        print(f"Scanned {res['messages_scanned']} messages.", file=sys.stderr)
        if res["more_messages"]:
            print(
                f"More messages match; raise --max-messages (now {args.max_messages}).",
                file=sys.stderr,
            )
    return res


def cmd_read(args) -> Any:
    res = Gmail.connect().read_attachment_text(
        args.message_id, args.attachment, _limit(args.max_chars)
    )
    if not args.json:
        print(res["text"])
        if res["truncated"]:
            print("\n[text truncated; use --max-chars 0 for all of it]", file=sys.stderr)
    return res


def _written(args, res: dict) -> dict:
    if not args.json:
        _print_write(res)
    return res


def cmd_send(args) -> Any:
    text, html = _bodies(args)
    res = Gmail.connect().send(
        args.to,
        args.subject or "",
        text,
        html,
        args.cc,
        args.bcc,
        args.attach,
        as_draft=args.draft,
        dry_run=args.dry_run,
    )
    return _written(args, res)


def cmd_reply(args) -> Any:
    text, html = _bodies(args)
    res = Gmail.connect().reply(
        args.message_id,
        text,
        html,
        reply_all=args.all,
        cc=args.cc,
        bcc=args.bcc,
        attachments=args.attach,
        quote=not args.no_quote,
        as_draft=args.draft,
        dry_run=args.dry_run,
    )
    return _written(args, res)


def cmd_forward(args) -> Any:
    text, html = _bodies(args)
    res = Gmail.connect().forward(
        args.message_id,
        args.to,
        text,
        html,
        args.cc,
        args.bcc,
        args.attach,
        include_attachments=not args.no_attachments,
        as_draft=args.draft,
        dry_run=args.dry_run,
    )
    return _written(args, res)


def cmd_draft_list(args) -> Any:
    res = Gmail.connect().list_drafts(args.max_results, args.page_token, args.query)
    if not args.json:
        for d in res["drafts"]:
            _print_summary(d)
        if not res["drafts"]:
            print("No drafts.", file=sys.stderr)
    return res


def cmd_draft_get(args) -> Any:
    d = Gmail.connect().get_draft(args.draft_id, _limit(args.max_chars))
    if not args.json:
        _print_detail(d)
    return d


def cmd_draft_create(args) -> Any:
    args.draft = True
    return cmd_send(args)


def cmd_draft_update(args) -> Any:
    text, html = _bodies(args)
    res = Gmail.connect().update_draft(
        args.draft_id,
        args.to,
        args.subject,
        text,
        html,
        args.cc,
        args.bcc,
        args.attach,
        keep_attachments=not args.drop_attachments,
        dry_run=args.dry_run,
    )
    return _written(args, res)


def cmd_draft_send(args) -> Any:
    return _written(args, Gmail.connect().send_draft(args.draft_id, dry_run=args.dry_run))


def cmd_draft_delete(args) -> Any:
    return _written(args, Gmail.connect().delete_draft(args.draft_id, dry_run=args.dry_run))


def cmd_labels(args) -> Any:
    labels = Gmail.connect().labels()
    if not args.json:
        for lb in labels:
            print(f"{lb['name']:<40} {lb['id']}  ({lb['type']})")
    return {"labels": labels}


def cmd_label_create(args) -> Any:
    res = Gmail.connect().create_label(args.name, dry_run=args.dry_run)
    if not args.json and not res.get("dry_run"):
        print(f"Created label {res['name']} ({res['id']}).")
        return res
    return _written(args, res)


def cmd_label_rename(args) -> Any:
    res = Gmail.connect().rename_label(args.label, args.new_name, dry_run=args.dry_run)
    if not args.json and not res.get("dry_run"):
        print(f"Renamed {res['old_name']} to {res['name']}.")
        return res
    return _written(args, res)


def cmd_label_delete(args) -> Any:
    res = Gmail.connect().delete_label(args.label, dry_run=args.dry_run)
    if not args.json and not res.get("dry_run"):
        print(f"Deleted label {res['name']}. Messages keep their other labels.")
        return res
    return _written(args, res)


def cmd_label_add(args) -> Any:
    res = Gmail.connect().modify_labels(
        args.ids, add=[args.label], threads=args.thread, dry_run=args.dry_run
    )
    return _written(args, res)


def cmd_label_remove(args) -> Any:
    res = Gmail.connect().modify_labels(
        args.ids, remove=[args.label], threads=args.thread, dry_run=args.dry_run
    )
    return _written(args, res)


def cmd_mark(args) -> Any:
    res = Gmail.connect().mark(args.ids, args.action, threads=args.thread, dry_run=args.dry_run)
    return _written(args, res)


def cmd_trash(args) -> Any:
    res = Gmail.connect().trash(args.ids, threads=args.thread, undo=False, dry_run=args.dry_run)
    return _written(args, res)


def cmd_untrash(args) -> Any:
    res = Gmail.connect().trash(args.ids, threads=args.thread, undo=True, dry_run=args.dry_run)
    return _written(args, res)


def cmd_delete(args) -> Any:
    if not args.permanent and not args.dry_run:
        raise GmailAgentError(
            "delete skips the Trash and cannot be undone. Add --permanent to confirm, or use "
            "`gmail-agent trash` instead."
        )
    res = Gmail.connect().delete_permanently(args.ids, threads=args.thread, dry_run=args.dry_run)
    return _written(args, res)


def cmd_mcp(args) -> Any:
    from .server import build_server

    build_server(read_only=args.read_only, allow_delete=args.allow_delete).run()
    return None


# Parser -----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON (for scripts and agents)")
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument(
        "--dry-run", action="store_true", help="show the exact request (and MIME), change nothing"
    )

    p = argparse.ArgumentParser(
        prog="gmail-agent",
        description="Gmail for humans and AI agents: search, read, download attachments, "
        "send, reply, forward, drafts, labels, trash.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"gmail-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(parent, name: str, func, help: str, write: bool = False, **kw):
        parents = [common, dry] if write else [common]
        sp = parent.add_parser(name, parents=parents, help=help, description=help, **kw)
        sp.set_defaults(func=func)
        return sp

    def ids_arg(sp, what="message"):
        sp.add_argument("ids", nargs="+", metavar="ID", help=f"{what} id(s)")
        sp.add_argument("--thread", action="store_true", help="the ids are thread ids")

    def body_args(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--body", help="plain-text body")
        g.add_argument("--body-file", metavar="PATH", help="read the plain body from a file, or -")
        h = sp.add_mutually_exclusive_group()
        h.add_argument("--html", help="HTML body (sent with a plain-text alternative)")
        h.add_argument("--html-file", metavar="PATH", help="read the HTML body from a file, or -")

    def recipients(sp, to_required: bool = False):
        sp.add_argument("--to", action="append", required=to_required, help="repeatable")
        sp.add_argument("--cc", action="append", help="repeatable")
        sp.add_argument("--bcc", action="append", help="repeatable")

    def attach(sp):
        sp.add_argument("--attach", action="append", metavar="PATH", help="file; repeatable")

    def as_draft(sp):
        sp.add_argument("--draft", action="store_true", help="save as a draft instead of sending")

    # Setup
    sp = add(sub, "login", cmd_login, "log in with your browser and choose the access level")
    sp.add_argument(
        "--scope",
        "--scopes",
        choices=list(config.LEVELS),
        default=config.DEFAULT_LEVEL,
        help="; ".join(f"{k}: {v}" for k, v in LEVEL_HELP.items()),
    )
    sp.add_argument("--no-browser", action="store_true", help="print the login URL instead")
    add(sub, "logout", cmd_logout, "delete the saved token")
    add(sub, "status", cmd_status, "check setup and show account and access (exit 1 if not ready)")

    # Reading
    sp = add(sub, "search", cmd_search, "search messages with a Gmail query")
    sp.add_argument("query", help='Gmail query, e.g. "from:alice@example.com has:attachment"')
    sp.add_argument("-n", "--max-results", type=int, default=20, help="page size (default 20)")
    sp.add_argument("--page-token", help="token from a previous page")
    sp.add_argument("--include-spam-trash", action="store_true")

    sp = add(sub, "message", cmd_message, "show one message: headers, text body, attachments")
    sp.add_argument("message_id")
    sp.add_argument("--max-chars", type=int, default=0, help="truncate the body (0 = no limit)")

    sp = add(sub, "thread", cmd_thread, "show every message in a thread")
    sp.add_argument("thread_id")
    sp.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_BODY_CHARS,
        help=f"truncate each body (default {DEFAULT_BODY_CHARS}, 0 = no limit)",
    )

    # Attachments
    sp = add(sub, "attachments", cmd_attachments, "list a message's attachments")
    sp.add_argument("message_id")

    skip_help = "skip images embedded in the HTML body (logos, signatures)"
    sp = add(sub, "download", cmd_download, "save a message's attachments to a folder")
    sp.add_argument("message_id")
    sp.add_argument("-o", "--out-dir", default=".", help="target folder (default: current)")
    sp.add_argument(
        "--name",
        action="append",
        metavar="FILENAME|PART_ID",
        help="only this attachment; repeat for more (default: all)",
    )
    sp.add_argument("--skip-inline", action="store_true", help=skip_help)

    sp = add(sub, "fetch", cmd_fetch, "save all attachments from messages matching a query")
    sp.add_argument("query")
    sp.add_argument("-o", "--out-dir", default=".", help="target folder (default: current)")
    sp.add_argument("--match", metavar="GLOB", help='only filenames matching, e.g. "*.pdf"')
    sp.add_argument("--max-messages", type=int, default=50, help="default 50")
    sp.add_argument("--skip-inline", action="store_true", help=skip_help)

    sp = add(sub, "read", cmd_read, "print the text of a PDF or text attachment")
    sp.add_argument("message_id")
    sp.add_argument("attachment", help="filename or part id")
    sp.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_TEXT_CHARS,
        help=f"default {DEFAULT_TEXT_CHARS}, 0 = no limit",
    )

    # Sending
    sp = add(sub, "send", cmd_send, "send a new message", write=True)
    recipients(sp)
    sp.add_argument("--subject")
    body_args(sp)
    attach(sp)
    as_draft(sp)

    sp = add(sub, "reply", cmd_reply, "reply in the thread (quotes the original)", write=True)
    sp.add_argument("message_id")
    sp.add_argument("--all", action="store_true", help="reply to all recipients")
    body_args(sp)
    sp.add_argument("--cc", action="append", help="extra Cc; repeatable")
    sp.add_argument("--bcc", action="append", help="repeatable")
    attach(sp)
    sp.add_argument("--no-quote", action="store_true", help="do not quote the original")
    as_draft(sp)

    sp = add(sub, "forward", cmd_forward, "forward a message with its attachments", write=True)
    sp.add_argument("message_id")
    recipients(sp, to_required=False)
    body_args(sp)
    attach(sp)
    sp.add_argument("--no-attachments", action="store_true", help="leave out the attachments")
    as_draft(sp)

    # Drafts
    dp = sub.add_parser("draft", help="list, show, create, update, send and delete drafts")
    dsub = dp.add_subparsers(dest="draft_command", required=True, metavar="ACTION")
    sp = add(dsub, "list", cmd_draft_list, "list drafts")
    sp.add_argument("-n", "--max-results", type=int, default=20)
    sp.add_argument("--page-token")
    sp.add_argument("--query", help="Gmail query to filter drafts")
    sp = add(dsub, "get", cmd_draft_get, "show a draft")
    sp.add_argument("draft_id")
    sp.add_argument("--max-chars", type=int, default=0, help="truncate the body (0 = no limit)")
    sp = add(dsub, "create", cmd_draft_create, "create a draft", write=True)
    recipients(sp)
    sp.add_argument("--subject")
    body_args(sp)
    attach(sp)
    sp = add(dsub, "update", cmd_draft_update, "change a draft; unset fields stay", write=True)
    sp.add_argument("draft_id")
    recipients(sp)
    sp.add_argument("--subject")
    body_args(sp)
    attach(sp)
    sp.add_argument(
        "--drop-attachments", action="store_true", help="remove the draft's current attachments"
    )
    sp = add(dsub, "send", cmd_draft_send, "send a draft", write=True)
    sp.add_argument("draft_id")
    sp = add(dsub, "delete", cmd_draft_delete, "delete a draft", write=True)
    sp.add_argument("draft_id")

    # Labels
    add(sub, "labels", cmd_labels, "list labels (same as `label list`)")
    lp = sub.add_parser("label", help="list, create, rename, delete, add and remove labels")
    lsub = lp.add_subparsers(dest="label_command", required=True, metavar="ACTION")
    add(lsub, "list", cmd_labels, "list labels")
    sp = add(lsub, "create", cmd_label_create, "create a label", write=True)
    sp.add_argument("name", help='use "/" for nesting, e.g. "Receipts/2026"')
    sp = add(lsub, "rename", cmd_label_rename, "rename a label", write=True)
    sp.add_argument("label", help="name or id")
    sp.add_argument("new_name")
    sp = add(lsub, "delete", cmd_label_delete, "delete a label (messages are kept)", write=True)
    sp.add_argument("label", help="name or id")
    sp = add(lsub, "add", cmd_label_add, "add a label to messages or threads", write=True)
    sp.add_argument("label", help="name or id")
    ids_arg(sp)
    sp = add(lsub, "remove", cmd_label_remove, "remove a label", write=True)
    sp.add_argument("label", help="name or id")
    ids_arg(sp)

    # Message state
    mark_help = {
        "read": "mark messages or threads as read",
        "unread": "mark messages or threads as unread",
        "star": "star messages or threads",
        "unstar": "remove the star",
        "archive": "archive (remove from Inbox, keep in All Mail)",
        "unarchive": "move back to the Inbox",
    }
    for action in MARK_ACTIONS:
        name = f"mark-{action}" if action in ("read", "unread") else action
        sp = add(sub, name, cmd_mark, mark_help[action], write=True)
        sp.set_defaults(action=action)
        ids_arg(sp)

    sp = add(sub, "trash", cmd_trash, "move to Trash (emptied by Gmail after 30 days)", write=True)
    ids_arg(sp)
    sp = add(sub, "untrash", cmd_untrash, "restore from Trash", write=True)
    ids_arg(sp)
    sp = add(
        sub,
        "delete",
        cmd_delete,
        "delete permanently, skipping Trash (needs --permanent and the full scope)",
        write=True,
    )
    ids_arg(sp)
    sp.add_argument("--permanent", action="store_true", help="confirm: this cannot be undone")

    # MCP
    sp = add(sub, "mcp", cmd_mcp, "run the MCP server over stdio")
    sp.add_argument("--read-only", action="store_true", help="expose only the reading tools")
    sp.add_argument(
        "--allow-delete",
        action="store_true",
        help="also expose delete_permanently (needs `login --scope full`)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_out = getattr(args, "json", False)
    try:
        result = args.func(args)
    except _StatusFailed as e:
        if json_out:
            print(json.dumps(e.status, indent=2, ensure_ascii=False))
        return 1
    except GmailAgentError as e:
        if json_out:
            print(json.dumps({"error": str(e), "code": e.code}, ensure_ascii=False))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2 if isinstance(e, SetupError) else 1
    except OSError as e:  # e.g. --body-file that does not exist
        msg = f"{e.strerror or e}: {e.filename}" if e.filename else str(e)
        if json_out:
            print(json.dumps({"error": msg, "code": "error"}, ensure_ascii=False))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if json_out and result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

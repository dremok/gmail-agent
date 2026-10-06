"""Command line interface. Human-readable output by default, `--json` for scripts and agents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__, auth, config
from .errors import GmailAgentError, SetupError
from .gmail import DEFAULT_BODY_CHARS, DEFAULT_TEXT_CHARS, Gmail

EPILOG = """\
examples:
  gmail-agent search "from:billing@example.com newer_than:30d" -n 5
  gmail-agent attachments 18c2f0a1b2c3d4e5
  gmail-agent download 18c2f0a1b2c3d4e5 -o ~/Downloads/invoices --name invoice.pdf
  gmail-agent fetch "subject:invoice has:attachment after:2026/01/01" -o ./invoices --match "*.pdf"
  gmail-agent --json message 18c2f0a1b2c3d4e5

Queries use Gmail search syntax: https://support.google.com/mail/answer/7190
"""


def _size(n: int) -> str:
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
    print(f"{m['id']}  {m['date']}  {m['from']}")
    print(f"    {m['subject'] or '(no subject)'}")
    for line in _attachment_lines(m["attachments"]):
        print(line)


def _print_detail(m: dict) -> None:
    for key in ("from", "to", "cc", "reply_to", "subject"):
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


def cmd_login(args) -> Any:
    creds = auth.login(allow_drafts=args.allow_drafts, open_browser=not args.no_browser)
    email = Gmail(auth.build_service(creds)).profile()["email"]
    result = {
        "logged_in": True,
        "email": email,
        "scopes": list(creds.scopes or []),
        "token": str(config.token_path()),
    }
    if not args.json:
        print(f"Logged in as {email}. Token saved to {config.token_path()}")
    return result


def cmd_logout(args) -> Any:
    removed = auth.logout()
    if not args.json:
        print(f"Removed {config.token_path()}" if removed else "Not logged in.")
        print("To revoke access entirely: https://myaccount.google.com/permissions")
    return {"removed": removed, "token": str(config.token_path())}


def cmd_status(args) -> Any:
    status: dict[str, Any] = {
        "version": __version__,
        "config_dir": str(config.config_dir()),
        "credentials_file": config.credentials_path().exists(),
        "token_file": config.token_path().exists(),
        "logged_in": False,
        "email": None,
        "drafts_enabled": False,
        "problem": None,
    }
    try:
        creds = auth.load_credentials()
        status["drafts_enabled"] = config.COMPOSE_SCOPE in (creds.scopes or [])
        status["email"] = Gmail(auth.build_service(creds)).profile()["email"]
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
        print(f"drafts:       {'enabled' if status['drafts_enabled'] else 'disabled (read-only)'}")
        if status["problem"]:
            print(f"\nproblem: {status['problem']}")
    if not status["logged_in"]:
        # Exit non-zero so `gmail-agent status` works as a setup check in scripts.
        raise _StatusFailed(status)
    return status


class _StatusFailed(Exception):
    def __init__(self, status: dict) -> None:
        self.status = status


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


def _limit(n: int) -> int | None:
    return None if n <= 0 else n


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


def cmd_labels(args) -> Any:
    labels = Gmail.connect().labels()
    if not args.json:
        for lb in labels:
            print(f"{lb['name']:<40} {lb['id']}  ({lb['type']})")
    return {"labels": labels}


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


def cmd_draft(args) -> Any:
    if args.body_file:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
    else:
        body = args.body or ""
    res = Gmail.connect(allow_drafts=True).create_draft(
        to=args.to or [],
        subject=args.subject,
        body=body,
        cc=args.cc,
        bcc=args.bcc,
        attachments=args.attach,
        reply_to_message_id=args.reply_to,
    )
    if not args.json:
        print(
            f"Draft {res['draft_id']} created (not sent). Open Gmail > Drafts to review and send."
        )
    return res


def cmd_mcp(args) -> Any:
    from .server import build_server

    build_server(allow_drafts=args.allow_drafts).run()
    return None


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON (for scripts and agents)")

    p = argparse.ArgumentParser(
        prog="gmail-agent",
        description="Search, read and download Gmail attachments. Read-only unless you opt in.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"gmail-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, func, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help, description=help)
        sp.set_defaults(func=func)
        return sp

    sp = add("login", cmd_login, "log in with your browser (one time)")
    sp.add_argument(
        "--allow-drafts",
        action="store_true",
        help="also grant gmail.compose so `draft` works (never used to send)",
    )
    sp.add_argument("--no-browser", action="store_true", help="print the login URL instead")

    add("logout", cmd_logout, "delete the saved token")
    add("status", cmd_status, "check setup and show the logged-in account (exit 1 if not ready)")

    sp = add("search", cmd_search, "search messages with a Gmail query")
    sp.add_argument("query", help='Gmail query, e.g. "from:alice@example.com has:attachment"')
    sp.add_argument("-n", "--max-results", type=int, default=20, help="page size (default 20)")
    sp.add_argument("--page-token", help="token from a previous page")
    sp.add_argument("--include-spam-trash", action="store_true")

    sp = add("message", cmd_message, "show one message: headers, text body, attachments")
    sp.add_argument("message_id")
    sp.add_argument("--max-chars", type=int, default=0, help="truncate the body (0 = no limit)")

    sp = add("thread", cmd_thread, "show every message in a thread")
    sp.add_argument("thread_id")
    sp.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_BODY_CHARS,
        help=f"truncate each body (default {DEFAULT_BODY_CHARS}, 0 = no limit)",
    )

    add("labels", cmd_labels, "list labels")

    sp = add("attachments", cmd_attachments, "list a message's attachments")
    sp.add_argument("message_id")

    sp = add("download", cmd_download, "save a message's attachments to a folder")
    sp.add_argument("message_id")
    sp.add_argument("-o", "--out-dir", default=".", help="target folder (default: current)")
    sp.add_argument(
        "--name",
        action="append",
        metavar="FILENAME|PART_ID",
        help="only this attachment; repeat for more (default: all)",
    )
    sp.add_argument(
        "--skip-inline",
        action="store_true",
        help="skip images embedded in the HTML body (logos, signatures)",
    )

    sp = add("fetch", cmd_fetch, "save all attachments from messages matching a query")
    sp.add_argument("query")
    sp.add_argument("-o", "--out-dir", default=".", help="target folder (default: current)")
    sp.add_argument("--match", metavar="GLOB", help='only filenames matching, e.g. "*.pdf"')
    sp.add_argument("--max-messages", type=int, default=50, help="default 50")
    sp.add_argument(
        "--skip-inline",
        action="store_true",
        help="skip images embedded in the HTML body (logos, signatures)",
    )

    sp = add("read", cmd_read, "print the text of a PDF or text attachment")
    sp.add_argument("message_id")
    sp.add_argument("attachment", help="filename or part id")
    sp.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_TEXT_CHARS,
        help=f"default {DEFAULT_TEXT_CHARS}, 0 = no limit",
    )

    sp = add("draft", cmd_draft, "create a draft (needs `login --allow-drafts`; never sends)")
    sp.add_argument("--to", action="append", help="recipient; repeat for more")
    sp.add_argument("--cc", action="append")
    sp.add_argument("--bcc", action="append")
    sp.add_argument("--subject")
    body = sp.add_mutually_exclusive_group()
    body.add_argument("--body", help="plain-text body")
    body.add_argument("--body-file", help="read the body from a file, or - for stdin")
    sp.add_argument("--attach", action="append", metavar="PATH", help="file to attach; repeatable")
    sp.add_argument(
        "--reply-to", metavar="MESSAGE_ID", help="make it a reply in that message's thread"
    )

    sp = add("mcp", cmd_mcp, "run the MCP server over stdio")
    sp.add_argument(
        "--allow-drafts",
        action="store_true",
        help="expose the create_draft tool (needs `login --allow-drafts`)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except _StatusFailed as e:
        if args.json:
            print(json.dumps(e.status, indent=2, ensure_ascii=False))
        return 1
    except GmailAgentError as e:
        if args.json:
            print(json.dumps({"error": str(e), "code": e.code}, ensure_ascii=False))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2 if isinstance(e, SetupError) else 1
    except KeyboardInterrupt:
        return 130
    if args.json and result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

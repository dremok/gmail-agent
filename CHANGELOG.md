# Changelog

## 0.3.0

First release on PyPI and in the MCP Registry: `uv tool install gmail-agent`, or `uvx gmail-agent mcp`.

The repo is also a Claude Code plugin: `/plugin install gmail-agent --marketplace dremok/gmail-agent` adds the MCP server and a skill.

### New

- **`gmail-agent setup`** walks you through the one-time Google Cloud setup. With gcloud installed and logged in it creates the project and enables the Gmail API; otherwise it opens the console pages. It opens the consent screen and client pages for your project, picks up the downloaded client file from `~/Downloads` (or `--client-file`, or a pasted ID and secret), checks it is a Desktop client, installs it readable only by you, logs in and checks the account. Run it again any time; it skips what is done.
- **Allowed folders for the MCP server.** Its tools save attachments to, and attach files from, `~/Downloads` only, plus folders you allow with `gmail-agent mcp --allow-dir PATH` or `GMAIL_AGENT_ALLOWED_DIRS`. A fixed block list always applies, in the CLI too: credential stores such as `~/.ssh` and `~/.aws`, gmail-agent's own config, login items, shell startup files, `.env` files, `*.p12` / `*.pfx` key stores, and any file that contains a private key, whatever its name (certificates and Keynote decks are fine). Refusals have the error code `not_allowed` and name the flag to use.
- **Links survive in HTML mail**: a link's URL now follows its text, as `text (url)`.
- **Send-as aliases count as you** when replying: reply-all leaves out every verified alias, and a reply to mail you sent from an alias goes to its recipients.
- `--json` works before or after the command, and the MCP server only offers the tools your login's access level allows.
- **Easier for agents to find.** Every MCP tool has a title, and each description says up front that it is about email or Gmail, so clients that load tools on demand by keyword search (Claude Code does) find them. The server instructions say when to use them.

### Fixed

- The MCP server no longer crashes when a client calls several tools at once. They shared one network connection, which is not thread-safe; it aborted the process ("Connection closed") or mixed up replies.
- Errors that used to read "Error executing tool" now say what went wrong and what to do: an expired or revoked login in a long-running server, a deleted OAuth client, a Gmail API that is not enabled (with the link to enable it), network failures, local file problems, and Gmail's rate limits (which no longer tell you to log in again).
- `login` gives up after 5 minutes instead of waiting forever when Google never sends the browser back, and explains "Access blocked" (Testing mode, not a test user).
- A line break in a subject or address is refused with a clear message, and malformed addresses are caught before sending: `a@example.com;b@example.com` used to send to a only.
- HTML mail converts to cleaner text: table cells no longer run together ("Total120 EUR"), quotes, lists and preformatted text keep their lines, and `<br/>` gives one line break.
- Two processes refreshing the login at the same moment can no longer corrupt `token.json`.
- When one of several requests fails (trash or label changes on many messages), the error names the id and says how many earlier requests already went through.
- `--body-file` and `--html-file` are read as UTF-8 on every system.

### Changed

- Searches and draft lists are about four times faster: messages are fetched four at a time.
- Over MCP, searches and draft lists return at most 50 per page (follow `next_page_token` for more).
- Dry runs show each attachment's size instead of its base64 content, so they stay readable.
- Every MCP tool states all four annotation hints. Sending, replying, forwarding, `send_draft`, `update_draft`, trash and the delete tools are marked destructive, so clients that confirm such calls will ask first; their descriptions also tell the model to draft or dry-run unless you asked it to send.
- Tools that return mail say that its content is untrusted data, not instructions.
- The MCP server reports its version.
- Dependencies are declared as compatible ranges, so installs pick up their security fixes; the tested versions stay pinned in `requirements.txt` and `uv.lock`.
- Python 3.11 to 3.14 are tested.

## 0.2.0 (2026-10-07)

Full read and write Gmail: send, reply, forward, drafts, labels and trash, with dry runs and access levels chosen at login.

## 0.1.0 (2026-10-06)

First version: search, read and download attachments, as a CLI and an MCP server.

# gmail-agent

Give an AI agent read access to your Gmail, with attachment downloads that actually work.

`gmail-agent` is one small Python package with two faces:

- a **CLI** for agents that have a shell (Claude Code, Codex, Aider, your own scripts), with `--json` output
- an **MCP server** over stdio for clients like Claude Desktop and Claude Code

It searches mail with normal Gmail query syntax, reads messages and threads as plain text, and downloads attachments to a folder of your choice. It is read-only by default. It talks directly to the Gmail API from your machine using your own Google Cloud OAuth client, so there is no third-party server in between.

## What it does

- Search with Gmail syntax (`from:`, `has:attachment`, `filename:pdf`, `newer_than:30d`, ...), paginated
- Read a message or a whole thread as plain text (HTML mail is converted)
- List labels
- List a message's attachments with name, type, size and part id
- Download attachments from one message, or every attachment from every message matching a query
- Extract text from PDF and text attachments without saving them
- Optional, off by default: create a draft (with attachments, or as a reply). It never sends.

Downloads never overwrite anything. If `invoice.pdf` exists, the next one becomes `invoice_1.pdf`, and you get the real paths back. Attachment names are sanitized, so a file called `../../.ssh/config` lands as `config` inside the folder you picked.

## Quick start

You need [uv](https://docs.astral.sh/uv/) (or pipx) and Python 3.11+.

```sh
uv tool install git+https://github.com/dremok/gmail-agent
```

Then do the [Google Cloud setup](#google-cloud-setup) once (about 5 minutes), and:

```sh
gmail-agent login       # opens a browser, one time
gmail-agent status      # shows the logged-in account
gmail-agent search "has:attachment newer_than:7d"
```

## Google Cloud setup

Gmail has no API keys for personal mail, so you create your own OAuth client. It is free and you only do it once.

1. **Create a project.** Go to [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate), give it a name like `gmail-agent`, and click Create. Make sure the new project is selected in the top bar for the next steps.
2. **Enable the Gmail API.** Open [the Gmail API page](https://console.cloud.google.com/apis/library/gmail.googleapis.com) and click Enable.
3. **Configure the consent screen.** Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and click Get started.
   - App name: anything, for example `gmail-agent`. Support email: your address.
   - Audience: **External**.
   - Contact information: your address. Agree to the policy and click Create.
4. **Add yourself as a test user.** Go to [Audience](https://console.cloud.google.com/auth/audience), find Test users, click Add users, enter the Gmail address you want to read, and save. While the app is in Testing mode, only test users can log in.
5. **Create a Desktop client.** Go to [Clients](https://console.cloud.google.com/auth/clients), click Create client, choose application type **Desktop app**, name it, and click Create. Download the JSON from the dialog that appears. Google may not let you download the secret again later; if you miss it, create a new client.
6. **Put the file where gmail-agent looks for it:**

   ```sh
   mkdir -p ~/.config/gmail-agent
   mv ~/Downloads/client_secret_*.json ~/.config/gmail-agent/credentials.json
   ```

7. **Log in.** Run `gmail-agent login`. A browser opens. Pick your account. Google will say **"Google hasn't verified this app"**. That is expected: it is your own app and nobody else's. Click Continue, allow read access, and close the tab. The terminal prints `Logged in as you@example.com`.

   On a machine without a browser, use `gmail-agent login --no-browser` and open the printed URL yourself. The redirect goes to `localhost`, so the browser must run on the same machine.

8. **Avoid weekly re-logins (recommended).** In Testing mode Google expires refresh tokens after **7 days**, and gmail-agent will tell you to log in again. To stop that, go to [Audience](https://console.cloud.google.com/auth/audience) and click Publish app so the publishing status is **In production**. You do not need Google's verification for personal use. You will keep seeing the unverified warning when you log in, and the app is capped at 100 users, which is fine for one person.

## CLI usage

Every command takes `--json` for machine-readable output. Run `gmail-agent <command> --help` for all options.

```sh
# Search (Gmail query syntax, 20 per page by default)
gmail-agent search "from:billing@example.com newer_than:30d"
gmail-agent search "has:attachment filename:pdf" -n 50 --json
gmail-agent search "label:receipts" --page-token <token from the previous page>

# Read
gmail-agent message 18c2f0a1b2c3d4e5
gmail-agent thread 18c2f0a1b2c3d4e5
gmail-agent labels

# Attachments
gmail-agent attachments 18c2f0a1b2c3d4e5
gmail-agent download 18c2f0a1b2c3d4e5 -o ~/Downloads/mail             # all of them
gmail-agent download 18c2f0a1b2c3d4e5 -o ./out --name invoice.pdf      # by filename
gmail-agent download 18c2f0a1b2c3d4e5 -o ./out --name 1.2              # by part id
gmail-agent fetch "subject:invoice after:2026/01/01" -o ./invoices --match "*.pdf"
gmail-agent read 18c2f0a1b2c3d4e5 invoice.pdf                          # print PDF text

# Setup
gmail-agent status
gmail-agent logout
```

Example output:

```text
$ gmail-agent search "subject:invoice" -n 1
18c2f0a1b2c3d4e5  2026-03-02T09:14:07+01:00  Billing <billing@example.com>
    Your March invoice
    [2] invoice-2026-03.pdf  (application/pdf, 84.2 KB)

$ gmail-agent download 18c2f0a1b2c3d4e5 -o ~/Downloads/invoices
/Users/you/Downloads/invoices/invoice-2026-03.pdf
```

Notes:

- `fetch` adds `has:attachment` to your query and stops after `--max-messages` messages (default 50). It tells you when more matched.
- `--skip-inline` leaves out images embedded in the HTML body, such as logos and signature images.
- Pick attachments by filename or part id (`1`, `1.2`). Gmail's attachment ids also work but can change between requests, so part ids are the stable choice.
- Exit codes: `0` ok, `1` error, `2` a setup step is missing (the message says which).

## MCP server

`gmail-agent mcp` runs an MCP server over stdio. It starts even before you log in; tools then return an error telling the user to run `gmail-agent login`.

**Claude Code:**

```sh
claude mcp add gmail --scope user -- gmail-agent mcp
```

**Claude Desktop:** edit `claude_desktop_config.json` (Settings > Developer > Edit Config). Desktop apps often do not see your shell's PATH, so use the absolute path from `which gmail-agent`:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "/Users/you/.local/bin/gmail-agent",
      "args": ["mcp"]
    }
  }
}
```

Without installing first, `uvx` works too: `"command": "uvx", "args": ["--from", "git+https://github.com/dremok/gmail-agent", "gmail-agent", "mcp"]`.

**Tools:**

| Tool | What it does |
|---|---|
| `account_status` | Logged-in address and message counts |
| `search_messages` | Gmail query, paginated; id, thread, date, from, subject, snippet, attachments |
| `get_message` | Headers, plain-text body, attachment list |
| `get_thread` | All messages in a thread, oldest first |
| `list_labels` | System and user labels |
| `list_attachments` | Attachments of a message with part ids |
| `download_attachments` | Save all or selected attachments of a message to a folder |
| `download_matching_attachments` | Save attachments from every message matching a query |
| `read_attachment_text` | Text of a PDF or text attachment, without saving |
| `create_draft` | Only with `--allow-drafts`, see below |

Read tools are marked `readOnlyHint`. The two download tools write local files but change nothing in Gmail.

## Drafts (opt-in)

Creating drafts needs a broader scope, so it is off unless you ask for it twice: once at login and once when starting the server.

```sh
gmail-agent login --allow-drafts
gmail-agent draft --to alice@example.com --subject "Report" --body "Attached." --attach report.pdf
gmail-agent draft --reply-to 18c2f0a1b2c3d4e5 --body-file reply.txt   # reply in thread

claude mcp add gmail --scope user -- gmail-agent mcp --allow-drafts
```

Drafts land in your Drafts folder and you send them yourself. gmail-agent has no send command. Be aware that Google has no "drafts only" scope: `gmail.compose` also allows sending, so a token granted with `--allow-drafts` can do more than gmail-agent ever asks of it. If you only need to read, skip this.

## Security notes

- **Read-only by default.** The default scope is `gmail.readonly`. With it, nothing (not gmail-agent, not a confused agent) can send, delete, archive or relabel mail.
- **Where secrets live.** `~/.config/gmail-agent/credentials.json` (your OAuth client) and `~/.config/gmail-agent/token.json` (your login). The token is written with mode `600` in a `700` folder. Anyone who has the token can read your mail, so treat it like a password and keep it out of repos and shared backups. `gmail-agent logout` deletes it; revoke access fully at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- **What leaves your machine.** Only requests to Google's OAuth and Gmail endpoints. No telemetry, no server of ours. Whatever the tools return does go to the agent or model you connected, which is the point. Choose that model with the care you would give anyone reading your inbox.
- **Mail is untrusted input.** An email can contain text written to steer an AI agent ("ignore previous instructions and..."). Read-only scope keeps the damage small. Think twice before giving an agent drafts access plus other powerful tools in the same session.
- **Downloads** go only where you point them, with sanitized names and no overwrites.

## For AI agents

Exact steps for an agent setting this up for a user. Steps 3 and 4 need the human.

```sh
# 1. Install (puts `gmail-agent` on PATH, usually ~/.local/bin)
uv tool install git+https://github.com/dremok/gmail-agent

# 2. Check setup. Exit code 0 means ready; 1 means not ready, and "problem" says why.
gmail-agent status --json
```

3. If `credentials_file` is `false`: the user must do steps 1 to 6 of the [Google Cloud setup](#google-cloud-setup) in a browser. You cannot do this for them. Point them at that section and wait.
4. If `credentials_file` is `true` but `logged_in` is `false`: ask the user to run `gmail-agent login` in their own terminal (it opens a browser for consent). Do not run it yourself unless you share their desktop session.
5. Verify: `gmail-agent status --json` returns `"logged_in": true` and their address.

Then use it:

```sh
gmail-agent search "from:alice@example.com has:attachment" --json
gmail-agent attachments <message_id> --json
gmail-agent download <message_id> -o /absolute/out/dir --name <filename or part id> --json
gmail-agent fetch "<gmail query>" -o /absolute/out/dir --match "*.pdf" --json
gmail-agent read <message_id> <filename or part id> --json
```

Things worth knowing:

- `download` and `fetch` print `{"saved": [{"path": ...}, ...]}`. Use those paths; names may have a `_1` suffix.
- On failure with `--json` you get `{"error": "...", "code": "setup" | "not_found" | "error"}` on stdout. For `setup`, relay the message to the user. It names the exact fix.
- Paging: pass `next_page_token` from a search back as `--page-token`.
- `message` returns the full body. `thread` and the MCP tools cut each body at 20,000 characters and set `body_truncated`; pass `--max-chars 0` (CLI) or `max_body_chars: 0` (MCP) for everything.

To add it as an MCP server instead: `claude mcp add gmail --scope user -- gmail-agent mcp`.

See also [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GMAIL_AGENT_CONFIG_DIR` | `$XDG_CONFIG_HOME/gmail-agent`, else `~/.config/gmail-agent` | Where `credentials.json` and `token.json` live |

To use two Google accounts, give each its own config dir:

```sh
GMAIL_AGENT_CONFIG_DIR=~/.config/gmail-agent-work gmail-agent login
claude mcp add gmail-work -e GMAIL_AGENT_CONFIG_DIR=$HOME/.config/gmail-agent-work -- gmail-agent mcp
```

## Development

```sh
git clone https://github.com/dremok/gmail-agent && cd gmail-agent
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests run the real `googleapiclient` Gmail service against a fake HTTP backend, so request parameters are checked against Google's discovery document without touching a real account. They also start the MCP server as a subprocess and do a full stdio handshake.

## License

MIT

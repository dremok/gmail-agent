# Privacy

gmail-agent runs entirely on your own computer. It has no server of its own, collects no analytics or telemetry, and sends nothing to its author.

## What it talks to

Only Google: the Gmail API (`www.googleapis.com`) to read and change your mail, and Google's OAuth endpoints (`accounts.google.com`, `oauth2.googleapis.com`) to log you in. It uses an OAuth client that you create in your own Google Cloud project, so no one else's app has access to your account. `gmail-agent setup` also runs the `gcloud` CLI if you have it, to create that project.

## What it keeps on your computer

- Your OAuth client (`credentials.json`) and login token (`token.json`) in `~/.config/gmail-agent` (or `$GMAIL_AGENT_CONFIG_DIR`), readable only by you.
- Attachments you ask it to save, in the folder you choose.

Nothing else is written to disk. Mail it reads goes to the program that asked for it: your terminal, or the AI agent you connected it to. What that agent does with it is up to the agent and its provider.

## Removing access

- `gmail-agent logout` deletes the saved token.
- Delete `~/.config/gmail-agent` to remove the OAuth client as well.
- Revoke the app's access to your Google account at https://myaccount.google.com/permissions.

Questions: https://github.com/dremok/gmail-agent/issues

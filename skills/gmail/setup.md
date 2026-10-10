# Connecting Gmail for the first time

gmail-agent uses the user's own Google Cloud OAuth client, so nothing passes through a third-party server. Creating that client is a one-time job of about five minutes, and parts of it can only be done by the user in their browser.

1. Check what is missing:

   ```sh
   uvx gmail-agent status --json
   ```

   `"logged_in": true` means it is ready; restart the MCP server (`/mcp`, then reconnect `gmail`) if the tools still report a setup error. If `credentials_file` is `true` but `logged_in` is `false`, only the login is missing: the user runs `uvx gmail-agent login`.

2. Otherwise ask the user to run the guided setup **in their own terminal**, not through your shell tool. It is interactive: it opens Google Cloud pages, waits while they click through, and then opens a Google login.

   ```sh
   uvx gmail-agent setup
   ```

   - It creates the project and enables the Gmail API itself when `gcloud` is installed and logged in, and otherwise opens the pages.
   - The user configures the consent screen (audience External), publishes the app, and creates a **Desktop app** client and downloads its JSON. Setup finds the file in `~/Downloads`.
   - Google shows "Google hasn't verified this app". That is expected for a personal client: Continue, and keep all boxes ticked.
   - The default access level is `modify`. For an agent that only needs to read, suggest `uvx gmail-agent setup --scope readonly`.

3. Never ask the user to paste the client secret or the token into the chat. Setup reads the downloaded file itself.

4. Verify with `uvx gmail-agent status --json`, then have the user reconnect the MCP server so the tool list matches the new login.

To let the tools save attachments to and attach files from folders besides `~/Downloads`, the user sets `GMAIL_AGENT_ALLOWED_DIRS` (folders separated by `:`) in the environment Claude Code starts from, or adds the server by hand with `claude mcp add gmail --scope user -- uvx gmail-agent mcp --allow-dir /path/to/folder`.

More detail: https://github.com/dremok/gmail-agent#google-cloud-setup

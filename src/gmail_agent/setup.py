"""`gmail-agent setup`: the one-time Google Cloud setup, as far as Google lets it be automated.

For a personal Gmail account only two steps have an API: creating the project and enabling
the Gmail API. gcloud does those when it is installed and logged in; otherwise they are two
console pages. The consent screen, publishing status and the Desktop OAuth client have no
public API, so setup opens those console pages, then picks up the downloaded client file,
checks it, installs it with owner-only permissions, logs in and verifies the account.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import auth, config
from .errors import SetupError
from .gmail import Gmail

GMAIL_API = "gmail.googleapis.com"
CLIENT_GLOB = "client_secret_*.json"
CLIENT_WAIT = 600  # seconds to wait for the downloaded client file
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")  # Google's rule: 6-30 chars


def say(text: str = "") -> None:
    """The wizard talks on stderr, so stdout stays clean for --json."""
    print(text, file=sys.stderr)


def ask(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise SetupError("Setup needs answers on standard input; run it in a terminal.")
    return line.strip()


def gcloud(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    return subprocess.run(
        ["gcloud", *args], capture_output=True, text=True, env=env, timeout=300, check=False
    )


def gcloud_account() -> str | None:
    """The active gcloud account, or None if gcloud is missing or not logged in."""
    if shutil.which("gcloud") is None:
        return None
    result = gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    lines = result.stdout.split() if result.returncode == 0 else []
    return lines[0] if lines else None


def check_client(data: Any, project: str | None) -> dict:
    """Check a downloaded OAuth client file before installing it. Returns it unchanged."""
    if not isinstance(data, dict):
        raise SetupError("That is not an OAuth client file (expected a JSON object).")
    if "installed" not in data:
        if "web" in data:
            raise SetupError(
                "That is a 'Web application' client. gmail-agent needs a 'Desktop app' client: "
                "create one with Application type Desktop app and download its JSON."
            )
        raise SetupError("That is not an OAuth client file (no 'installed' section).")
    client = data["installed"]
    if not str(client.get("client_id", "")).endswith(".apps.googleusercontent.com"):
        raise SetupError("The client file has no valid client_id (*.apps.googleusercontent.com).")
    if not client.get("client_secret"):
        raise SetupError(
            "The client file has no client_secret. Google shows the secret only when the "
            "client is created, so create a new Desktop client and download its JSON at once."
        )
    if project and client.get("project_id") and client["project_id"] != project:
        raise SetupError(
            f"That client belongs to project {client['project_id']}, not {project}. Use a "
            f"client from {project}, or run setup again with --project {client['project_id']}."
        )
    return data


def client_from_id_and_secret(client_id: str, secret: str, project: str | None) -> dict:
    """The same JSON Google's download contains, built from a pasted ID and secret."""
    client: dict[str, Any] = {
        "client_id": client_id,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": secret,
        "redirect_uris": ["http://localhost"],
    }
    if project:
        client["project_id"] = project
    return {"installed": client}


class Setup:
    def __init__(
        self,
        project: str | None = None,
        client_file: str | None = None,
        use_gcloud: bool = True,
        open_browser: bool = True,
        level: str | None = None,
    ) -> None:
        """`level` None keeps a working login as it is, and logs in at the default level
        otherwise; a level given explicitly always logs in again at that level."""
        self.project = project
        self.client_file = client_file
        self.use_gcloud = use_gcloud
        self.open_browser = open_browser
        self.level = level
        self.new_client = False

    def open_page(self, path: str, what: str) -> None:
        url = auth.console_url(path, self.project)
        say(f"\n{what}\n   {url}")
        if self.open_browser:
            webbrowser.open(url)

    def run(self) -> dict[str, Any]:
        installed = self.installed_client()
        if installed is None:
            account = gcloud_account() if self.use_gcloud else None
            self.project_and_api(account)
            self.consent_screen()
            self.install_client(self.desktop_client())
        return self.log_in()

    # Step 0: is a good client already installed? -----------------------------------------

    def installed_client(self) -> dict | None:
        path = config.credentials_path()
        if self.client_file:
            data = self.read_client(Path(self.client_file).expanduser())
            self.install_client(data)
            return data
        if not path.exists():
            return None
        try:
            data = check_client(json.loads(path.read_text()), None)
        except (OSError, ValueError, SetupError) as e:
            say(f"The OAuth client at {path} is not usable ({e}); setting up a new one.")
            return None
        self.project = self.project or data["installed"].get("project_id")
        say(f"OAuth client found at {path}; skipping the Google Cloud steps.")
        return data

    # Step 1 and 2: project and Gmail API ----------------------------------------------------

    def project_and_api(self, account: str | None) -> None:
        if account:
            say(f"Using gcloud, logged in as {account}.")
            if not self.project:
                self.project = self.create_project()
            self.enable_api()
            return
        if self.use_gcloud and shutil.which("gcloud"):
            say("gcloud is installed but not logged in, so this uses the console instead.")
        if not self.project:
            self.open_page("/projectcreate", "1. Create a Google Cloud project (any name):")
            while not PROJECT_ID.match(project := ask("   Project ID (shown under the name): ")):
                say("   That is not a project ID: 6 to 30 lowercase letters, digits or dashes.")
            self.project = project
        self.open_page(
            f"/apis/enableflow;apiid={GMAIL_API}", "2. Enable the Gmail API (click Enable):"
        )
        ask("   Press Enter when it is enabled. ")

    def create_project(self) -> str:
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        project = f"gmail-agent-{suffix}"
        if ask(f"Create the Google Cloud project {project}? [Y/n] ").lower() not in ("", "y"):
            raise SetupError("Stopped. Run setup with --project ID to use an existing project.")
        while (result := gcloud("projects", "create", project, "--name=gmail-agent")).returncode:
            if "Terms of Service" not in result.stderr:
                raise SetupError(f"gcloud could not create {project}: {result.stderr.strip()}")
            say(
                "\nGoogle Cloud needs you to accept its Terms of Service once. Sign in at "
                f"{auth.CONSOLE} with the same account and accept them."
            )
            ask("Press Enter to try again. ")
        state = gcloud("projects", "describe", project, "--format=value(lifecycleState)")
        if state.stdout.strip() != "ACTIVE":
            raise SetupError(f"Project {project} was created but is not active yet; try again.")
        say(f"Created project {project}.")
        return project

    def enable_api(self) -> None:
        project = str(self.project)
        result = gcloud("services", "enable", GMAIL_API, f"--project={project}")
        if result.returncode:
            raise SetupError(f"gcloud could not enable the Gmail API: {result.stderr.strip()}")
        enabled = gcloud(
            "services",
            "list",
            "--enabled",
            f"--project={project}",
            f"--filter=config.name={GMAIL_API}",
            "--format=value(config.name)",
        )
        if enabled.stdout.strip() != GMAIL_API:
            raise SetupError(f"The Gmail API does not show as enabled in {project}; try again.")
        say(f"Gmail API enabled in {project}.")

    # Step 3: consent screen, by hand ---------------------------------------------------------

    def consent_screen(self) -> None:
        self.open_page(
            "/auth/overview",
            "3. Configure the consent screen: click Get started. App name: gmail-agent. "
            "Support email: yours.\n   Audience: External. Contact email: yours. Agree, then "
            "Create.",
        )
        ask("   Press Enter when that is done. ")
        self.open_page(
            "/auth/audience",
            "4. Click Publish app, so the status is 'In production'. Otherwise Google expires "
            "the login\n   every 7 days and only addresses under Test users can log in. Google "
            "will call the app\n   unverified; that is expected for your own app.",
        )
        ask("   Press Enter when it is published (or you added yourself as a test user). ")

    # Step 4: the Desktop client --------------------------------------------------------------

    def desktop_client(self) -> dict:
        started = time.time()
        self.open_page(
            "/auth/clients",
            "5. Create client: Application type Desktop app, any name, Create. Then click "
            "Download JSON\n   (Google shows the secret only now).",
        )
        found = self.wait_for_download(started)
        if found:
            return self.read_client(found)
        say("Paste the client's ID and secret instead (the secret is not shown).")
        client_id = ask("   Client ID: ")
        secret = getpass.getpass("   Client secret: ", stream=sys.stderr)
        return check_client(client_from_id_and_secret(client_id, secret, self.project), None)

    def wait_for_download(self, since: float) -> Path | None:
        downloads = Path.home() / "Downloads"
        say(
            f"\nWaiting for {CLIENT_GLOB} to appear in {downloads}. Press Ctrl-C to paste the "
            "client ID and secret instead, or run setup with --client-file PATH."
        )
        deadline = time.monotonic() + CLIENT_WAIT
        try:
            while time.monotonic() < deadline:
                new = [p for p in downloads.glob(CLIENT_GLOB) if p.stat().st_mtime >= since]
                if new:
                    return max(new, key=lambda p: p.stat().st_mtime)
                time.sleep(1)
        except KeyboardInterrupt:
            say()
        return None

    def read_client(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text())
        except OSError as e:
            raise SetupError(f"Cannot read {path}: {e.strerror or e}") from e
        except ValueError as e:
            raise SetupError(f"{path} is not JSON, so not an OAuth client file.") from e
        check_client(data, self.project)
        self.project = self.project or data["installed"].get("project_id")
        say(f"Found a Desktop client in {path}.")
        return data

    def install_client(self, data: dict) -> None:
        path = auth.write_private(config.credentials_path(), json.dumps(data, indent=2))
        self.new_client = True
        say(f"Installed the OAuth client at {path} (readable only by you).")
        if self.client_file is None:
            say("You can delete the downloaded client_secret_*.json now; setup keeps its copy.")

    # Step 5: log in and check ----------------------------------------------------------------

    def log_in(self) -> dict[str, Any]:
        creds = None
        if not self.new_client and self.level is None:
            try:
                creds = auth.load_credentials()
                granted = list(creds.scopes or [])
                say("Already logged in; checking the account.")
            except SetupError:
                creds = None
        if creds is None:
            say("\n6. Log in with your Google account and allow access (a browser opens).")
            creds = auth.login(self.level or config.DEFAULT_LEVEL, self.open_browser)
            granted = auth.granted_scopes(creds)
        email = Gmail(auth.build_service(creds), granted).profile()["email"]
        level = config.level_of(granted)
        return {
            "logged_in": True,
            "email": email,
            "level": level,
            "scopes": granted,
            "project": self.project,
            "credentials": str(config.credentials_path()),
            "token": str(config.token_path()),
        }

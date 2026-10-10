"""Which local files gmail-agent may attach to mail, and where it may save attachments.

Two layers. The MCP server only uses allowed folders: ~/Downloads, plus any given with
`gmail-agent mcp --allow-dir PATH` or $GMAIL_AGENT_ALLOWED_DIRS. The CLI takes any folder,
since a person typed the path. On top of that a fixed block list always applies, in both:
credential stores, private keys, .env files and shell or login startup files are never read
or written, even inside an allowed folder.

Paths are compared after `~` expansion and symlink resolution, so `..` and links cannot
lead out of a folder. Folders that exist are also compared by file identity, so a different
spelling (case on macOS, /System/Volumes/Data/...) still matches.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config
from .errors import NotAllowedError

ALLOWED_DIRS_ENV = "GMAIL_AGENT_ALLOWED_DIRS"

# Under the home folder: credential stores and places whose files run at login.
BLOCKED_HOME_DIRS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".claude",
    ".config/gcloud",
    ".config/gh",
    ".config/fish",
    ".config/autostart",
    ".config/systemd",
    ".local/share/keyrings",
    "Library/Keychains",
    "Library/LaunchAgents",
)
BLOCKED_SYSTEM_DIRS = ("/Library/LaunchAgents", "/Library/LaunchDaemons")
# Files blocked by name wherever they are: shell startup files, credential files, SSH keys.
BLOCKED_NAMES = frozenset(
    {
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".bash_logout",
        ".profile",
        ".zshrc",
        ".zshenv",
        ".zprofile",
        ".zlogin",
        ".zlogout",
        ".cshrc",
        ".tcshrc",
        ".kshrc",
        ".env",
        ".netrc",
        ".pypirc",
        ".git-credentials",
        ".npmrc",
        ".pgpass",
        ".claude.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
BLOCKED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})  # private keys and keystores


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _inside(path: Path, folder: Path) -> bool:
    """Whether the resolved `path` is `folder` or anything under it."""
    if path == folder or folder in path.parents:
        return True
    folder_id = _identity(folder)
    if folder_id is None:
        return False
    # Same folder under another spelling: compare the existing ancestors by identity.
    return any(_identity(p) == folder_id for p in (path, *path.parents))


def _named_inside(path: Path, folder: Path) -> bool:
    """Case-insensitive name comparison, for blocked folders that do not exist yet."""
    p, f = str(path).casefold(), str(folder).casefold()
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def blocked_dirs() -> list[Path]:
    home = Path.home()
    dirs = [config.config_dir(), home / ".config" / "gmail-agent"]
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        dirs.append(Path(xdg) / "gmail-agent")
    dirs += [home / d for d in BLOCKED_HOME_DIRS]
    dirs += [Path(d) for d in BLOCKED_SYSTEM_DIRS]
    return [_resolve(d) for d in dirs]


def _blocked_name(name: str) -> bool:
    name = name.casefold()
    return (
        name in BLOCKED_NAMES
        or name.startswith(".env.")
        or os.path.splitext(name)[1] in BLOCKED_SUFFIXES
    )


class FileGuard:
    """`allowed` folders, or None for any folder (the CLI). The block list always applies."""

    def __init__(self, allowed: list[str | Path] | None = None) -> None:
        self.allowed = None if allowed is None else [_resolve(d) for d in allowed]

    @classmethod
    def for_mcp(cls, extra: list[str] | None = None) -> FileGuard:
        """~/Downloads, plus $GMAIL_AGENT_ALLOWED_DIRS and `--allow-dir` folders."""
        env = [d for d in os.environ.get(ALLOWED_DIRS_ENV, "").split(os.pathsep) if d.strip()]
        folders = [Path.home() / "Downloads", *env, *(extra or [])]
        unique = list(dict.fromkeys(_resolve(d) for d in folders))
        return cls(unique)

    def describe(self) -> list[str] | None:
        return None if self.allowed is None else [str(d) for d in self.allowed]

    def _check(self, path: Path, what: str, suggest: Path) -> None:
        for folder in blocked_dirs():
            if _inside(path, folder) or _named_inside(path, folder):
                raise NotAllowedError(
                    f"{what} {path} is inside {folder}. gmail-agent never reads or writes "
                    "credential stores, its own config folder or login startup folders, not "
                    "even inside an allowed folder."
                )
        if self.allowed is not None and not any(_inside(path, d) for d in self.allowed):
            raise NotAllowedError(
                f"{what} {path} is outside the folders this MCP server may use: "
                f"{', '.join(map(str, self.allowed))}. To allow another folder, start the "
                f"server with `gmail-agent mcp --allow-dir {suggest}` or add it to "
                f"{ALLOWED_DIRS_ENV} (separated by {os.pathsep!r}), then restart it."
            )

    def writable_dir(self, out_dir: str | Path) -> Path:
        """The resolved folder to save attachments in, if allowed."""
        path = _resolve(out_dir)
        self._check(path, "Download folder", path)
        return path

    def readable_file(self, file: str | Path) -> Path:
        """The resolved file to attach, if allowed."""
        path = _resolve(file)
        if _blocked_name(path.name):
            raise NotAllowedError(
                f"Attachment {path} looks like a key, credentials or shell startup file "
                f"({path.name}). gmail-agent never attaches those, not even from an allowed "
                "folder."
            )
        self._check(path, "Attachment", path.parent)
        return path

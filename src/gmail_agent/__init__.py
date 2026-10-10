"""Gmail for AI agents: a CLI and an MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gmail-agent")  # pyproject.toml is the one place the version lives
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

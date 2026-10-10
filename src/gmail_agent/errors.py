class GmailAgentError(Exception):
    """An error with a message meant for the person or agent running the tool."""

    code = "error"


class SetupError(GmailAgentError):
    """A setup step is missing: no OAuth client, not logged in, token expired."""

    code = "setup"


class ScopeError(SetupError):
    """The login works but did not grant the scope this call needs."""

    code = "scope"


class NotFoundError(GmailAgentError):
    code = "not_found"


def describe_os_error(e: OSError) -> str:
    """ "Permission denied: /path" rather than "[Errno 13] Permission denied: '/path'"."""
    return f"{e.strerror or e}: {e.filename}" if e.filename else str(e)

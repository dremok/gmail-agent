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

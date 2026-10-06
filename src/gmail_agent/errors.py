class GmailAgentError(Exception):
    """An error with a message meant for the person or agent running the tool."""

    code = "error"


class SetupError(GmailAgentError):
    """A setup step is missing: no OAuth client, not logged in, token expired, missing scope."""

    code = "setup"


class NotFoundError(GmailAgentError):
    code = "not_found"

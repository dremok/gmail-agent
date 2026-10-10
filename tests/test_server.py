import json
import sys
import threading

import anyio
import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp import Client, StdioServerParameters

from gmail_agent import auth, config
from gmail_agent.gmail import Gmail
from gmail_agent.server import build_server

from .conftest import FakeGmailHttp

READ_TOOLS = {
    "account_status",
    "search_messages",
    "get_message",
    "get_thread",
    "list_labels",
    "list_attachments",
    "download_attachments",
    "download_matching_attachments",
    "read_attachment_text",
    "list_drafts",
    "get_draft",
}
WRITE_TOOLS = {
    "send_message",
    "reply_to_message",
    "forward_message",
    "create_draft",
    "update_draft",
    "send_draft",
    "delete_draft",
    "create_label",
    "rename_label",
    "delete_label",
    "modify_labels",
    "mark_messages",
    "trash",
    "untrash",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def server_with(gmail, **kw):
    return build_server(connect=lambda: gmail, **kw)


async def tool_map(server):
    async with Client(server) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


@pytest.mark.anyio
async def test_default_tools_read_and_write_but_no_permanent_delete(gmail):
    tools = await tool_map(server_with(gmail))
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    assert tools["search_messages"].annotations.read_only_hint is True
    assert tools["download_attachments"].annotations.destructive_hint is False
    assert tools["trash"].annotations.destructive_hint is True
    assert tools["send_message"].annotations.read_only_hint is False
    schema = tools["download_attachments"].input_schema
    assert schema["required"] == ["message_id", "out_dir"]
    assert "attachments" in schema["properties"]
    actions = tools["mark_messages"].input_schema["properties"]["action"]["enum"]
    assert set(actions) == {"read", "unread", "star", "unstar", "archive", "unarchive"}


@pytest.mark.anyio
async def test_read_only_server(gmail):
    assert set(await tool_map(server_with(gmail, read_only=True))) == READ_TOOLS


COMPOSE_TOOLS = {
    "send_message",
    "reply_to_message",
    "forward_message",
    "create_draft",
    "update_draft",
    "send_draft",
    "delete_draft",
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("level", "allow_delete", "expected"),
    [
        ("readonly", True, READ_TOOLS),
        ("compose", True, READ_TOOLS | COMPOSE_TOOLS),
        ("modify", True, READ_TOOLS | WRITE_TOOLS),
        ("full", False, READ_TOOLS | WRITE_TOOLS),
        ("full", True, READ_TOOLS | WRITE_TOOLS | {"delete_permanently"}),
    ],
)
async def test_tools_follow_the_granted_scopes(gmail, level, allow_delete, expected):
    server = server_with(gmail, scopes=config.LEVELS[level], allow_delete=allow_delete)
    assert set(await tool_map(server)) == expected


@pytest.mark.anyio
async def test_permanent_delete_is_destructive(gmail):
    server = server_with(gmail, scopes=config.LEVELS["full"], allow_delete=True)
    tools = await tool_map(server)
    assert tools["delete_permanently"].annotations.destructive_hint is True


@pytest.mark.anyio
async def test_read_only_flag_beats_a_full_login(gmail):
    server = server_with(gmail, scopes=config.LEVELS["full"], allow_delete=True, read_only=True)
    assert set(await tool_map(server)) == READ_TOOLS


@pytest.mark.anyio
async def test_stdio_server_reads_scopes_from_saved_token(isolated_config):
    isolated_config.mkdir()
    config.token_path().write_text(json.dumps({"scopes": config.LEVELS["compose"]}))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gmail_agent", "mcp"],
        env={"GMAIL_AGENT_CONFIG_DIR": str(isolated_config)},
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == READ_TOOLS | COMPOSE_TOOLS


@pytest.mark.anyio
async def test_search_and_download(gmail, tmp_path):
    async with Client(server_with(gmail)) as client:
        found = await client.call_tool("search_messages", {"query": "invoice", "max_results": 5})
        assert not found.is_error
        ids = [m["id"] for m in found.structured_content["messages"]]
        assert ids == ["m3", "m1"]

        saved = await client.call_tool(
            "download_attachments",
            {"message_id": "m1", "out_dir": str(tmp_path), "attachments": ["invoice.pdf"]},
        )
        assert not saved.is_error
        [entry] = saved.structured_content["saved"]
        assert entry["path"] == str((tmp_path / "invoice.pdf").resolve())


@pytest.mark.anyio
async def test_read_attachment_text(gmail):
    async with Client(server_with(gmail)) as client:
        res = await client.call_tool(
            "read_attachment_text", {"message_id": "m1", "attachment": "1"}
        )
    assert "Total due 120 EUR" in res.structured_content["text"]


@pytest.mark.anyio
async def test_reply_dry_run_then_send(gmail, mailbox):
    async with Client(server_with(gmail)) as client:
        dry = await client.call_tool(
            "reply_to_message", {"message_id": "m1", "body": "Paid", "dry_run": True}
        )
        assert dry.structured_content["dry_run"] is True
        assert "In-Reply-To: <m1@mail.example.com>" in dry.structured_content["mime"]
        assert mailbox.sent == []
        sent = await client.call_tool("reply_to_message", {"message_id": "m1", "body": "Paid"})
    assert sent.structured_content["sent"] is True
    assert sent.structured_content["id"] == mailbox.sent[0]["id"]


@pytest.mark.anyio
async def test_mark_and_trash(gmail, mailbox):
    async with Client(server_with(gmail)) as client:
        res = await client.call_tool("mark_messages", {"ids": ["m1"], "action": "star"})
        assert not res.is_error
        await client.call_tool("trash", {"ids": ["m2"]})
    assert "STARRED" in mailbox.messages["m1"]["labelIds"]
    assert "TRASH" in mailbox.messages["m2"]["labelIds"]


@pytest.mark.anyio
async def test_scope_errors_are_readable(service):
    # A server built before a re-login can still hold tools the new token lacks.
    readonly = Gmail(service, config.LEVELS["readonly"])
    async with Client(server_with(readonly, scopes=config.LEVELS["modify"])) as client:
        res = await client.call_tool(
            "send_message", {"to": ["bob@example.com"], "subject": "x", "body": "y"}
        )
    assert res.is_error
    assert "gmail-agent login --scope compose" in res.content[0].text


@pytest.mark.anyio
async def test_errors_come_back_as_tool_errors(gmail):
    async with Client(server_with(gmail)) as client:
        res = await client.call_tool("get_message", {"message_id": "nope"})
    assert res.is_error
    assert "not found" in res.content[0].text


@pytest.mark.anyio
async def test_setup_error_when_not_logged_in():
    async with Client(build_server()) as client:
        res = await client.call_tool("account_status", {})
    assert res.is_error
    assert "gmail-agent login" in res.content[0].text


@pytest.mark.anyio
async def test_stdio_handshake(isolated_config):
    """Start the real `gmail-agent mcp` process and talk to it over stdio."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gmail_agent", "mcp"],
        env={"GMAIL_AGENT_CONFIG_DIR": str(isolated_config)},
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == READ_TOOLS | WRITE_TOOLS
        res = await client.call_tool("search_messages", {"query": "x"})
        assert res.is_error
        assert "No OAuth client file" in res.content[0].text


class Connection(FakeGmailHttp):
    """One connection, like an httplib2.Http, that notices when two threads use it at once.

    The first `together` requests wait for each other, so they are in flight at the same
    moment whether or not they share a connection.
    """

    def __init__(self, mailbox, together: threading.Barrier, overlaps: list[str]) -> None:
        super().__init__(mailbox)
        self.together = together
        self.overlaps = overlaps
        self.busy = threading.Lock()

    def request(self, *args, **kwargs):
        mine = self.busy.acquire(blocking=False)
        if not mine:
            self.overlaps.append(threading.current_thread().name)
        try:
            if not self.together.broken:
                self.together.wait()
            return super().request(*args, **kwargs)
        finally:
            if mine:
                self.busy.release()


async def get_two_messages_at_once(server) -> dict[str, dict]:
    results: dict[str, dict] = {}
    async with Client(server) as client:

        async def get(message_id):
            res = await client.call_tool("get_message", {"message_id": message_id})
            assert not res.is_error, res.content[0].text
            results[message_id] = res.structured_content

        async with anyio.create_task_group() as tg:
            tg.start_soon(get, "m1")
            tg.start_soon(get, "m2")
    return results


@pytest.mark.anyio
async def test_parallel_tool_calls_use_separate_connections(mailbox):
    """Regression: parallel calls shared one httplib2 connection and crashed the server."""
    together, overlaps = threading.Barrier(2, timeout=5), []

    def connect():
        transport = lambda: Connection(mailbox, together, overlaps)  # noqa: E731
        return Gmail(auth.build_service(Credentials(token="t"), transport=transport))

    results = await get_two_messages_at_once(build_server(connect=connect))
    assert overlaps == []
    assert results["m1"]["subject"] == "Invoice March"
    assert results["m2"]["subject"] == "Notes from Alice"


@pytest.mark.anyio
async def test_parallel_tool_calls_really_overlap(mailbox):
    """The control for the test above: one shared connection is used by two threads at once."""
    together, overlaps = threading.Barrier(2, timeout=5), []
    shared = build("gmail", "v1", http=Connection(mailbox, together, overlaps))
    await get_two_messages_at_once(build_server(connect=lambda: Gmail(shared)))
    assert len(overlaps) == 1

import sys

import pytest
from mcp import Client, StdioServerParameters

from gmail_agent.server import build_server

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
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def server_with(gmail, allow_drafts=False):
    return build_server(allow_drafts=allow_drafts, connect=lambda needs_drafts: gmail)


@pytest.mark.anyio
async def test_read_only_by_default(gmail):
    async with Client(server_with(gmail)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS
    assert tools["search_messages"].annotations.read_only_hint is True
    assert tools["download_attachments"].annotations.destructive_hint is False
    schema = tools["download_attachments"].input_schema
    assert schema["required"] == ["message_id", "out_dir"]
    assert "attachments" in schema["properties"]


@pytest.mark.anyio
async def test_drafts_only_when_opted_in(gmail):
    async with Client(server_with(gmail, allow_drafts=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == READ_TOOLS | {"create_draft"}


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
        assert names == READ_TOOLS
        res = await client.call_tool("search_messages", {"query": "x"})
        assert res.is_error
        assert "No OAuth client file" in res.content[0].text

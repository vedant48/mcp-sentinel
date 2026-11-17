import asyncio
import sys
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from aiohttp import ClientSession as HttpSession
from mcp import ClientSession, McpError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from test_approval import wait_pending
from test_gateway import TOKEN, serve

from mcp_sentinel.approval import ApprovalQueue
from mcp_sentinel.gateway import Gateway

SERVER = str(Path(__file__).parents[1] / "examples" / "demo_server.py")


async def approve_and_block(session, queue, gate_all=False):
    async def setup_request(coroutine):
        task = asyncio.create_task(coroutine)
        if gate_all:
            (key,) = await wait_pending(queue)
            queue.decide(key, True)
        return await task

    initialized = await setup_request(session.initialize())
    assert initialized.serverInfo.name == "Sentinel demo"
    assert {tool.name for tool in (await setup_request(session.list_tools())).tools} == {
        "echo",
        "slow_echo",
    }
    approved = asyncio.create_task(session.call_tool("echo", {"message": "approved"}))
    blocked = asyncio.create_task(session.call_tool("echo", {"message": "blocked"}))
    keys = await wait_pending(queue, 2)
    by_text = {queue.pending[key].message["params"]["arguments"]["message"]: key for key in keys}
    queue.decide(by_text["blocked"], False)
    with pytest.raises(McpError, match="Blocked by user"):
        await blocked
    assert not approved.done()
    queue.decide(by_text["approved"], True)
    result = await approved
    assert result.content[0].text == "Echo: approved"


@pytest.mark.parametrize("gate_all", [False, True])
async def test_official_sdk_stdio_client_and_fastmcp_server(tmp_path, gate_all):
    token = tmp_path / "control.token"
    token.write_text(TOKEN)
    queue = ApprovalQueue(timeout=10)
    async with serve(Gateway(queue, TOKEN, TOKEN).control_app()) as url:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "mcp_sentinel.cli",
                "stdio",
                "--control-url",
                url,
                "--control-token-file",
                str(token),
                "--",
                sys.executable,
                SERVER,
            ],
        )
        if gate_all:
            params.args.insert(params.args.index("--"), "--gate-all")
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=10)
            ) as session:
                await approve_and_block(session, queue, gate_all)


@pytest.mark.parametrize("gate_all", [False, True])
async def test_official_sdk_streamable_http_client_and_fastmcp_server(gate_all):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        SERVER,
        "--http",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10), HttpSession() as client:
            while True:
                if process.returncode is not None:
                    pytest.fail((await process.stderr.read()).decode())
                try:
                    async with client.get("http://127.0.0.1:9000/mcp"):
                        break
                except OSError:
                    await asyncio.sleep(0.02)
        queue = ApprovalQueue(timeout=10)
        gateway = Gateway(queue, TOKEN, TOKEN, "http://127.0.0.1:9000/mcp", gate_all=gate_all)
        async with serve(gateway.remote_app()) as url:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15
            ) as client:
                async with streamable_http_client(url + "/mcp", http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await approve_and_block(session, queue, gate_all)
    finally:
        if process.returncode is None:
            process.terminate()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 3)
        if process.returncode is None:
            process.kill()
            await process.wait()

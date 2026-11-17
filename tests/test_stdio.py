import asyncio
import json
import sys

import pytest
from test_approval import message, wait_pending
from test_gateway import TOKEN, serve

from mcp_sentinel.approval import ApprovalQueue
from mcp_sentinel.gateway import Gateway

SERVER = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "tools/call":
        with open(sys.argv[1], "a") as file:
            file.write(str(message["id"]) + "\n")
    if "id" in message and "method" in message:
        print(json.dumps({"jsonrpc":"2.0", "id":message["id"], "result":{"ok":True}}), flush=True)
"""


async def launch(tmp_path, url):
    token = tmp_path / "control.token"
    token.write_text(TOKEN)
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    marker = tmp_path / "executed.txt"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_sentinel.cli",
        "stdio",
        "--control-url",
        url,
        "--control-token-file",
        str(token),
        "--",
        sys.executable,
        str(server),
        str(marker),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return process, marker


async def send(process, payload):
    process.stdin.write(json.dumps(payload).encode() + b"\n")
    await process.stdin.drain()


async def read(process):
    async with asyncio.timeout(5):
        return json.loads(await process.stdout.readline())


async def finish(process):
    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        process.kill()
        await process.wait()
        pytest.fail("stdio wrapper did not exit after client EOF")
    error = (await process.stderr.read()).decode()
    assert "Traceback" not in error, error


async def test_stdio_discovery_concurrency_and_no_blocked_execution(tmp_path):
    queue = ApprovalQueue(timeout=10)
    gateway = Gateway(queue, TOKEN, TOKEN)
    async with serve(gateway.control_app()) as url:
        process, marker = await launch(tmp_path, url)
        try:
            await send(process, message(0, "tools/list"))
            assert (await read(process))["id"] == 0
            await send(process, message(1))
            await send(process, message(2))
            keys = await wait_pending(queue, 2)
            by_id = {queue.pending[k].message["id"]: k for k in keys}
            assert not marker.exists()
            queue.decide(by_id[2], True)
            assert (await read(process)) == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
            assert marker.read_text() == "2\n"
            queue.decide(by_id[1], False)
            blocked = await read(process)
            assert blocked["id"] == 1 and blocked["error"]["code"] == -32001
            assert marker.read_text() == "2\n"
        finally:
            await finish(process)


async def test_stdio_approval_daemon_unavailable_is_fail_closed(tmp_path):
    # Bind an unused HTTP route rather than assuming a particular port is free.
    from aiohttp import web

    async with serve(web.Application()) as url:
        process, marker = await launch(tmp_path, url)
        try:
            await send(process, message())
            assert "Approval service unavailable" in (await read(process))["error"]["message"]
            assert not marker.exists()
        finally:
            await finish(process)


async def test_stdio_cancel_does_not_forward_original_request(tmp_path):
    queue = ApprovalQueue(timeout=10)
    gateway = Gateway(queue, TOKEN, TOKEN)
    async with serve(gateway.control_app()) as url:
        process, marker = await launch(tmp_path, url)
        try:
            await send(process, message())
            await wait_pending(queue)
            await send(
                process,
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            )
            assert (await read(process))["error"]["code"] == -32800
            async with asyncio.timeout(3):
                while queue.pending:
                    await asyncio.sleep(0.01)
            assert not marker.exists()
        finally:
            await finish(process)


async def test_stdio_invalid_message_does_not_kill_proxy(tmp_path):
    queue = ApprovalQueue(timeout=10)
    async with serve(Gateway(queue, TOKEN, TOKEN).control_app()) as url:
        process, _ = await launch(tmp_path, url)
        try:
            process.stdin.write(b"not-json\n")
            await process.stdin.drain()
            assert (await read(process))["error"]["code"] == -32700
            await send(process, message(0, "ping"))
            assert (await read(process))["id"] == 0
        finally:
            await finish(process)

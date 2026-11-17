import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, web
from test_approval import message, wait_pending

from mcp_sentinel.approval import ApprovalQueue
from mcp_sentinel.gateway import Gateway

TOKEN = "test-only-credential-not-for-use-outside-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@asynccontextmanager
async def serve(app):
    runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@asynccontextmanager
async def setup_gateway(response=None, timeout=1):
    received = []

    async def upstream(request):
        data = await request.read()
        received.append((request.method, data, dict(request.headers)))
        if response:
            return await response(request)
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": json.loads(data)["id"],
                "result": {"content": [{"type": "text", "text": "executed"}]},
            }
        )

    app = web.Application()
    app.router.add_route("*", "/mcp", upstream)
    async with serve(app) as url:
        queue = ApprovalQueue(timeout=timeout)
        gateway = Gateway(queue, TOKEN, TOKEN, url + "/mcp")
        async with serve(gateway.remote_app()) as proxy:
            async with ClientSession(headers=AUTH) as client:
                yield queue, received, client, proxy, gateway


async def post(client, url, payload):
    async with client.post(url + "/mcp", json=payload) as response:
        return response.status, await response.json()


async def test_held_then_approved_exactly_once():
    async with setup_gateway() as (queue, received, client, url, _):
        payload = message()
        task = asyncio.create_task(post(client, url, payload))
        (key,) = await wait_pending(queue)
        assert not received
        assert not task.done()
        queue.decide(key, True)
        status, result = await task
        assert status == 200 and result["result"]["content"][0]["text"] == "executed"
        assert len(received) == 1
        assert json.loads(received[0][1]) == payload
        assert "Authorization" not in received[0][2]


async def test_block_and_timeout_never_reach_upstream():
    async with setup_gateway(timeout=0.03) as (queue, received, client, url, _):
        task = asyncio.create_task(post(client, url, message()))
        (key,) = await wait_pending(queue)
        queue.decide(key, False)
        _, result = await task
        assert result["error"]["code"] == -32001
        _, expired = await post(client, url, message(2))
        assert "timed out" in expired["error"]["message"]
        assert not received


async def test_discovery_passes_through():
    async with setup_gateway() as (queue, received, client, url, _):
        await post(client, url, message(method="tools/list"))
        assert len(received) == 1 and not queue.pending


async def test_two_requests_can_finish_in_reverse_order():
    async with setup_gateway() as (queue, received, client, url, _):
        one = asyncio.create_task(post(client, url, message(1)))
        two = asyncio.create_task(post(client, url, message(2)))
        keys = await wait_pending(queue, 2)
        by_id = {queue.pending[k].message["id"]: k for k in keys}
        queue.decide(by_id[2], True)
        assert (await two)[1]["id"] == 2
        assert not one.done()
        queue.decide(by_id[1], False)
        assert "error" in (await one)[1]
        assert len(received) == 1


async def test_auth_origin_validation_and_bad_json():
    async with setup_gateway() as (queue, received, client, url, _):
        async with client.post(
            url + "/mcp", json=message(), headers={"Authorization": "Bearer wrong"}
        ) as response:
            assert response.status == 401
        async with client.post(
            url + "/mcp", json=message(), headers={"Origin": "https://untrusted.invalid"}
        ) as response:
            assert response.status == 403
        for payload in [[], {"method": "tools/call"}, message(request_id=True)]:
            async with client.post(url + "/mcp", json=payload) as response:
                assert response.status == 400
        async with client.post(url + "/mcp", data=b"not json") as response:
            assert response.status == 400
        assert not queue.pending and not received


async def test_streaming_sse_session_and_protocol_headers_survive():
    async def sse(request):
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "upstream-session"}
        )
        await response.prepare(request)
        await response.write(
            b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        )
        await response.write(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
        await response.write_eof()
        return response

    async with setup_gateway(response=sse) as (queue, received, client, url, _):

        async def read():
            async with client.post(
                url + "/mcp",
                json=message(),
                headers={
                    "Mcp-Session-Id": "session-a",
                    "MCP-Protocol-Version": "2025-11-25",
                    "Accept": "application/json, text/event-stream",
                },
            ) as response:
                return response.headers, await response.read()

        task = asyncio.create_task(read())
        (key,) = await wait_pending(queue)
        queue.decide(key, True)
        headers, data = await task
        assert headers["Mcp-Session-Id"] == "upstream-session"
        assert b"notifications/progress" in data and b'"result":{}' in data
        sent = {k.lower(): v for k, v in received[0][2].items()}
        assert sent["mcp-session-id"] == "session-a"
        assert sent["mcp-protocol-version"] == "2025-11-25"


async def test_duplicate_request_cannot_overwrite_pending():
    async with setup_gateway() as (queue, received, client, url, _):
        task = asyncio.create_task(post(client, url, message()))
        (key,) = await wait_pending(queue)
        _, duplicate = await post(client, url, message())
        assert "Duplicate" in duplicate["error"]["message"]
        assert list(queue.pending) == [key]
        queue.decide(key, False)
        await task
        assert not received


async def test_control_listener_rejects_browser_origin_and_wrong_token():
    queue = ApprovalQueue()
    gateway = Gateway(queue, TOKEN, TOKEN)
    async with serve(gateway.control_app()) as url, ClientSession() as client:
        async with client.post(url + "/approval", json={"message": message()}) as response:
            assert response.status == 403
        async with client.post(
            url + "/approval",
            json={"message": message()},
            headers={**AUTH, "Origin": "http://localhost"},
        ) as response:
            assert response.status == 403
        async with client.get(url + "/health", headers=AUTH) as response:
            assert (await response.json())["status"] == "ready"


async def test_all_requests_mode_holds_discovery():
    async with setup_gateway() as (queue, received, client, url, gateway):
        gateway.gate_all = True
        task = asyncio.create_task(post(client, url, message(method="tools/list")))
        (key,) = await wait_pending(queue)
        assert not received
        queue.decide(key, False)
        assert "error" in (await task)[1]
        assert not received


async def test_upstream_redirect_cannot_bypass_gateway():
    async def redirect(request):
        raise web.HTTPTemporaryRedirect("https://example.invalid/unguarded")

    async with setup_gateway(response=redirect) as (queue, received, client, url, _):
        task = asyncio.create_task(post_status(client, url, message()))
        (key,) = await wait_pending(queue)
        queue.decide(key, True)
        assert await task == 502


async def post_status(client, url, payload):
    async with client.post(url + "/mcp", json=payload) as response:
        await response.read()
        return response.status


async def test_disconnect_removes_held_http_request():
    async with setup_gateway(timeout=5) as (queue, received, client, url, _):
        task = asyncio.create_task(post(client, url, message()))
        await wait_pending(queue)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(2):
            while queue.pending:
                await asyncio.sleep(0.01)
        assert not received


async def test_execution_cannot_be_smuggled_as_notification():
    async with setup_gateway() as (_, received, client, url, _):
        payload = message()
        del payload["id"]
        assert await post_status(client, url, payload) == 400
        assert not received


async def test_compressed_request_is_rejected_instead_of_misforwarded():
    import gzip

    async with setup_gateway() as (_, received, client, url, _):
        async with client.post(
            url + "/mcp",
            data=gzip.compress(json.dumps(message()).encode()),
            headers={"Content-Encoding": "gzip"},
        ) as response:
            assert response.status == 415
        assert not received


async def test_compressed_upstream_response_is_relayed_without_double_decoding():
    import gzip

    encoded = gzip.compress(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    async def compressed(request):
        return web.Response(
            body=encoded, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}
        )

    async with setup_gateway(response=compressed) as (queue, _, _, url, _):
        async with ClientSession(headers=AUTH, auto_decompress=False) as client:

            async def read():
                async with client.post(url + "/mcp", json=message()) as response:
                    assert response.headers["Content-Encoding"] == "gzip"
                    return await response.read()

            task = asyncio.create_task(read())
            (key,) = await wait_pending(queue)
            queue.decide(key, True)
            assert await task == encoded

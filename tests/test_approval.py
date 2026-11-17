import asyncio

import pytest

from mcp_sentinel.approval import ApprovalQueue, needs_approval


def message(request_id=1, method="tools/call"):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"name": "echo", "arguments": {"message": "hello"}},
    }


async def wait_pending(queue, count=1):
    async with asyncio.timeout(3):
        while len(queue.pending) != count:
            await asyncio.sleep(0.005)
    return list(queue.pending)


@pytest.mark.parametrize("method", ["tools/call", "resources/read", "prompts/get", "unknown/write"])
def test_execution_methods_need_approval(method):
    assert needs_approval(message(method=method))


@pytest.mark.parametrize("method", ["initialize", "ping", "tools/list", "prompts/list"])
def test_discovery_is_not_held(method):
    assert not needs_approval(message(method=method))


def test_notifications_and_responses_are_not_held():
    assert not needs_approval({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert not needs_approval({"jsonrpc": "2.0", "id": 0, "result": {}})
    assert needs_approval(message(request_id=0))


async def test_concurrent_requests_resolve_independently_out_of_order():
    queue = ApprovalQueue()
    first = asyncio.create_task(queue.ask(message(1), "a", "server"))
    second = asyncio.create_task(queue.ask(message(1), "b", "server"))
    a, b = await wait_pending(queue, 2)
    queue.decide(b, True)
    assert (await second)[0]
    assert not first.done()
    queue.decide(a, False)
    assert not (await first)[0]
    assert not queue.pending
    assert not queue.decide(a, True)


async def test_timeout_fails_closed():
    queue = ApprovalQueue(timeout=0.01)
    allowed, reason = await queue.ask(message(), "a", "s")
    assert not allowed
    assert "timed out" in reason
    assert queue.history[0].state == "expired"
    assert not queue.pending


async def test_capacity_and_shutdown():
    queue = ApprovalQueue(capacity=1)
    task = asyncio.create_task(queue.ask(message(), "a", "s"))
    await wait_pending(queue)
    assert not (await queue.ask(message(2), "b", "s"))[0]
    queue.close()
    assert not (await task)[0]
    assert not (await queue.ask(message(3), "a", "s"))[0]


async def test_cancellation_cleans_pending():
    queue = ApprovalQueue()
    task = asyncio.create_task(queue.ask(message(), "a", "s"))
    await wait_pending(queue)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not queue.pending
    assert queue.history[0].state == "cancelled"


async def test_payload_is_a_snapshot():
    queue = ApprovalQueue()
    payload = message()
    task = asyncio.create_task(queue.ask(payload, "a", "s"))
    (key,) = await wait_pending(queue)
    payload["params"]["name"] = "changed"
    assert queue.pending[key].title == "echo"
    queue.decide(key, False)
    await task


def test_all_requests_policy_includes_initialization_but_not_notifications():
    assert needs_approval(message(method="initialize"), gate_all=True)
    assert needs_approval(message(method="tools/list"), gate_all=True)
    assert not needs_approval(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, gate_all=True
    )


async def test_late_allow_is_expiration_not_permission():
    queue = ApprovalQueue(timeout=10)
    task = asyncio.create_task(queue.ask(message(), "a", "s"))
    (key,) = await wait_pending(queue)
    queue.pending[key].deadline = 0
    queue.decide(key, True)
    assert not (await task)[0]
    assert queue.history[0].state == "expired"

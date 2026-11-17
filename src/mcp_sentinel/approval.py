from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Discovery must work before a client knows which tools it can call.
DISCOVERY = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "prompts/list",
    }
)


def needs_approval(message: dict, gate_all: bool = False) -> bool:
    return (
        "id" in message and "method" in message and (gate_all or message["method"] not in DISCOVERY)
    )


def rpc_error(request_id: Any, reason: str, code: int = -32001) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": reason}}


@dataclass
class Pending:
    key: str
    message: dict
    client: str
    server: str
    deadline: float
    future: asyncio.Future = field(repr=False)
    state: str = "pending"
    reason: str = "Awaiting your decision"
    created: float = field(default_factory=time.time)

    @property
    def title(self) -> str:
        return str(self.message.get("params", {}).get("name") or self.message["method"])[:120]


class ApprovalQueue:
    def __init__(
        self, timeout: float = 90, capacity: int = 64, changed: Callable[[], None] = lambda: None
    ):
        self.timeout = timeout
        self.capacity = capacity
        self.changed = changed
        self.pending: dict[str, Pending] = {}
        self.history: deque[Pending] = deque(maxlen=100)
        self.closed = False

    async def ask(self, message: dict, client: str, server: str) -> tuple[bool, str]:
        if self.closed:
            return False, "Approval service is shutting down"
        if len(self.pending) >= self.capacity:
            return False, "Approval queue is full; request was not forwarded"
        loop = asyncio.get_running_loop()
        item = Pending(
            uuid.uuid4().hex,
            copy.deepcopy(message),
            client,
            server,
            time.monotonic() + self.timeout,
            loop.create_future(),
        )
        self.pending[item.key] = item
        self.changed()
        try:
            allowed = await asyncio.wait_for(asyncio.shield(item.future), self.timeout)
            return allowed, item.reason
        except TimeoutError:
            self.decide(item.key, False, "Decision timed out; request was not forwarded", "expired")
            return False, item.reason
        except asyncio.CancelledError:
            self.decide(item.key, False, "Client disconnected or cancelled", "cancelled")
            raise
        finally:
            self.pending.pop(item.key, None)
            if not item.future.done():
                item.future.cancel()
            self.history.appendleft(item)
            self.changed()

    def decide(
        self, key: str, allow: bool, reason: str | None = None, state: str | None = None
    ) -> bool:
        item = self.pending.get(key)
        if item is None or item.future.done():
            return False
        if allow and time.monotonic() >= item.deadline:
            allow = False
            reason = "Decision timed out; request was not forwarded"
            state = "expired"
        item.state = state or ("allowed" if allow else "blocked")
        item.reason = reason or (
            "Approved by you" if allow else "Blocked by user; request was not forwarded"
        )
        item.future.set_result(allow)
        self.changed()
        return True

    def close(self):
        self.closed = True
        for key in list(self.pending):
            self.decide(key, False, "Approval service shut down", "cancelled")

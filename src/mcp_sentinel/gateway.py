from __future__ import annotations

import asyncio
import hmac
import json
import logging
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .approval import ApprovalQueue, needs_approval, rpc_error

log = logging.getLogger(__name__)
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def authenticated(request: web.Request, token: str) -> bool:
    return hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}")


def validate_message(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise web.HTTPBadRequest(text="Expected a single JSON-RPC 2.0 object")
    if "method" in message and not isinstance(message["method"], str):
        raise web.HTTPBadRequest(text="Method must be a string")
    if (
        "method" in message
        and "id" not in message
        and not message["method"].startswith("notifications/")
    ):
        raise web.HTTPBadRequest(text="MCP requests require an id")
    if "params" in message and not isinstance(message["params"], dict):
        raise web.HTTPBadRequest(text="MCP params must be an object")
    if "id" in message and (
        isinstance(message["id"], bool) or not isinstance(message["id"], (int, str))
    ):
        raise web.HTTPBadRequest(text="MCP request id must be a string or integer")
    if "method" not in message and not ({"result", "error"} & message.keys()):
        raise web.HTTPBadRequest(text="Missing method or response")


class Gateway:
    def __init__(
        self,
        queue: ApprovalQueue,
        control_token: str,
        remote_token: str,
        upstream: str | None = None,
        upstream_token: str | None = None,
        origins: tuple[str, ...] = (),
        gate_all: bool = False,
    ):
        if upstream and urlsplit(upstream).scheme not in {"http", "https"}:
            raise ValueError("Upstream must be an HTTP(S) MCP endpoint")
        self.queue = queue
        self.control_token = control_token
        self.remote_token = remote_token
        self.upstream = upstream
        self.upstream_token = upstream_token
        self.origins = origins
        self.gate_all = gate_all
        self.session: ClientSession | None = None
        self.inflight: dict[tuple[str, str], asyncio.Task] = {}

    def control_app(self) -> web.Application:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/approval", self.approval)
        return app

    def remote_app(self) -> web.Application:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_route("*", "/mcp", self.proxy)
        app.cleanup_ctx.append(self.http_session)
        return app

    async def http_session(self, app):
        timeout = ClientTimeout(total=None, connect=10, sock_read=None)
        async with ClientSession(timeout=timeout, auto_decompress=False) as session:
            self.session = session
            yield
        self.session = None

    async def health(self, request):
        if not authenticated(request, self.control_token):
            raise web.HTTPUnauthorized()
        return web.json_response({"status": "ready", "pending": len(self.queue.pending)})

    async def approval(self, request):
        if request.headers.get("Origin") or not authenticated(request, self.control_token):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        message = body.get("message")
        validate_message(message)
        if not needs_approval(message, gate_all=True):
            raise web.HTTPBadRequest(text="Only JSON-RPC requests can be approved")
        allowed, reason = await self.queue.ask(
            message,
            str(body.get("client", "Desktop / stdio"))[:160],
            str(body.get("server", "local MCP server"))[:160],
        )
        return web.json_response({"allowed": allowed, "reason": reason})

    async def proxy(self, request):
        if request.headers.get("Origin") and request.headers["Origin"] not in self.origins:
            raise web.HTTPForbidden(text="Origin is not allowed")
        if not authenticated(request, self.remote_token):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise web.HTTPUnsupportedMediaType(text="Compressed request bodies are not supported")
        if not self.upstream:
            raise web.HTTPServiceUnavailable(text="No HTTP upstream configured")
        if request.method not in {"POST", "GET", "DELETE"}:
            raise web.HTTPMethodNotAllowed(request.method, ["POST", "GET", "DELETE"])
        body = await request.read()
        key = None
        try:
            if request.method == "POST":
                try:
                    message = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise web.HTTPBadRequest(text="Invalid JSON") from None
                validate_message(message)
                if message.get("method") == "notifications/cancelled":
                    cancelled = message.get("params", {}).get("requestId")
                    task = self.inflight.get(
                        (request.headers.get("Mcp-Session-Id", ""), json.dumps(cancelled))
                    )
                    if task:
                        task.cancel()
                if needs_approval(message, self.gate_all):
                    candidate = (
                        request.headers.get("Mcp-Session-Id", ""),
                        json.dumps(message["id"]),
                    )
                    if candidate in self.inflight:
                        return web.json_response(
                            rpc_error(message["id"], "Duplicate active request id")
                        )
                    key = candidate
                    self.inflight[key] = asyncio.current_task()
                    allowed, reason = await self.queue.ask(
                        message,
                        "Remote connector / HTTP",
                        urlsplit(self.upstream).netloc,
                    )
                    if not allowed:
                        return web.json_response(rpc_error(message["id"], reason))
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in HOP_HEADERS | {"authorization", "cookie"}
            }
            if self.upstream_token:
                headers["Authorization"] = f"Bearer {self.upstream_token}"
            assert self.session is not None
            async with self.session.request(
                request.method, self.upstream, data=body, headers=headers, allow_redirects=False
            ) as upstream:
                if 300 <= upstream.status < 400:
                    raise web.HTTPBadGateway(
                        text="Upstream redirect refused; check the configured endpoint"
                    )
                response_headers = {
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower() not in HOP_HEADERS | {"set-cookie"}
                }
                response_headers["X-Accel-Buffering"] = "no"
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, TimeoutError):
            log.warning("HTTP upstream unavailable; request is not retried")
            raise web.HTTPBadGateway(text="MCP upstream unavailable; no automatic retry") from None
        finally:
            if key:
                self.inflight.pop(key, None)

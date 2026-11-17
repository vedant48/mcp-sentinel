from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys
import threading
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout

from .approval import needs_approval, rpc_error
from .gateway import validate_message

log = logging.getLogger(__name__)
MAX_LINE = 1024 * 1024


async def proxy_stdio(command: list[str], control_url: str, token: str, gate_all: bool = False):
    if not command:
        raise ValueError("An upstream command is required after --")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
        limit=MAX_LINE,
    )
    loop = asyncio.get_running_loop()
    incoming = asyncio.Queue(maxsize=128)
    stop = threading.Event()

    def read_stdin():
        while not stop.is_set():
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if loop.is_closed():
                return
            future = asyncio.run_coroutine_threadsafe(incoming.put(line), loop)
            try:
                future.result()
            except Exception:
                return
            if not line or len(line) > MAX_LINE:
                return

    threading.Thread(target=read_stdin, daemon=True, name="stdin-reader").start()
    writes = asyncio.Lock()
    pending: dict[str, asyncio.Task] = {}

    async def write_client(message):
        sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        sys.stdout.buffer.flush()

    async def write_server(line):
        async with writes:
            process.stdin.write(line)
            await process.stdin.drain()

    async def upstream_output():
        while line := await process.stdout.readline():
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    async with ClientSession(timeout=ClientTimeout(total=180)) as session:

        async def gated(message, line):
            try:
                async with session.post(
                    control_url + "/approval",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "message": message,
                        "client": "Desktop route / stdio",
                        "server": shlex.join(command).replace(str(Path.home()), "~"),
                    },
                ) as response:
                    response.raise_for_status()
                    decision = await response.json()
                if decision.get("allowed") is True:
                    await write_server(line)
                else:
                    await write_client(rpc_error(message["id"], decision.get("reason", "Blocked")))
            except asyncio.CancelledError:
                await write_client(rpc_error(message["id"], "Request cancelled", -32800))
                raise
            except Exception:
                log.warning("Approval service unavailable; refusing request")
                await write_client(
                    rpc_error(message["id"], "Approval service unavailable; not forwarded")
                )

        output_task = asyncio.create_task(upstream_output())
        input_task = asyncio.create_task(incoming.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {input_task, output_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if output_task in done:
                    await output_task
                    break
                line = input_task.result()
                if not line:
                    break
                input_task = asyncio.create_task(incoming.get())
                if len(line) > MAX_LINE:
                    await write_client(rpc_error(None, "Message exceeds 1 MiB", -32700))
                    break
                try:
                    message = json.loads(line)
                    validate_message(message)
                except Exception:
                    await write_client(rpc_error(None, "Invalid MCP message", -32700))
                    continue
                if message.get("method") == "notifications/cancelled":
                    task = pending.get(json.dumps(message.get("params", {}).get("requestId")))
                    if task:
                        task.cancel()
                if needs_approval(message, gate_all):
                    key = json.dumps(message["id"])
                    if key in pending and not pending[key].done():
                        await write_client(rpc_error(message["id"], "Duplicate active request id"))
                        continue
                    task = asyncio.create_task(gated(message, line))
                    pending[key] = task
                    task.add_done_callback(
                        lambda finished, k=key: (
                            pending.pop(k, None) if pending.get(k) is finished else None
                        )
                    )
                else:
                    await write_server(line)
        finally:
            stop.set()
            for task in [input_task, output_task, *pending.values()]:
                task.cancel()
            await asyncio.gather(input_task, output_task, *pending.values(), return_exceptions=True)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()

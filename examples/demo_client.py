"""Send real MCP calls and wait for a person to approve or block them."""

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcp_sentinel.cli import CONFIG, read_token


async def calls(session, concurrent):
    await session.initialize()
    tools = await session.list_tools()
    print("Connected. Available tools:", ", ".join(tool.name for tool in tools.tools), flush=True)

    async def call(text):
        print(f"Requesting echo({text!r}). Waiting for Sentinel...", flush=True)
        try:
            result = await session.call_tool("echo", {"message": text})
            print(f"RESULT {text}: {result.model_dump_json()}", flush=True)
        except Exception as exc:
            print(f"DENIED / ERROR {text}: {exc}", flush=True)

    if concurrent:
        await asyncio.gather(call("approve this one"), call("block this one"))
    else:
        await call("approve this one")
        await call("block this one")


async def main(args):
    if args.transport == "stdio":
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "mcp_sentinel.cli",
                "stdio",
                "--control-token-file",
                str(CONFIG / "control.token"),
                "--",
                sys.executable,
                str(Path(__file__).with_name("demo_server.py")),
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=150)
            ) as session:
                await calls(session, args.concurrent)
    else:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {read_token(CONFIG / 'remote.token')}"},
            timeout=150,
        ) as http_client:
            async with streamable_http_client(args.url, http_client=http_client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await calls(session, args.concurrent)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transport", choices=["stdio", "http"])
    parser.add_argument("--concurrent", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    asyncio.run(main(parser.parse_args()))

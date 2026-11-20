"""A side-effect-free MCP server for trying both transports."""

import argparse
import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Sentinel demo", host="127.0.0.1", port=9000, json_response=False)


@mcp.tool()
async def echo(message: str) -> str:
    """Return a message without changing anything."""
    return f"Echo: {message}"


@mcp.tool()
async def slow_echo(message: str, seconds: float = 1) -> str:
    """Wait briefly, then return a message. Useful for showing concurrency."""
    await asyncio.sleep(max(0, min(seconds, 5)))
    return f"Slow echo: {message}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    args = parser.parse_args()
    mcp.run(transport="streamable-http" if args.http else "stdio")


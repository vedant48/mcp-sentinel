from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

CONFIG = Path(os.environ.get("SENTINEL_CONFIG_DIR", Path.home() / ".config" / "mcp-sentinel"))


def read_token(path: Path, minimum: int = 32) -> str:
    token = path.read_text().strip()
    if len(token) < minimum:
        raise ValueError(f"Token in {path} must contain at least {minimum} characters")
    return token


def init_tokens():
    CONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ["control.token", "remote.token"]:
        path = CONFIG / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print(f"Already exists: {path}")
        else:
            with os.fdopen(fd, "w") as file:
                file.write(secrets.token_urlsafe(32) + "\n")
            print(f"Created: {path}")
    print("Keep both files private. Only the remote token belongs in a connector header.")


def main():
    parser = argparse.ArgumentParser(description="Sentinel: human approval for MCP")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("init", help="Create local credentials, without printing them")
    gui = sub.add_parser("run", help="Start the approval window and both local listeners")
    gui.add_argument("--upstream", help="Full URL of a Streamable HTTP MCP server")
    gui.add_argument("--upstream-token-file", type=Path)
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8766)
    gui.add_argument("--control-port", type=int, default=8765)
    gui.add_argument("--decision-timeout", type=float, default=90)
    gui.add_argument(
        "--origin", action="append", default=[], help="Explicitly allowed browser Origin"
    )
    gui.add_argument("--control-token-file", type=Path, default=CONFIG / "control.token")
    gui.add_argument("--remote-token-file", type=Path, default=CONFIG / "remote.token")
    stdio = sub.add_parser("stdio", help="Wrap a local MCP server launched by Desktop")
    stdio.add_argument("--control-url", default="http://127.0.0.1:8765")
    stdio.add_argument("--control-token-file", type=Path, default=CONFIG / "control.token")
    for command in [gui, stdio]:
        command.add_argument(
            "--gate-all",
            action="store_true",
            help="Ask for every request, including initialization and discovery",
        )
    stdio.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s")
    try:
        if options.mode == "init":
            init_tokens()
        elif options.mode == "stdio":
            from .stdio import proxy_stdio

            command = options.command[1:] if options.command[:1] == ["--"] else options.command
            asyncio.run(
                proxy_stdio(
                    command,
                    options.control_url,
                    read_token(options.control_token_file),
                    options.gate_all,
                )
            )
        else:
            from .gui import run_gui

            if not 1 <= options.decision_timeout <= 120:
                parser.error("Decision timeout must be between 1 and 120 seconds")
            options.control_token = read_token(options.control_token_file)
            options.remote_token = read_token(options.remote_token_file)
            options.upstream_token = (
                read_token(options.upstream_token_file, minimum=1)
                if options.upstream_token_file
                else None
            )
            run_gui(options)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"sentinel: {exc}\nRun 'sentinel init' before starting.\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

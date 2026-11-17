"""Print a Desktop config entry using this checkout's actual Python and paths."""

import argparse
import json
import sys
from pathlib import Path

from mcp_sentinel.cli import CONFIG

parser = argparse.ArgumentParser()
parser.add_argument("--gate-all", action="store_true")
options = parser.parse_args()
args = ["-m", "mcp_sentinel.cli", "stdio", "--control-token-file", str(CONFIG / "control.token")]
if options.gate_all:
    args.append("--gate-all")
args.extend(
    ["--", sys.executable, str(Path(__file__).resolve().parents[1] / "examples/demo_server.py")]
)
print(
    json.dumps(
        {"mcpServers": {"sentinel-demo": {"command": sys.executable, "args": args}}}, indent=2
    )
)

# Demo recording script

Target length: 4-6 minutes. Record the actual application windows and a terminal with readable text. Do a rehearsal before recording. Keep credential files, authentication screens, personal chats, and unrelated browser tabs off screen.

## Before recording

1. Install the project with the demo dependencies:

   ```bash
   python -m pip install -e ".[dev]"
   sentinel init
   ```

2. In terminal A, start the harmless example upstream:

   ```bash
   python examples/demo_server.py --http
   ```

3. In terminal B, start the gateway:

   ```bash
   sentinel run --upstream http://127.0.0.1:9000/mcp
   ```

4. Configure the Desktop entry using `python scripts/desktop_config.py`, merge it into Desktop's MCP configuration, and restart Desktop.
5. Set up the authenticated HTTPS tunnel and Claude Web connector using the README. Do this before recording so the token never appears in the video.
6. Rehearse one call from each actual Claude client. If a client or fixed-header authentication is unavailable, use the clearly labeled SDK fallback below. Do not call it a tested Claude client.
7. Check that the popup appears above an ordinary terminal window on your OS. If the OS suppresses foreground focus, keep Sentinel beside the client and describe that limitation instead of claiming it always steals focus.

## 0:00-0:35 | Explain the boundary

**Show:** Sentinel's ready dashboard, then the architecture diagram or the first section of `ARCHITECTURE.md`.

**Say, in your own words:**

"This is Sentinel, a Python gateway for MCP requests. Desktop connects through a stdio wrapper. The Web route connects to an HTTP endpoint through an HTTPS tunnel. Both routes wait for a decision in this native window before forwarding an execution request. This is an explicit proxy, not a packet sniffer."

"In the normal mode, initialization and tool discovery pass through. Execution requests need approval. There is also an all-requests mode that includes initialization and discovery."

## 0:35-1:35 | Desktop: approve one real call

**Show:** Claude Desktop with the Sentinel tool enabled. Put a terminal in front of Sentinel so the popup behavior can be seen.

**Type in Claude Desktop:**

> Use sentinel-demo's echo tool once with the message "desktop approved". Do not substitute a plain-text answer for the tool call.

If Claude presents its own tool permission, accept that first.

**When Sentinel opens:**

1. Do not click for 3-5 seconds.
2. Show the method `tools/call`, tool name `echo`, and the exact message argument.
3. Point to **HELD / Not forwarded** and the countdown.
4. Click **Allow once**.
5. Show the client receiving `Echo: desktop approved`.

**Say:**

"The request was held before the server call. Allow releases only this request. The result comes back through the same MCP connection."

## 1:35-2:15 | Desktop: block the next call

**Type:**

> Call sentinel-demo's echo tool once with the message "desktop blocked". If the request is denied, do not retry it.

1. Inspect the popup's different argument.
2. Click **Block request**.
3. Show the denial in the client and the blocked history entry in Sentinel.

**Say:**

"This call was denied before forwarding. The gateway returns a JSON-RPC error with the original request ID. It does not call the upstream and hide the result afterward."

Claude can paraphrase a tool error or offer a retry. Show the recorded block in Sentinel too; do not imply a model's wording is protocol evidence.

## 2:15-3:15 | Web: demonstrate the separate transport

**Show:** claude.ai with the custom connector enabled. Show only the connector's name and HTTPS endpoint if needed, never its authentication header.

**Type:**

> Use the Sentinel connector's echo tool once with the message "web approved".

1. Show the local popup labeled **Remote connector / HTTP**.
2. Click **Allow once** and show the echo result in Claude Web.
3. Ask for `web blocked`, explicitly asking Claude not to retry if denied.
4. Click **Block request** and show the blocked history entry.

**Say:**

"Claude Web's MCP connection originates in the cloud. This tunnel reaches only the HTTP data port. The decision is still made on this computer; the private control port is not exposed."

## 3:15-4:00 | Concurrency, with exact results

**Show:** a terminal in the project environment. This is an SDK test client, not Claude.

```bash
python examples/demo_client.py stdio --concurrent
```

1. Show two pending `echo` requests with different IDs and arguments.
2. Hide the popup briefly if needed to show the dashboard queue.
3. Select the request whose argument says `block this one` and block it from the dashboard, even if it arrived second.
4. Show its denial in the terminal while the other request remains pending.
5. Select `approve this one` and click **Allow once**.
6. Show its `Echo: approve this one` result.

**Say:**

"Each request waits on its own future. Blocking this one does not resolve the other one. They can finish out of order, and the request ID is preserved."

Optionally repeat with `python examples/demo_client.py http --concurrent` to show the same behavior through HTTP.

## 4:00-4:40 | Tests and honest limits

**Show:** stop the manual example server in terminal A first, then run:

```bash
pytest -q
ruff check .
```

Briefly show the test files or test output.

**Say:**

"The tests cover both real transports with the official MCP SDK, request isolation, no execution after a block, timeouts, cancellation, authentication, Origin validation, and the popup's behavior."

"This guards the configured path only. A direct upstream connection would bypass it. The Web demo needs a reachable HTTPS endpoint and fixed-header authentication. The topmost flag is subject to OS focus rules. Once an approved operation has started, cancellation cannot promise to undo it."

Finish on the dashboard with allowed and blocked entries visible.

## SDK fallback when an actual client cannot be exercised

Use the same approval/blocking sequence, but make the source explicit:

```bash
python examples/demo_client.py stdio
python examples/demo_client.py http
```

Say:

"This is the official MCP Python SDK exercising the Desktop stdio path / Web HTTP path. I have not verified the actual Claude Desktop / Claude Web client in this recording. The repository contains its connection setup and the remaining limitations."

Do not label an SDK window as Claude, edit the video to imply an unperformed client test, or mark an untested OS/client as tested in the submission form.

## Optional strict-mode clip

To demonstrate literal approval of every request, restart the HTTP adapter with `--gate-all` and run the HTTP SDK client. Approve initialization, approve tool listing, then allow or block the tool call. For Desktop, generate the config with `python scripts/desktop_config.py --gate-all` and restart Desktop.

This is optional because it adds protocol setup prompts to the recording. State which policy is running.

## After recording

- Stop the tunnel and the demo servers.
- Watch the video once with audio to check that the popup, arguments, clicked decision, and resulting client state are all visible.
- Ensure the repository and video are accessible to the intended reviewer before submitting their links.
- In the form, list only the clients and operating systems actually exercised.

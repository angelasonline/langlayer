# Operating Language Layer with agents (Goose, Claude, any MCP client)

Language Layer works with AI agents through an MCP server (mcp_server.py). 
Any MCP-capable agent can create spaces, manage multilingual communication,
and verify delivery without changing the underlying engine.

Tools: `get_health`, `get_coverage`, `create_space`, `get_space`,
`get_attendees`, `send_announcement`, `get_summary`, `get_transcript`.
Only `send_announcement` has real-world effect, and its description warns
the agent accordingly.

## Setup

    pip install -r requirements-mcp.txt

Environment:
- `LL_BASE_URL` — the deployment to operate (default `http://localhost:8000`;
  use `https://langlayer.onrender.com` for the cloud instance, or the venue
  box's LAN address for offline drills)
- `LL_INVITE_CODE` — the host invite code; required only by `create_space`.
 Keep execution and context in the environment, never in the chat.

## Goose

Add the extension (Settings → Extensions → Add, or config):

    extensions:
      language-layer:
        type: stdio
        cmd: python
        args: ["/path/to/langlayer/mcp_server.py"]
        envs:
          LL_BASE_URL: "https://langlayer.onrender.com"
          LL_INVITE_CODE: "<host invite code>"

Then: `goose session` and ask it to create a space and send an
announcement or run the shipped recipes:

    goose run --recipe recipes/offline-drill.yaml
    goose run --recipe recipes/nightly-health.yaml

Scheduling (goose automations):

    goose schedule add --id ll-nightly --cron "0 6 * * *" \
      --recipe recipes/nightly-health.yaml

## Claude Desktop

`claude_desktop_config.json`:

    {
      "mcpServers": {
        "language-layer": {
          "command": "python",
          "args": ["/path/to/langlayer/mcp_server.py"],
          "env": {
            "LL_BASE_URL": "https://langlayer.onrender.com",
            "LL_INVITE_CODE": "<host invite code>"
          }
        }
      }
    }

## Security notes

- The invite code gates space creation only; treat it like a password.
- Announcements reach real connected attendees. Agents are warned in the
  tool description; humans wiring agents should confirm intent policies
  (e.g., Goose's approval mode) before granting `send_announcement`.
- Allow agents read broadly. Control what agents can change.

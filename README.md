# road-scout MCP server

Read-only local MCP server for the Open Minis road-trip scout. It keeps the
logged-in Chrome session on the Mac and exposes only high-level search tools.

## Local run

```bash
cp .env.example .env
uv run python server.py
```

Health check:

```bash
curl http://127.0.0.1:18787/health
```

The MCP endpoint is `http://127.0.0.1:18787/mcp`.

For a persistent local process, copy the example LaunchAgent into
`~/Library/LaunchAgents/`, then load it with `launchctl bootstrap gui/$(id -u)`.

Before social searches, connect the OpenCLI Chrome extension and verify:

```bash
opencli doctor
opencli xiaohongshu whoami -f json
opencli bilibili whoami -f json
```

No cookie is exported by this project. The MCP server invokes read-only
OpenCLI commands, and it never exposes arbitrary shell execution or social
write actions.

## Open Minis configuration

Use the Mac's Tailscale/private HTTPS address once configured:

```json
{
  "mcpServers": {
    "road-scout": {
      "url": "https://<mac-private-name>/mcp",
      "headers": {"Authorization": "Bearer $$ROAD_SCOUT_MCP_TOKEN"}
    }
  }
}
```

Keep the token in the Open Minis environment and in the Mac's local `.env`;
never put it in a skill prompt or a source file.

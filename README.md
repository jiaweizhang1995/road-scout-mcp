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

## Search result contract

`social_search` uses one source registry for direct searches and
`nearby_discover`. `sources` omitted means `xiaohongshu`, `bilibili`, and
`web`; an explicit empty list or an unknown source is a parameter error. Query
whitespace is trimmed, queries must be 1–500 characters, and duplicate sources
are de-duplicated in first-seen order. The supported source names are
`xiaohongshu`, `bilibili`, `douyin`, and `web`.

Each source result has a `status` of `ok`, `empty`, `partial`, `unavailable`, or
`parse_error`, plus `source`, `data`, and a structured `error` when applicable.
Adapters may explicitly return `status: "partial"`; the aggregate search status
preserves that partial result for combinations with successful, empty, or failed
sources. OpenCLI search results must be an array or a documented list wrapper;
the Exa MCP result must contain MCP `content` blocks. Unexpected JSON shapes are
reported as `parse_error`.
The legacy boolean `ok` is retained for clients that still read it; it is true
for `ok`, `empty`, and `partial`. A successful process with an MCP
`isError: true` response is `unavailable`, while malformed JSON or a scalar
JSON value is `parse_error`. Text commands such as `road_scout_status` keep
their diagnostic text and are not forced through JSON parsing.

The local Exa schema was inspected with `mcporter list exa --schema --json`.
The redacted fixture is [docs/contracts/exa-web-search.schema.json](docs/contracts/exa-web-search.schema.json);
the adapter sends only its confirmed `query`, `numResults`, and required
`objective` fields.

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

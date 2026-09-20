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

## Xiaohongshu note content

`xiaohongshu_note(note_url)` calls the locally installed
`opencli xiaohongshu note <note-url> -f json` command. The URL must be the
complete signed URL returned by search, including `xsec_token`; the server
never reconstructs it from a note ID. OpenCLI currently returns field/value
rows, including `content`, `title`, `author`, `likes`, `collects`, and
`comments`.

Search results are converted into small Jev candidates and de-duplicated by
the note identity in the original URL (so different signed tokens for the
same note do not repeat a body read). Their `text` remains empty until `xiaohongshu_note` reads
the body. A note read failure returns `unavailable` with an empty `text`; a
title is never copied into the body field.

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

## High-level recommend flow

`road_scout_recommend(request, area_name, categories, preferences, include_food,
max_results, radius_km, latitude, longitude)` is the single-call entry point. It
generates a few plain queries (`<area> 周边 <category>` plus one
`<area> <category> 实际体验`), searches all four sources, deduplicates candidates
across queries (Xiaohongshu by note identity, so a signed search hit wins over
an unsigned copy), reads note bodies for the top ~10 preselected candidates,
runs `jev_rank_candidates`, and only then does at most one targeted follow-up
(XHS comments or one `<place> 停车 门票 营业` web query) for promising candidates
that still lack practical details like parking, tickets, or opening hours.
Followed-up candidates are re-ranked once.

Distance constraints: `radius_km` caps how far recommendations may be, or the
same limit is parsed from `request` ("附近100公里", "500米内"; the explicit
parameter wins). The origin is `latitude`/`longitude` (declared WGS-84;
converted to GCJ-02 automatically when `AMAP_API_KEY` is set so it compares
like-for-like with Amap coordinates) when both are given, otherwise
`area_name` is geocoded — via Amap `geocode/geo` when `AMAP_API_KEY` is set,
else Nominatim with a display-name sanity check. `distance_km` is an
approximate straight-line distance for range filtering, not actual driving
mileage. Niche places may not resolve to coordinates at all — they keep
`distance_km: null` ("距离未知") and are never filtered out; Amap's
city-or-coarser fallback levels are treated as unknown too, since the area
centroid would produce a wrongly small distance. Nominatim calls are
throttled to the OSMF public-service limit of 1 request/second and the `geo`
output carries attribution; data © OpenStreetMap contributors.
Ranked candidates are deduplicated by place (multiple notes about one place
collapse into a single entry with `mentions`), annotated with haversine
distance, and anything known to be beyond the radius is moved to `exploratory`
with the distance noted in `risks`. Out-of-range candidates also skip the
follow-up step — no point fetching comments for a place too far away.

The result is `recommendations` (`supported`/`marketing_risk`, capped by
`max_results`, never padded, each with `distance_km`), a small `exploratory`
list for `insufficient` or out-of-range leads, an optional `food` section from
the Amap city ranking when the request is about eating (with a simple
parent-city fallback for inputs such as `广州番禺`), a `geo` section
(reporting the resolved origin, radius, and geocoder used), per-source
`source_status`, and `notes`. Low-ranking candidates stay in `exploratory`.
A failed source never fails the whole call.
Non-Xiaohongshu candidates rely on search snippets and are marked as weaker
evidence in `risks`.

## Jev ranking contract

`jev_rank_candidates` sends the Issue #3 candidate `text` and available comment
evidence to Jev for three independent signals: `firsthand`, `marketing`, and
`fit`. Explicit `user_preferences` are passed through to `fit`; defaults are
used only when no preferences are supplied. Short or unavailable bodies become
`evidence_status: "insufficient"` and are not forced into a low-quality verdict.

The simple code-side ranking keeps detailed first-hand candidates near the top,
filters high-marketing/low-firsthand candidates, and retains high-marketing
content when it still contains useful route, parking, price, or limitation
details. Likes are not used as an automatic filter. Jev outputs are ranking
signals, not identity verification or factual proof.

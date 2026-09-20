#!/bin/zsh
set -euo pipefail
cd /Users/jimmymacmini/workspace/sandbox/road-scout-mcp
exec /Users/jimmymacmini/.local/bin/uv run python server.py

"""Read-only MCP server for nearby road-trip discovery.

The server keeps platform credentials on the Mac and exposes only high-level
read tools to a remote Open Minis client.  It intentionally does not expose
shell, posting, commenting, liking, or cookie-management tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from bs4 import BeautifulSoup
from pypinyin import lazy_pinyin
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn


load_dotenv()

OPENCLI = os.getenv("OPENCLI_BIN", "opencli")
AGENT_REACH = os.getenv("AGENT_REACH_BIN", "agent-reach")
MCP_HOST = os.getenv("ROAD_SCOUT_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("ROAD_SCOUT_PORT", "18787"))
MCP_TOKEN = os.getenv("ROAD_SCOUT_MCP_TOKEN", "")
COMMAND_TIMEOUT = float(os.getenv("ROAD_SCOUT_COMMAND_TIMEOUT", "90"))
MAX_QUERY_LENGTH = 500
DEFAULT_SOURCES = ("xiaohongshu", "bilibili", "web")
SUCCESS_STATUSES = {"ok", "empty", "partial"}
FAILURE_STATUSES = {"unavailable", "parse_error"}
DEFAULT_USER_PREFERENCES = ("小众", "人少", "本地体验", "适合自驾")
MIN_JEV_TEXT_LENGTH = 20
RECOMMEND_SOURCES = ("xiaohongshu", "bilibili", "douyin", "web")
DEFAULT_RECOMMEND_CATEGORIES = ("山野", "民宿", "本地体验")
MAX_SEARCH_QUERIES = 4
SEARCH_LIMIT_PER_SOURCE = 10
MAX_RANKED_CANDIDATES = 10
MAX_FOLLOWUP_CANDIDATES = 3
FOLLOWUP_COMMENT_LIMIT = 15
MAX_EXPLORATORY = 3
FOOD_KEYWORDS = ("吃", "餐", "美食", "饭")
# A candidate missing every one of these topics is treated as having a
# practical-information gap worth one targeted follow-up.
PRACTICAL_KEYWORDS = (
    "停车", "门票", "预约", "排队", "营业", "闭店", "路况",
    "收费", "价格", "费用", "踩坑", "限行",
)
EVIDENCE_KEYWORDS = PRACTICAL_KEYWORDS + (
    "路线", "公里", "小时", "分钟", "信号", "人多", "人少", "导航", "泥", "滑",
)
AMAP_API_KEY = os.getenv("AMAP_API_KEY", "")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEO_TIMEOUT = 15

mcp = FastMCP(
    "road-scout",
    instructions=(
        "Read-only nearby travel research. Search social and public web sources, "
        "return evidence and uncertainty, and never perform account mutations."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
)


async def run_command(
    *args: str,
    timeout: float = COMMAND_TIMEOUT,
    expect_json: bool = False,
) -> dict[str, Any]:
    """Run one allow-listed command, keeping process and parse outcomes separate.

    Text diagnostics (for example ``doctor``) are intentionally not parsed as
    JSON. Search adapters pass ``expect_json=True`` so malformed JSON cannot be
    reported as a successful search.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "ok": False,
            "process_ok": False,
            "exit_code": None,
            "command": list(args),
            "data": None,
            "parse_error": None,
            "error": {"code": "command_timeout", "message": "command timed out"},
        }

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    parsed: Any = out
    parse_error: dict[str, Any] | None = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            if expect_json:
                parse_error = {"code": "invalid_json", "message": "stdout is not valid JSON"}
    elif expect_json:
        parse_error = {"code": "invalid_json", "message": "stdout is empty"}
    process_ok = proc.returncode == 0
    ok = process_ok and parse_error is None
    return {
        "ok": ok,
        "process_ok": process_ok,
        "exit_code": proc.returncode,
        "command": list(args),
        "data": parsed,
        "parse_error": parse_error,
        "stderr": err[-4000:] if err else None,
    }


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _data_is_empty(data: Any) -> bool:
    if isinstance(data, list):
        return not data
    if isinstance(data, dict):
        for key in ("results", "items", "data", "content"):
            value = data.get(key)
            if isinstance(value, list):
                return not value
    return False


def _has_valid_shape(source: str, data: Any) -> bool:
    """Check only the stable outer shape promised by each local adapter."""
    if source in {"xiaohongshu", "bilibili", "douyin"}:
        # OpenCLI -f json search commands return a result array. Some versions
        # wrap it in a documented results/items/data list; reject arbitrary
        # objects so diagnostics cannot be mistaken for search results.
        return isinstance(data, list) or (
            isinstance(data, dict)
            and any(isinstance(data.get(key), list) for key in ("results", "items", "data"))
        )
    if source in {"web", "exa"}:
        # mcporter's MCP result is an object with content blocks. isError is
        # handled separately, but a normal result still needs content.
        return isinstance(data, dict) and isinstance(data.get("content"), list)
    return False


def _candidate_id(note_url: str) -> str:
    match = re.search(
        r"/(?:explore|note|search_result|discovery/item|user/profile/[^/]+)/([a-f0-9]+)(?:[/?#]|$)",
        urlsplit(note_url).path,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).lower()
    return hashlib.sha256(note_url.encode("utf-8")).hexdigest()[:16]


def _xhs_note_url(note_url: str) -> str:
    """Validate, but never rewrite, a signed URL returned by search."""
    cleaned = note_url.strip()
    parsed = urlsplit(cleaned)
    host = (parsed.hostname or "").lower()
    supported_path = re.search(
        r"^/(?:explore|note|search_result|discovery/item)/[a-f0-9]+(?:/|$)|^/user/profile/[^/]+/[a-f0-9]+(?:/|$)",
        parsed.path,
        re.IGNORECASE,
    )
    token = parse_qs(parsed.query).get("xsec_token", [""])[0].strip()
    if (
        parsed.scheme not in {"http", "https"}
        or not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
        or supported_path is None
        or not token
    ):
        raise ValueError("note_url must be the full signed Xiaohongshu URL returned by search")
    return cleaned


def _public_url(url: Any) -> Any:
    """Rewrite an outbound Xiaohongshu note link to the phone-openable form.

    Search-state URLs (``/search_result/<id>?xsec_token=...&xsec_source=``)
    render as "页面不见了" outside the logged-in session.  The public shape is
    ``https://www.xiaohongshu.com/discovery/item/<id>?xsec_token=<token>&xsec_source=pc_search``
    with the token kept verbatim (its ``=`` padding URL-encoded as ``%3D``).
    Non-Xiaohongshu links, non-note Xiaohongshu links, and anything that cannot
    be converted are returned unchanged.  Internal reads and validation keep
    using the original URL.
    """
    if not isinstance(url, str) or not url.strip():
        return url
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host != "xiaohongshu.com" and not host.endswith(".xiaohongshu.com"):
        return url
    match = re.search(
        r"/(?:explore|note|search_result|discovery/item)/([a-f0-9]+)(?:/|$)",
        parts.path,
        re.IGNORECASE,
    )
    if not match:
        return url
    token = ""
    for pair in parts.query.split("&"):
        if pair.startswith("xsec_token="):
            token = unquote(pair.split("=", 1)[1]).strip()
            break
    if not token:
        return url
    return (
        f"https://www.xiaohongshu.com/discovery/item/{match.group(1).lower()}"
        f"?xsec_token={quote(token, safe='')}&xsec_source=pc_search"
    )


def _publicize_xhs_urls(data: Any) -> None:
    """Rewrite note URLs inside a normalized Xiaohongshu result list in place."""
    for row in _xhs_search_rows(data):
        if isinstance(row.get("url"), str):
            row["url"] = _public_url(row["url"])
        raw = row.get("raw_search_result")
        if isinstance(raw, dict) and isinstance(raw.get("url"), str):
            raw["url"] = _public_url(raw["url"])


def _xhs_search_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def dedupe_xiaohongshu_search_results(search_results: list[Any]) -> list[dict[str, Any]]:
    """Turn one or more OpenCLI search arrays into unique Jev candidates."""
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for result in search_results:
        for row in _xhs_search_rows(result):
            url = row.get("url") or row.get("note_url")
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            candidate_id = _candidate_id(url)
            if candidate_id in seen_ids:
                continue
            seen_ids.add(candidate_id)
            title = row.get("title") or row.get("name") or ""
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "title": title,
                    "name": title,
                    "source": "xiaohongshu",
                    "url": url,
                    "author": row.get("author") or "",
                    "text": "",
                    "comments": row.get("comments"),
                    "likes": row.get("likes"),
                    "collects": row.get("collects"),
                    "published_at": row.get("published_at"),
                    "body_status": "not_read",
                    "raw_search_result": row,
                }
            )
    return candidates


def dedupe_xiaohongshu_candidates(candidate_lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Deduplicate already-normalized candidates while preserving first-seen order."""
    unique: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidates in candidate_lists:
        for candidate in candidates:
            url = candidate.get("url")
            candidate_id = candidate.get("candidate_id")
            if not isinstance(url, str) or not url:
                continue
            key = candidate_id if isinstance(candidate_id, str) and candidate_id else _candidate_id(url)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            unique.append(candidate)
    return unique


def dedupe_xiaohongshu_evidence(evidence: list[dict[str, Any]]) -> None:
    """Remove repeated Xiaohongshu candidates across nearby query groups."""
    seen_ids: set[str] = set()
    for query_evidence in evidence:
        result = query_evidence.get("results", {}).get("xiaohongshu", {})
        candidates = result.get("data") if isinstance(result, dict) else None
        if not isinstance(candidates, list):
            continue
        unique = dedupe_xiaohongshu_candidates([candidates])
        kept: list[dict[str, Any]] = []
        for candidate in unique:
            url = candidate.get("url")
            candidate_id = candidate.get("candidate_id")
            if not isinstance(url, str) or not url:
                continue
            key = candidate_id if isinstance(candidate_id, str) and candidate_id else _candidate_id(url)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            kept.append(candidate)
        result["data"] = kept


def _note_rows_to_mapping(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, list):
        return None
    mapping: dict[str, Any] = {}
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("field"), str):
            return None
        mapping[row["field"]] = row.get("value")
    return mapping


def _empty_xhs_candidate(note_url: str) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(note_url),
        "title": "",
        "name": "",
        "source": "xiaohongshu",
        "url": note_url,
        "author": "",
        "text": "",
        "comments": None,
        "likes": None,
        "collects": None,
        "published_at": None,
        "body_status": "unavailable",
    }


def _xiaohongshu_note_result(note_url: str, raw: dict[str, Any]) -> dict[str, Any]:
    candidate = _empty_xhs_candidate(note_url)
    if not raw.get("process_ok", raw.get("ok", False)):
        return {
            "status": "unavailable",
            "source": "xiaohongshu",
            "candidate": candidate,
            "data": raw.get("data"),
            "error": raw.get("error") or _error("command_failed", "note command failed"),
            "ok": False,
        }
    if raw.get("parse_error"):
        return {
            "status": "parse_error",
            "source": "xiaohongshu",
            "candidate": candidate,
            "data": raw.get("data"),
            "error": raw["parse_error"],
            "ok": False,
        }
    fields = _note_rows_to_mapping(raw.get("data"))
    if fields is None:
        return {
            "status": "parse_error",
            "source": "xiaohongshu",
            "candidate": candidate,
            "data": raw.get("data"),
            "error": _error("invalid_shape", "xiaohongshu note returned unexpected field rows"),
            "ok": False,
        }
    candidate.update(
        {
            "title": fields.get("title") or "",
            "name": fields.get("title") or "",
            "author": fields.get("author") or "",
            "comments": fields.get("comments"),
            "likes": fields.get("likes"),
            "collects": fields.get("collects"),
            "text": fields.get("content") or "",
            "body_status": "ok" if fields.get("content") else "unavailable",
            "raw_note": raw.get("data"),
        }
    )
    if not fields.get("content"):
        return {
            "status": "unavailable",
            "source": "xiaohongshu",
            "candidate": candidate,
            "data": raw.get("data"),
            "error": _error("missing_content", "xiaohongshu note returned no body content"),
            "ok": False,
        }
    return {
        "status": "ok",
        "source": "xiaohongshu",
        "candidate": candidate,
        "data": raw.get("data"),
        "error": None,
        "ok": True,
    }


def normalize_adapter_result(source: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a command/MCP response to the public source result contract."""
    process_ok = raw.get("process_ok", raw.get("ok", False))
    parse_error = raw.get("parse_error")
    data = raw.get("data")
    error = raw.get("error")
    requested_status = raw.get("status")
    if not process_ok:
        if not isinstance(error, dict):
            error = _error(
                "command_failed",
                str(error) if error else "adapter process exited unsuccessfully",
                exit_code=raw.get("exit_code"),
            )
        status = "unavailable"
    elif parse_error:
        status = "parse_error"
        error = parse_error
    elif isinstance(data, dict) and data.get("isError") is True:
        status = "unavailable"
        error = _error("mcp_is_error", "MCP tool reported a business failure", content=data.get("content"))
    elif not _has_valid_shape(source, data):
        status = "parse_error"
        error = _error("invalid_shape", f"{source} adapter returned an unexpected JSON shape")
    else:
        if source == "xiaohongshu":
            data = dedupe_xiaohongshu_search_results([data])
        status = requested_status if requested_status in SUCCESS_STATUSES else (
            "empty" if _data_is_empty(data) else "ok"
        )
        error = None
    return {
        "status": status,
        "source": source,
        "data": data,
        "error": error,
        # Compatibility for clients that only know the old boolean field.
        "ok": status in SUCCESS_STATUSES,
        "exit_code": raw.get("exit_code"),
        "stderr": raw.get("stderr"),
        "command": raw.get("command"),
    }


def _validate_query(query: str) -> str:
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("query must not be empty")
    if len(cleaned) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
    return cleaned


def _select_sources(sources: list[str] | None) -> list[str]:
    if sources is None:
        selected = list(DEFAULT_SOURCES)
    elif not sources:
        raise ValueError("sources must not be empty")
    else:
        selected = list(dict.fromkeys(sources))
    unknown = [source for source in selected if source not in SOURCE_NAMES]
    if unknown:
        raise ValueError(f"unknown source: {unknown[0]}")
    return selected


async def search_xiaohongshu(query: str, limit: int) -> dict[str, Any]:
    return await run_command(
        OPENCLI,
        "xiaohongshu",
        "search",
        query,
        "--limit",
        str(limit),
        "-f",
        "json",
        expect_json=True,
    )


async def search_bilibili(query: str, limit: int) -> dict[str, Any]:
    return await run_command(
        OPENCLI,
        "bilibili",
        "search",
        query,
        "--limit",
        str(limit),
        "-f",
        "json",
        expect_json=True,
    )


async def search_douyin(query: str, limit: int) -> dict[str, Any]:
    return await run_command(
        OPENCLI,
        "douyin",
        "search",
        query,
        "--limit",
        str(limit),
        "-f",
        "json",
        expect_json=True,
    )


async def fetch_xiaohongshu_note(note_url: str) -> dict[str, Any]:
    return await run_command(
        OPENCLI,
        "xiaohongshu",
        "note",
        note_url,
        "-f",
        "json",
        expect_json=True,
    )


async def fetch_xiaohongshu_comments(note_url: str, limit: int, with_replies: bool) -> dict[str, Any]:
    """Fetch comments for one Xiaohongshu note URL using the logged-in OpenCLI session."""
    return await run_command(
        OPENCLI,
        "xiaohongshu",
        "comments",
        note_url,
        "--limit",
        str(limit),
        "--with-replies",
        str(with_replies).lower(),
        "-f",
        "json",
        expect_json=True,
    )


async def fetch_douyin_creator_comments(sec_uid: str, limit: int, comment_limit: int) -> dict[str, Any]:
    """Fetch top comments from a creator's recent videos.

    OpenCLI currently exposes Douyin comments through user-videos, rather than
    a direct arbitrary-video comments command.  Keep that limitation explicit
    in the MCP surface instead of pretending search results include comments.
    """
    return await run_command(
        OPENCLI,
        "douyin",
        "user-videos",
        sec_uid,
        "--limit",
        str(limit),
        "--with_comments",
        "true",
        "--comment_limit",
        str(comment_limit),
        "-f",
        "json",
        expect_json=True,
    )


async def search_web(query: str, limit: int) -> dict[str, Any]:
    args = build_exa_search_args(query, limit)
    return await run_command(*args, expect_json=True)


def build_exa_search_args(query: str, limit: int) -> list[str]:
    """Build arguments from the locally inspected Exa MCP contract.

    The fixture in ``docs/contracts/exa-web-search.schema.json`` records the
    schema observed on this machine.  Keep the MCP content blocks in ``data``;
    parsing them into search records belongs to a later issue.
    """
    return [
        "mcporter",
        "call",
        "exa.web_search_exa",
        "--args",
        json.dumps(
            {
                "query": query,
                "numResults": limit,
                "objective": "Find current local travel, food, lodging, and route evidence; prefer first-hand and local sources.",
            },
            ensure_ascii=False,
        ),
        "--output",
        "json",
        "--timeout",
        str(int(COMMAND_TIMEOUT * 1000)),
    ]


SourceSearch = Callable[[str, int], Awaitable[dict[str, Any]]]
SOURCE_REGISTRY: dict[str, SourceSearch] = {
    # Wrappers resolve the current function at call time, which keeps the
    # registry patchable for offline adapter tests without a second dispatch
    # table in callers.
    "xiaohongshu": lambda query, limit: search_xiaohongshu(query, limit),
    "bilibili": lambda query, limit: search_bilibili(query, limit),
    "douyin": lambda query, limit: search_douyin(query, limit),
    "web": lambda query, limit: search_web(query, limit),
}
SOURCE_NAMES = tuple(SOURCE_REGISTRY)


def _aggregate_status(results: dict[str, dict[str, Any]]) -> str:
    statuses = [result["status"] for result in results.values()]
    if not statuses:
        return "unavailable"
    if any(status == "partial" for status in statuses):
        return "partial"
    has_failure = any(status in FAILURE_STATUSES for status in statuses)
    has_success = any(status in {"ok", "empty"} for status in statuses)
    if has_failure:
        return "partial" if has_success else "unavailable"
    if all(status == "empty" for status in statuses):
        return "empty"
    return "ok"


async def dispatch_sources(query: str, sources: list[str], limit: int) -> dict[str, dict[str, Any]]:
    """Dispatch registered adapters and retain independent failures."""
    jobs = [(source, SOURCE_REGISTRY[source](query, limit)) for source in sources]
    raw_results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
    normalized: dict[str, dict[str, Any]] = {}
    for (source, _), raw in zip(jobs, raw_results):
        if isinstance(raw, Exception):
            normalized[source] = normalize_adapter_result(
                source,
                {
                    "process_ok": False,
                    "error": _error("adapter_exception", str(raw)),
                },
            )
        else:
            normalized[source] = normalize_adapter_result(source, raw)
    return normalized


def city_to_slug(city_name: str) -> str:
    """Turn a Chinese city name into the Amap ranking URL slug."""
    cleaned = city_name.strip().replace("市", "").replace("地区", "")
    return "".join(lazy_pinyin(cleaned)).lower()


async def fetch_gaode_food_ranking(city_name: str, limit: int) -> dict[str, Any]:
    """Read the public Amap city food ranking page (榜单, not ad search)."""
    slug = city_to_slug(city_name)
    url = f"https://www.amap.com/ranking/{slug}/food"
    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": "road-scout/0.1"},
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else url
        items: list[dict[str, Any]] = []
        for position, card in enumerate(soup.select(".poi-card")[:limit], 1):
            name_node = card.select_one(".poi-name")
            if not name_node:
                continue
            details = [node.get_text(" ", strip=True) for node in card.select(".poi-detail-item")]
            score = next((d for d in details if "综合分" in d), None)
            tags = next((d for d in details if d.startswith("🏷️")), None)
            highlight = next((d for d in details if d.startswith("💡")), None)
            href = card.get("href")
            items.append(
                {
                    "rank": position,
                    "name": name_node.get_text(" ", strip=True),
                    "score": score,
                    "tags": tags,
                    "highlight": highlight,
                    "url": f"https://www.amap.com{href}" if href else None,
                }
            )
        return {"ok": True, "city": city_name, "city_slug": slug, "url": url, "title": title, "items": items}
    except Exception as exc:
        return {"ok": False, "city": city_name, "city_slug": slug, "url": url, "error": str(exc)}


@mcp.tool()
async def road_scout_status() -> dict[str, Any]:
    """Check local tool availability and browser-session connectivity."""
    checks: dict[str, Any] = {
        "opencli_installed": shutil.which(OPENCLI) is not None,
        "agent_reach_installed": shutil.which(AGENT_REACH) is not None,
        "mcporter_installed": shutil.which("mcporter") is not None,
        "jev_configured": bool(os.getenv("TYPESAFE_API_KEY")),
    }
    if checks["opencli_installed"]:
        checks["opencli_doctor"] = await run_command(OPENCLI, "doctor", timeout=30)
    if checks["agent_reach_installed"]:
        checks["agent_reach_doctor"] = await run_command(AGENT_REACH, "doctor", timeout=45)
    return checks


@mcp.tool()
async def social_search(
    query: str,
    sources: list[str] | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Search read-only social/public sources.

    ``sources=None`` uses the default sources. An explicit empty list or an
    unknown source is a parameter error. Failed sources retain structured
    diagnostics and do not discard successful results from other sources.
    """
    query = _validate_query(query)
    limit = max(1, min(limit, 20))
    selected = _select_sources(sources)
    results = await dispatch_sources(query, selected, limit)
    xiaohongshu = results.get("xiaohongshu")
    if isinstance(xiaohongshu, dict):
        _publicize_xhs_urls(xiaohongshu.get("data"))
    return {
        "query": query,
        "status": _aggregate_status(results),
        "results": results,
    }


@mcp.tool()
async def gaode_food_ranking(city_name: str, limit: int = 20) -> dict[str, Any]:
    """Return restaurants from the public 高德扫街榜/状元榜 city food page."""
    if not city_name.strip():
        raise ValueError("city_name must not be empty")
    return await fetch_gaode_food_ranking(city_name, max(1, min(limit, 20)))


@mcp.tool()
async def douyin_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search Douyin videos as supplementary recent/local evidence."""
    query = _validate_query(query)
    selected = await dispatch_sources(query, ["douyin"], max(1, min(limit, 30)))
    return selected["douyin"]


@mcp.tool()
async def xiaohongshu_note(note_url: str) -> dict[str, Any]:
    """Read one full Xiaohongshu note from a signed search-result URL."""
    validated_url = _xhs_note_url(note_url)
    raw = await fetch_xiaohongshu_note(validated_url)
    result = _xiaohongshu_note_result(validated_url, raw)
    result["candidate"]["url"] = _public_url(result["candidate"]["url"])
    return result


@mcp.tool()
async def xiaohongshu_comments(
    note_url: str,
    limit: int = 20,
    with_replies: bool = True,
) -> dict[str, Any]:
    """Fetch comments and optional replies for one Xiaohongshu note.

    The URL must be the full note URL returned by search, including its
    xsec_token when the logged-in adapter requires it.  Comments are evidence
    for practical details and contradictions; they are not automatically
    treated as verified facts.
    """
    if not note_url.strip():
        raise ValueError("note_url must not be empty")
    return await fetch_xiaohongshu_comments(
        note_url.strip(), max(1, min(limit, 50)), with_replies
    )


@mcp.tool()
async def douyin_creator_comments(
    sec_uid: str,
    limit: int = 5,
    comment_limit: int = 10,
) -> dict[str, Any]:
    """Fetch top comments from a Douyin creator's recent videos.

    This is the supported OpenCLI path today.  It requires the creator's
    sec_uid and returns top_comments per video; direct comments for an
    arbitrary search-result video are not exposed by the current adapter.
    """
    if not sec_uid.strip():
        raise ValueError("sec_uid must not be empty")
    return await fetch_douyin_creator_comments(
        sec_uid.strip(), max(1, min(limit, 20)), max(1, min(comment_limit, 10))
    )


@mcp.tool()
async def nearby_discover(
    latitude: float,
    longitude: float,
    area_name: str = "",
    radius_km: int = 100,
    categories: list[str] | None = None,
    preferences: list[str] | None = None,
    max_results: int = 8,
) -> dict[str, Any]:
    """Collect nearby travel evidence from several read-only sources.

    This first version retrieves evidence and returns it for ranking.  Jev
    ranking is added after the source adapters are verified on this machine.
    """
    radius_km = max(5, min(radius_km, 300))
    max_results = max(1, min(max_results, 12))
    area = area_name.strip() or f"{latitude:.5f},{longitude:.5f}"
    cats = categories or ["小众景点", "本地美食", "安静住宿"]
    prefs = preferences or ["人少", "真实体验", "适合自驾", "避开网红打卡"]
    queries = [
        f"{area} {category} {' '.join(prefs)} 周边{radius_km}公里"
        for category in cats
    ]
    async def collect_query_evidence(query: str) -> dict[str, Any]:
        source_limits = {"xiaohongshu": 15, "bilibili": 12, "douyin": 10, "web": 12}
        source_jobs = [
            (source, social_search(query, [source], limit))
            for source, limit in source_limits.items()
        ]
        source_results = await asyncio.gather(
            *(job for _, job in source_jobs), return_exceptions=True
        )
        return {
            "query": query,
            "results": {
                source: (
                    normalize_adapter_result(
                        source,
                        {
                            "process_ok": False,
                            "error": _error("wrapper_exception", str(result)),
                        },
                    )
                    if isinstance(result, Exception)
                    else result.get("results", {}).get(source, result)
                )
                for (source, _), result in zip(source_jobs, source_results)
            },
        }

    evidence = await asyncio.gather(*(collect_query_evidence(query) for query in queries))
    dedupe_xiaohongshu_evidence(evidence)
    gaode = await fetch_gaode_food_ranking(area_name, 20) if area_name.strip() else {
        "ok": False,
        "error": "area_name is required to query the Amap city food ranking",
    }
    return {
        "location": {"latitude": latitude, "longitude": longitude, "area_name": area_name},
        "constraints": {"radius_km": radius_km, "categories": cats, "preferences": prefs},
        "queries": queries,
        "evidence": evidence,
        "gaode_food_ranking": gaode,
        "next_step": "Run Jev ranking after extracting and deduplicating place candidates.",
    }


def _candidate_text_for_jev(candidate: dict[str, Any]) -> str:
    text = candidate.get("text")
    if not isinstance(text, str):
        text = ""
    extras: list[str] = []
    comments = candidate.get("comments")
    if isinstance(comments, list):
        extras.extend(
            str(item.get("text", item)) if isinstance(item, dict) else str(item)
            for item in comments
        )
    comment_evidence = candidate.get("comment_evidence")
    if isinstance(comment_evidence, list):
        extras.extend(str(item) for item in comment_evidence)
    extra_text = "\n".join(item for item in extras if item).strip()
    if extra_text:
        return f"{text.strip()}\n补充证据：{extra_text}".strip()
    return text.strip()


def candidate_has_sufficient_evidence(candidate: dict[str, Any]) -> bool:
    if candidate.get("body_status") not in (None, "ok"):
        return False
    return len(_candidate_text_for_jev(candidate)) >= MIN_JEV_TEXT_LENGTH


def _jev_candidate_state(candidate: dict[str, Any]) -> dict[str, Any]:
    """Pass source evidence to Jev without replacing it with an agent summary."""
    return {
        key: candidate.get(key)
        for key in (
            "candidate_id",
            "title",
            "source",
            "url",
            "author",
            "text",
            "comments",
            "comment_evidence",
            "likes",
            "collects",
            "published_at",
        )
    }


def build_jev_payload(
    candidates: list[dict[str, Any]], preferences: list[str]
) -> tuple[dict[str, Any], dict[str, int]]:
    state_candidates = [_jev_candidate_state(candidate) for candidate in candidates]
    state = {"user_preferences": preferences, "candidates": state_candidates}
    questions: dict[str, Any] = {}
    eligible: dict[str, int] = {}
    preference_text = "、".join(preferences) if preferences else "没有额外偏好"
    for index, candidate in enumerate(candidates):
        if not candidate_has_sufficient_evidence(candidate):
            continue
        eligible_id = str(candidate.get("candidate_id", index))
        eligible[eligible_id] = index
        questions[f"candidate_{index}_firsthand"] = {
            "type": "noul",
            "instructions": {
                "question": "判断这段正文是否呈现明显的第一手体验，而不是判断作者身份是否已验证。",
                "evidence": f"`candidates[{index}].text` 及必要的评论证据",
            },
            "criteria": {
                "true": "有具体路线、到达时间、停车、价格、排队、现场情况、天气路况、优缺点或踩坑等体验细节。",
                "false": "只有泛泛形容、转载口吻、模板化推荐，缺少可核对的体验细节。",
            },
        }
        questions[f"candidate_{index}_marketing"] = {
            "type": "noul",
            "instructions": {
                "question": "判断正文是否有明显营销、商业合作或导流倾向。商业合作本身不等于内容无价值。",
                "evidence": f"`candidates[{index}].text` 及必要的评论证据",
            },
            "criteria": {
                "true": "有团购、私信预订、购买链接、联系方式、商家自营、旅行社模板或明显夸张导流。",
                "false": "没有明显导流，或虽有合作标记但正文仍以具体体验和限制为主。",
            },
        }
        questions[f"candidate_{index}_fit"] = {
            "type": "score",
            "instructions": {
                "question": "评价正文内容对本次用户偏好的匹配程度。只按当前用户偏好判断，不把默认偏好当成固定标准。",
                "preferences": preference_text,
                "evidence": f"`candidates[{index}].text` 及候选元数据",
            },
            "criteria": [
                "不符合当前偏好或缺乏可执行信息",
                "部分符合当前偏好，但信息有限",
                "较符合当前偏好，且有具体可执行信息",
                "非常符合当前偏好，具体且有明确体验价值",
            ],
        }
    return {"model": os.getenv("TYPESAFE_MODEL", "jev-latest"), "state": state, "questions": questions}, eligible


def _noul_value(answer: Any) -> float | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get("noul")
    return float(value) if isinstance(value, (int, float)) else None


def _score_value(answer: Any) -> float | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get("score")
    return float(value) if isinstance(value, (int, float)) else None


def rank_jev_results(candidates: list[dict[str, Any]], answers: dict[str, Any]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        result = dict(candidate)
        candidate_key = f"candidate_{index}"
        if not candidate_has_sufficient_evidence(candidate):
            result.update(
                {
                    "firsthand": None,
                    "marketing": None,
                    "fit": None,
                    "evidence_status": "insufficient",
                    "ranking": -1.0,
                    "reason": "正文信息不足，暂不进入正式推荐",
                }
            )
            ranked.append(result)
            continue
        firsthand_answer = answers.get(f"{candidate_key}_firsthand")
        marketing_answer = answers.get(f"{candidate_key}_marketing")
        fit_answer = answers.get(f"{candidate_key}_fit")
        firsthand = _noul_value(firsthand_answer)
        marketing = _noul_value(marketing_answer)
        fit = _score_value(fit_answer)
        if firsthand is None or marketing is None or fit is None:
            result.update(
                {
                    "firsthand": firsthand_answer,
                    "marketing": marketing_answer,
                    "fit": fit_answer,
                    "evidence_status": "insufficient",
                    "ranking": -1.0,
                    "reason": "Jev 未返回完整判断，证据待补查",
                }
            )
            ranked.append(result)
            continue
        ranking = round((fit / 3.0) * 0.55 + firsthand * 0.35 - marketing * 0.25, 4)
        if marketing >= 0.75 and firsthand < 0.5:
            status = "filtered"
            ranking = min(ranking, -0.1)
            reason = "营销倾向明显且缺少第一手细节"
        elif marketing >= 0.65 and firsthand >= 0.5:
            status = "marketing_risk"
            reason = "营销倾向较高，但正文仍有具体体验信息"
        else:
            status = "supported"
            reason = "第一手体验细节丰富，偏好匹配较高" if fit >= 2.0 and firsthand >= 0.65 else "有部分第一手体验支持"
        result.update(
            {
                "firsthand": firsthand_answer,
                "marketing": marketing_answer,
                "fit": fit_answer,
                "evidence_status": status,
                "ranking": ranking,
                "reason": reason,
            }
        )
        ranked.append(result)
    return sorted(ranked, key=lambda item: item["ranking"], reverse=True)


@mcp.tool()
async def jev_rank_candidates(candidates: list[dict[str, Any]], user_preferences: list[str] | None = None) -> dict[str, Any]:
    """Judge firsthand evidence, marketing tendency, and preference fit with Jev."""
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        return {"ok": False, "error": "TYPESAFE_API_KEY is not configured", "candidates": candidates}
    if not candidates:
        return {"ok": True, "answers": {}, "results": []}
    candidates = candidates[:12]
    preferences = list(DEFAULT_USER_PREFERENCES if user_preferences is None else user_preferences)
    payload, eligible = build_jev_payload(candidates, preferences)
    if not eligible:
        results = rank_jev_results(candidates, {})
        for item in results:
            item["url"] = _public_url(item.get("url"))
        return {"ok": True, "answers": {}, "results": results, "candidates": results, "model": payload["model"]}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.typesafe.ai/v1/systemone",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            answers = body.get("answers", {}) if isinstance(body, dict) else {}
            results = rank_jev_results(candidates, answers)
            for item in results:
                item["url"] = _public_url(item.get("url"))
            return {"ok": True, "answers": answers, "results": results, "candidates": results, "model": body.get("model", payload["model"])}
    except Exception as exc:  # keep the MCP tool useful when the service is unavailable
        return {"ok": False, "error": str(exc), "candidates": candidates}


# --- High-level recommend pipeline -----------------------------------------
# search -> dedupe -> read Xiaohongshu bodies -> Jev -> targeted follow-up ->
# final recommendations.  Kept deliberately small: two research rounds at most.


def _recommend_queries(request: str, area: str, categories: list[str]) -> list[str]:
    """Build a few plain queries; never stuff preference words into them."""
    anchor = area or request
    connector = "" if "周边" in anchor else "周边 "
    queries: list[str] = []
    if not area:
        queries.append(request)
    for category in categories[: 3 if area else 2]:
        queries.append(f"{anchor} {connector}{category}")
    queries.append(f"{anchor} {categories[0]} 实际体验")
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        query = " ".join(query.split())
        if query and query not in seen:
            seen.add(query)
            unique.append(query)
    return unique[:MAX_SEARCH_QUERIES]


def _candidate_dedupe_key(candidate: dict[str, Any]) -> str:
    url = (candidate.get("url") or "").strip()
    host = (urlsplit(url).hostname or "").lower()
    if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
        return f"xhs:{_candidate_id(url)}"
    return f"{host}{urlsplit(url).path.rstrip('/')}"


def _dedupe_pool(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate across queries and sources; a signed XHS URL wins over an unsigned copy."""
    seen: dict[str, int] = {}
    unique: list[dict[str, Any]] = []
    for candidate in pool:
        key = _candidate_dedupe_key(candidate)
        index = seen.get(key)
        if index is None:
            seen[key] = len(unique)
            unique.append(candidate)
        elif "xsec_token=" in (candidate.get("url") or "") and "xsec_token=" not in (
            unique[index].get("url") or ""
        ):
            unique[index] = candidate
    return unique


def _social_row_candidate(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    url = row.get("url") or row.get("link") or row.get("share_url")
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    title = row.get("title") or row.get("desc") or ""
    text = row.get("desc") or row.get("description") or row.get("content") or ""
    return {
        "candidate_id": _candidate_id(url),
        "title": title,
        "name": title,
        "source": source,
        "url": url,
        "author": row.get("author") or "",
        "text": text if isinstance(text, str) else "",
        "comments": None,
        "likes": row.get("likes") if row.get("likes") is not None else row.get("score"),
        "collects": None,
        "published_at": row.get("published_at"),
        "evidence_level": "snippet",
    }


def _web_result_candidates(data: Any) -> list[dict[str, Any]]:
    """Parse Exa text blocks shaped as `Title:/URL:/Published:/Author:/Highlights:` groups."""
    if not isinstance(data, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        for segment in re.split(r"(?m)(?=^Title: )", block.get("text") or ""):
            url_match = re.search(r"(?m)^URL: (\S+)", segment)
            if not url_match:
                continue
            url = url_match.group(1).strip()
            title_match = re.search(r"(?m)^Title: (.+)", segment)
            author_match = re.search(r"(?m)^Author: (.+)", segment)
            published_match = re.search(r"(?m)^Published: (.+)", segment)
            body_match = re.search(r"(?m)^Highlights:\s*\n(.*)", segment, re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""
            author = author_match.group(1).strip() if author_match else ""
            published = published_match.group(1).strip() if published_match else ""
            candidates.append(
                {
                    "candidate_id": _candidate_id(url),
                    "title": title,
                    "name": title,
                    "source": "web",
                    "url": url,
                    "author": "" if author == "N/A" else author,
                    "text": (body_match.group(1).strip() if body_match else "")[:800],
                    "comments": None,
                    "likes": None,
                    "collects": None,
                    "published_at": None if published == "N/A" else published,
                    "evidence_level": "snippet",
                }
            )
    return candidates


def _summarize_source_status(statuses: list[str]) -> str:
    return _aggregate_status({str(index): {"status": status} for index, status in enumerate(statuses)})


def _collect_candidates(
    per_query_results: list[dict[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    pool: list[dict[str, Any]] = []
    status_lists: dict[str, list[str]] = {source: [] for source in RECOMMEND_SOURCES}
    for results in per_query_results:
        for source, result in results.items():
            status_lists.setdefault(source, []).append(result.get("status", "unavailable"))
            if result.get("status") not in SUCCESS_STATUSES:
                continue
            data = result.get("data")
            if source == "xiaohongshu":
                pool.extend(c for c in (data or []) if isinstance(c, dict))
            elif source in ("bilibili", "douyin"):
                for row in _xhs_search_rows(data):
                    candidate = _social_row_candidate(row, source)
                    if candidate:
                        pool.append(candidate)
            elif source == "web":
                pool.extend(_web_result_candidates(data))
    source_status = {
        source: _summarize_source_status(statuses)
        for source, statuses in status_lists.items()
    }
    return _dedupe_pool(pool), source_status


def _preselect_candidates(
    pool: list[dict[str, Any]], terms: list[str], limit: int
) -> list[dict[str, Any]]:
    """Prefer relevant, body-readable candidates; deprioritize but never drop."""

    def order(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, candidate = item
        haystack = f"{candidate.get('title') or ''} {candidate.get('text') or ''}"
        relevant = not terms or any(term and term in haystack for term in terms)
        readable = candidate.get("source") == "xiaohongshu"
        return (0 if relevant else 1, 0 if readable else 1, index)

    return [candidate for _, candidate in sorted(enumerate(pool), key=order)[:limit]]


async def _read_xhs_bodies(candidates: list[dict[str, Any]]) -> None:
    """Read note bodies in place; failures keep the candidate marked unavailable."""

    async def read_one(candidate: dict[str, Any]) -> None:
        try:
            url = _xhs_note_url(candidate.get("url") or "")
        except ValueError:
            candidate["body_status"] = "unavailable"
            return
        result = _xiaohongshu_note_result(url, await fetch_xiaohongshu_note(url))
        note = result["candidate"]
        if result["status"] == "ok":
            candidate.update(
                {
                    key: note[key]
                    for key in (
                        "title", "name", "author", "text", "comments",
                        "likes", "collects", "published_at", "body_status",
                    )
                }
            )
            candidate["evidence_level"] = "body"
        else:
            candidate["body_status"] = "unavailable"

    await asyncio.gather(
        *(read_one(c) for c in candidates if c.get("source") == "xiaohongshu")
    )


def _comment_texts(raw: dict[str, Any]) -> list[str]:
    if not isinstance(raw, dict) or not raw.get("process_ok", raw.get("ok")) or raw.get("parse_error"):
        return []
    return [
        row["text"].strip()
        for row in _xhs_search_rows(raw.get("data"))
        if isinstance(row.get("text"), str) and row["text"].strip()
    ]


_PLACE_SUFFIX = "山顶|山|湖|村|镇|民宿|营地|景区|公园|古道|瀑布|溪谷|峡谷|草甸|农庄|水库|岛|溪|寺|桥|湾"


def _extract_place(candidate: dict[str, Any]) -> str:
    """Best-effort place token: longest suffix span in the first place-like run."""
    fallback = ""
    for field in (candidate.get("title"), candidate.get("text")):
        if not isinstance(field, str):
            continue
        for run in re.findall(r"[一-鿿]{2,}", field):
            matches = list(re.finditer(_PLACE_SUFFIX, run))
            if not matches:
                continue
            first, last = matches[0], matches[-1]
            place = run[max(0, last.start() - 4) : last.end()]
            if first.start() <= 3:
                return place
            if not fallback:
                fallback = place
    return fallback


def _has_practical_info(candidate: dict[str, Any]) -> bool:
    blob = f"{candidate.get('title') or ''} {_candidate_text_for_jev(candidate)}"
    return any(keyword in blob for keyword in PRACTICAL_KEYWORDS)


def _followup_targets(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only promising candidates missing key practical info get one follow-up."""
    targets: list[dict[str, Any]] = []
    for candidate in ranked:
        if len(targets) >= MAX_FOLLOWUP_CANDIDATES:
            break
        status = candidate.get("evidence_status")
        if status == "filtered" or candidate.get("followed_up"):
            continue
        if _has_practical_info(candidate):
            continue
        if status in ("supported", "marketing_risk") and (candidate.get("ranking") or 0) < 0.1:
            continue
        if status == "insufficient" and candidate.get("body_status") == "unavailable":
            continue  # comments alone cannot rescue a missing body
        if candidate.get("out_of_range"):
            continue  # not worth a follow-up call when it exceeds the distance limit
        if candidate.get("source") == "xiaohongshu" or _extract_place(candidate):
            targets.append(candidate)
    return targets


async def _followup_candidate(candidate: dict[str, Any]) -> None:
    """One shot at the missing detail: XHS comments, else one targeted web query."""
    candidate["followed_up"] = True
    if candidate.get("source") == "xiaohongshu":
        texts = _comment_texts(
            await fetch_xiaohongshu_comments(
                candidate.get("url") or "", FOLLOWUP_COMMENT_LIMIT, False
            )
        )
        if texts:
            candidate["comment_evidence"] = texts
            return
    place = _extract_place(candidate)
    if not place:
        return
    results = await dispatch_sources(f"{place} 停车 门票 营业", ["web"], 5)
    web = results.get("web", {})
    if web.get("status") in SUCCESS_STATUSES:
        snippets = [
            f"{c['title']}：{c['text'][:120]}"
            for c in _web_result_candidates(web.get("data"))[:3]
            if c.get("text")
        ]
        if snippets:
            candidate["comment_evidence"] = snippets


def _extract_key_evidence(candidate: dict[str, Any]) -> list[str]:
    blobs = []
    if isinstance(candidate.get("text"), str) and candidate["text"].strip():
        blobs.append(candidate["text"])
    blobs.extend(str(item) for item in candidate.get("comment_evidence") or [])
    sentences: list[str] = []
    for blob in blobs:
        for sentence in re.split(r"[。！？!?\n]+", blob):
            sentence = sentence.strip()
            if len(sentence) >= 8 and not sentence.startswith("#") and re.search(r"[一-鿿]", sentence):
                sentences.append(sentence[:80])
    if not sentences:
        return []
    picked = [s for s in sentences if any(k in s for k in EVIDENCE_KEYWORDS)]
    return (picked or sentences)[:2]


def _risks_for(candidate: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    if candidate.get("evidence_status") == "marketing_risk":
        risks.append("内容有营销/合作倾向，建议核实是否为商家宣传")
    if candidate.get("body_status") == "unavailable":
        risks.append("正文读取失败，仅有标题与搜索摘要")
    elif candidate.get("evidence_level") == "snippet":
        risks.append("仅搜索摘要，未读取正文")
    if candidate.get("comment_evidence"):
        risks.append("部分信息来自评论或补搜，可能已变化")
    if candidate.get("out_of_range"):
        risks.append(f"距起点约 {candidate.get('distance_km')} 公里，超出约定范围")
    elif candidate.get("distance_requested") and candidate.get("distance_km") is None:
        risks.append("位置未能核实，距离未知")
    if not _has_practical_info(candidate):
        risks.append("缺少停车、门票等实用信息，出发前需自行核实")
    return risks


def _to_recommendation(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": candidate.get("title") or candidate.get("name") or "",
        "source": candidate.get("source"),
        "url": _public_url(candidate.get("url")),
        "reason": candidate.get("reason"),
        "evidence_status": candidate.get("evidence_status"),
        "distance_km": candidate.get("distance_km"),
        "mentions": candidate.get("mentions", 1),
        "firsthand": _noul_value(candidate.get("firsthand")),
        "marketing": _noul_value(candidate.get("marketing")),
        "fit": _score_value(candidate.get("fit")),
        "ranking": candidate.get("ranking"),
        "key_evidence": _extract_key_evidence(candidate),
        "risks": _risks_for(candidate),
    }


async def _safe_dispatch(query: str, sources: list[str], limit: int) -> dict[str, dict[str, Any]]:
    try:
        return await dispatch_sources(query, sources, limit)
    except Exception as exc:
        return {
            source: normalize_adapter_result(
                source,
                {"process_ok": False, "error": _error("dispatch_exception", str(exc))},
            )
            for source in sources
        }


# --- Distance / place helpers ------------------------------------------------
# Geocoding is best-effort: Amap when AMAP_API_KEY is configured, otherwise
# Nominatim with a display-name sanity check.  Places that cannot be resolved
# keep distance_km=None and are never filtered out on distance.


def _parse_radius_km(request: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:公里|千米|km)", request, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:米|m)(?![a-zA-Z])", request, re.IGNORECASE)
    if match:
        return float(match.group(1)) / 1000.0
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


async def _amap_get(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=GEO_TIMEOUT) as client:
            response = await client.get(
                f"https://restapi.amap.com{path}",
                params={**params, "key": AMAP_API_KEY, "output": "json"},
            )
            body = response.json()
        return body if isinstance(body, dict) and body.get("status") == "1" else None
    except Exception:
        return None


def _split_lng_lat(location: Any) -> tuple[float, float] | None:
    if not isinstance(location, str) or "," not in location:
        return None
    try:
        lng, lat = location.split(",", 1)
        return float(lat), float(lng)
    except ValueError:
        return None


async def geocode_area(area: str) -> tuple[float, float, str] | None:
    """Resolve the request's origin anchor. Returns (lat, lon, label)."""
    if AMAP_API_KEY:
        body = await _amap_get("/v3/geocode/geo", {"address": area})
        codes = (body or {}).get("geocodes") or []
        point = _split_lng_lat(codes[0].get("location")) if codes else None
        if point:
            return point[0], point[1], area
        return None
    return await _nominatim_geocode(area, must_contain=(area,))


async def geocode_place(place: str, area: str) -> tuple[float, float, str] | None:
    """Resolve one candidate place. Returns (lat, lon, label)."""
    if AMAP_API_KEY:
        body = await _amap_get(
            "/v3/place/text",
            {"keywords": place, "city": area or "", "citylimit": "true" if area else "false"},
        )
        pois = (body or {}).get("pois") or []
        point = _split_lng_lat(pois[0].get("location")) if pois else None
        if point:
            return point[0], point[1], str(pois[0].get("name") or place)
        return None
    return await _nominatim_geocode(f"{place} {area}".strip(), must_contain=(place,))


async def _nominatim_geocode(query: str, must_contain: tuple[str, ...]) -> tuple[float, float, str] | None:
    try:
        async with httpx.AsyncClient(
            timeout=GEO_TIMEOUT,
            headers={"User-Agent": "road-scout/0.1 (personal nearby-search tool)"},
        ) as client:
            response = await client.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1, "accept-language": "zh"},
            )
            rows = response.json()
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    display = str(row.get("display_name") or "")
    # Reject implausible matches (e.g. a bus stop in another city sharing a word).
    if must_contain and not any(token and token in display for token in must_contain):
        return None
    try:
        return float(row["lat"]), float(row["lon"]), display
    except (KeyError, TypeError, ValueError):
        return None


async def _resolve_origin(
    latitude: float | None, longitude: float | None, area: str
) -> tuple[float, float, str] | None:
    if latitude is not None and longitude is not None:
        try:
            return float(latitude), float(longitude), "指定坐标"
        except (TypeError, ValueError):
            return None
    if area:
        return await geocode_area(area)
    return None


async def _annotate_distances(
    candidates: list[dict[str, Any]], origin: tuple[float, float], area: str
) -> None:
    places = {c.get("_place") for c in candidates if c.get("_place")}
    coords: dict[str, tuple[float, float, str] | None] = {}
    for place in places:  # sequential: be polite to the free geocoder
        coords[place] = await geocode_place(place, area)
    for candidate in candidates:
        geo = coords.get(candidate.get("_place") or "")
        if geo:
            candidate["distance_km"] = round(
                _haversine_km(origin[0], origin[1], geo[0], geo[1]), 1
            )
            candidate["place_label"] = geo[2]
        else:
            candidate["distance_km"] = None


def _place_key(candidate: dict[str, Any]) -> str | None:
    place = re.sub(r"\s+", "", str(candidate.get("_place") or _extract_place(candidate) or ""))
    return place if len(place) >= 3 else None


def _dedupe_by_place(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple notes about the same place; the best-ranked one wins."""
    groups: list[tuple[str, dict[str, Any]]] = []
    result: list[dict[str, Any]] = []
    for candidate in ranked:
        key = _place_key(candidate)
        merged = False
        if key:
            for existing_key, kept in groups:
                if key in existing_key or existing_key in key:
                    kept["mentions"] = kept.get("mentions", 1) + 1
                    merged = True
                    break
        if not merged:
            if key:
                groups.append((key, candidate))
            result.append(candidate)
    return result


@mcp.tool()
async def road_scout_recommend(
    request: str,
    area_name: str = "",
    categories: list[str] | None = None,
    preferences: list[str] | None = None,
    include_food: bool = False,
    max_results: int = 5,
    radius_km: float | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    """One-call recommend: search -> dedupe -> read bodies -> Jev -> gap-fill -> output.

    ``request`` is the user's own words (e.g. "杭州附近小众自驾"). ``area_name``
    anchors queries to a place; when empty, ``request`` is the anchor. Failed
    sources are skipped, never fatal. ``max_results`` caps formal
    recommendations; weaker leads land in ``exploratory`` instead of padding.

    Distance constraints come from ``radius_km`` or phrases like "附近100公里"/
    "500米" inside ``request`` (explicit ``radius_km`` wins). The origin is
    ``latitude``/``longitude`` when both are given, otherwise ``area_name``
    geocoded via Amap (AMAP_API_KEY) or Nominatim. Candidates beyond the radius
    move to ``exploratory`` with their distance; unresolved places keep
    ``distance_km=None`` and are never filtered out.
    """
    request = _validate_query(request)
    max_results = max(1, min(max_results, 12))
    cats = [
        c.strip() for c in (categories or DEFAULT_RECOMMEND_CATEGORIES) if isinstance(c, str) and c.strip()
    ][:3] or list(DEFAULT_RECOMMEND_CATEGORIES)
    prefs = list(DEFAULT_USER_PREFERENCES if preferences is None else preferences)
    area = area_name.strip()
    queries = _recommend_queries(request, area, cats)
    notes: list[str] = []

    radius = radius_km if radius_km is not None else _parse_radius_km(request)
    if radius is not None:
        radius = max(0.1, min(float(radius), 1000.0))

    per_query, origin = await asyncio.gather(
        asyncio.gather(
            *(_safe_dispatch(query, list(RECOMMEND_SOURCES), SEARCH_LIMIT_PER_SOURCE) for query in queries)
        ),
        _resolve_origin(latitude, longitude, area),
    )
    pool, source_status = _collect_candidates(per_query)
    for source, status in source_status.items():
        if status in FAILURE_STATUSES:
            notes.append(f"{source} 搜索失败，结果仅来自其他来源")
        elif status in ("partial", "empty"):
            notes.append(f"{source} 结果不完整或为空")

    def result(**overrides: Any) -> dict[str, Any]:
        base = {
            "request": request,
            "area_name": area or None,
            "queries": queries,
            "recommendations": [],
            "exploratory": [],
            "food": None,
            "geo": {
                "origin": (
                    {"latitude": origin[0], "longitude": origin[1], "label": origin[2]}
                    if origin
                    else None
                ),
                "radius_km": radius,
                "provider": "amap" if AMAP_API_KEY else "nominatim",
            },
            "source_status": source_status,
            "notes": notes,
            "stats": {"candidates": len(pool), "ranked": 0, "filtered": 0, "followups": 0},
        }
        base.update(overrides)
        return base

    food = None
    if include_food or any(keyword in request for keyword in FOOD_KEYWORDS):
        if area:
            food = await fetch_gaode_food_ranking(area, 10)
            if isinstance(food, dict) and food.get("ok"):
                food["note"] = "高德榜单仅作候选参考，出发前确认营业与排队"
        else:
            notes.append("缺少 area_name，跳过高德美食榜")

    if not pool:
        notes.append("所有来源均未返回可用候选")
        return result(food=food)

    relevance_terms = [term for term in [area, *cats] if term]
    selected = _preselect_candidates(pool, relevance_terms, MAX_RANKED_CANDIDATES)
    await _read_xhs_bodies(selected)

    # jev_rank_candidates normalizes outbound URLs; restore the original signed
    # URLs for internal follow-up reads (comments / note fetches).
    original_urls = {c.get("candidate_id"): c.get("url") for c in selected}

    def restore_urls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items:
            original = original_urls.get(item.get("candidate_id"))
            if original:
                item["url"] = original
        return items

    ranked_result = await jev_rank_candidates(selected, prefs)
    if not ranked_result.get("ok"):
        notes.append(f"Jev 不可用：{ranked_result.get('error') or 'unknown error'}")
        exploratory = [
            {
                **_to_recommendation(candidate),
                "evidence_status": "unranked",
                "reason": "Jev 未返回判断，候选未排序",
            }
            for candidate in selected
        ]
        return result(
            exploratory=exploratory,
            food=food,
            stats={"candidates": len(pool), "ranked": len(selected), "filtered": 0, "followups": 0},
        )

    ranked = restore_urls(ranked_result["results"])
    for candidate in ranked:
        candidate["_place"] = _extract_place(candidate)
    ranked = _dedupe_by_place(ranked)

    if origin:
        await _annotate_distances(ranked, (origin[0], origin[1]), area)
    elif radius:
        notes.append("请求包含距离约束，但缺少可定位起点（area_name 或 lat/lon），未做距离过滤")
    if origin and radius:
        for candidate in ranked:
            candidate["distance_requested"] = True
            distance = candidate.get("distance_km")
            candidate["out_of_range"] = distance is not None and distance > radius

    targets = _followup_targets(ranked)
    if targets:
        await asyncio.gather(*(_followup_candidate(target) for target in targets))
        rerank = await jev_rank_candidates(targets, prefs)
        if rerank.get("ok"):
            updated = {item.get("candidate_id"): item for item in restore_urls(rerank["results"])}
            ranked = [
                {**updated.get(candidate.get("candidate_id"), candidate),
                 "distance_km": candidate.get("distance_km"),
                 "distance_requested": candidate.get("distance_requested"),
                 "out_of_range": candidate.get("out_of_range"),
                 "mentions": candidate.get("mentions", 1),
                 "_place": candidate.get("_place")}
                for candidate in ranked
            ]
            ranked.sort(key=lambda item: item.get("ranking", -1.0), reverse=True)

    def recommendable(candidate: dict[str, Any]) -> bool:
        return candidate.get("evidence_status") in ("supported", "marketing_risk")

    recommendations = [
        _to_recommendation(candidate)
        for candidate in ranked
        if recommendable(candidate) and not candidate.get("out_of_range")
    ][:max_results]
    exploratory = [
        _to_recommendation(candidate)
        for candidate in ranked
        if (recommendable(candidate) and candidate.get("out_of_range"))
        or candidate.get("evidence_status") == "insufficient"
    ][:MAX_EXPLORATORY]
    filtered = sum(1 for candidate in ranked if candidate.get("evidence_status") == "filtered")

    return result(
        recommendations=recommendations,
        exploratory=exploratory,
        food=food,
        stats={
            "candidates": len(pool),
            "ranked": len(selected),
            "filtered": filtered,
            "followups": len(targets),
            "out_of_range": sum(1 for candidate in ranked if candidate.get("out_of_range")),
        },
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Optional bearer auth for the future public HTTPS endpoint."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not MCP_TOKEN:
            return await call_next(request)
        if request.url.path in {"/health", "/"}:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {MCP_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def main() -> None:
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    app.add_route("/health", lambda request: JSONResponse({"ok": True, "service": "road-scout"}), methods=["GET"])
    config = uvicorn.Config(app, host=MCP_HOST, port=MCP_PORT, log_level=os.getenv("ROAD_SCOUT_LOG_LEVEL", "info"))
    server = uvicorn.Server(config)
    await server.serve()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()

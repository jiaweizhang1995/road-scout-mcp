"""Read-only MCP server for nearby road-trip discovery.

The server keeps platform credentials on the Mac and exposes only high-level
read tools to a remote Open Minis client.  It intentionally does not expose
shell, posting, commenting, liking, or cookie-management tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any, Awaitable, Callable

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


def normalize_adapter_result(source: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a command/MCP response to the public source result contract."""
    process_ok = raw.get("process_ok", raw.get("ok", False))
    parse_error = raw.get("parse_error")
    data = raw.get("data")
    error = raw.get("error")
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
    elif not isinstance(data, (dict, list)):
        status = "parse_error"
        error = _error("invalid_shape", "search adapter returned a non-object/non-array JSON value")
    else:
        status = "empty" if _data_is_empty(data) else "ok"
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
    if all(status == "empty" for status in statuses):
        return "empty"
    if all(status in SUCCESS_STATUSES for status in statuses):
        return "ok"
    if any(status in SUCCESS_STATUSES for status in statuses):
        return "partial"
    return "unavailable"


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
                    {"ok": False, "error": str(result)}
                    if isinstance(result, Exception)
                    else result.get("results", {}).get(source, result)
                )
                for (source, _), result in zip(source_jobs, source_results)
            },
        }

    evidence = await asyncio.gather(*(collect_query_evidence(query) for query in queries))
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


@mcp.tool()
async def jev_rank_candidates(candidates: list[dict[str, Any]], user_preferences: list[str] | None = None) -> dict[str, Any]:
    """Score candidate evidence with Jev without generating or executing actions.

    Each candidate should include a name and evidence text.  The returned
    probabilities are signals, not proof that a creator is a real user.
    """
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        return {"ok": False, "error": "TYPESAFE_API_KEY is not configured", "candidates": candidates}
    if not candidates:
        return {"ok": True, "answers": {}}
    candidates = candidates[:12]
    preferences = user_preferences or ["小众", "人少", "本地体验", "适合自驾"]
    state = {"preferences": preferences, "candidates": candidates}
    questions: dict[str, Any] = {}
    for index, candidate in enumerate(candidates):
        questions[f"candidate_{index}_firsthand"] = {
            "type": "noul",
            "instructions": f"判断 candidates[{index}] 是否像作者亲自到访后写下的第一手体验，而不是广告、转载或模板化营销内容。",
            "criteria": {
                "true": "包含具体地点、路线、价格、时间、体验细节或真实缺点，能看出作者实际到访。",
                "false": "主要是夸张形容、泛泛推荐、导流、带货、合作宣传或缺乏可核验细节。",
            },
        }
        questions[f"candidate_{index}_marketing"] = {
            "type": "noul",
            "instructions": f"判断 candidates[{index}] 是否有明显营销或商业推广倾向。",
            "criteria": {
                "true": "出现广告/合作/团购/私信/购买链接、商务联系方式、统一宣传模板或强导流。",
                "false": "没有明显商业导流，且同时包含具体体验、限制或负面信息。",
            },
        }
        questions[f"candidate_{index}_fit"] = {
            "type": "score",
            "instructions": f"评价 candidates[{index}] 对用户偏好的匹配程度。",
            "criteria": [
                "几乎不符合：热门、泛泛而谈或与自驾需求无关",
                "部分符合：有一些相关信息，但缺少小众或路线细节",
                "比较符合：有明确的本地体验和自驾价值",
                "非常符合：冷门、少人、具体、可执行，且明显适合这次自驾",
            ],
        }
    payload = {"model": os.getenv("TYPESAFE_MODEL", "jev-latest"), "state": state, "questions": questions}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.typesafe.ai/v1/systemone",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            return {"ok": True, "answers": response.json().get("answers", {}), "model": payload["model"]}
    except Exception as exc:  # keep the MCP tool useful when the service is unavailable
        return {"ok": False, "error": str(exc), "candidates": candidates}


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

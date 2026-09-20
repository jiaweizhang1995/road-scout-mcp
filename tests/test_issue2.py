import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import server


class Issue2DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_social_search_dispatches_douyin(self):
        with patch.object(server, "search_douyin", new=AsyncMock(return_value={"ok": True, "data": []})) as search:
            result = await server.social_search("  route  ", ["douyin"], limit=3)
        search.assert_awaited_once_with("route", 3)
        self.assertIn("douyin", result["results"])

    async def test_nearby_discover_dispatches_douyin_once(self):
        with patch.object(server, "search_douyin", new=AsyncMock(return_value={"ok": True, "data": []})) as search, patch.object(
            server, "search_xiaohongshu", new=AsyncMock(return_value={"ok": True, "data": []})
        ), patch.object(server, "search_bilibili", new=AsyncMock(return_value={"ok": True, "data": []})), patch.object(
            server, "search_web", new=AsyncMock(return_value={"ok": True, "data": []})
        ), patch.object(server, "fetch_gaode_food_ranking", new=AsyncMock(return_value={"ok": True, "items": []})):
            await server.nearby_discover(
                31.2,
                121.5,
                area_name="上海",
                categories=["景点"],
                preferences=[],
            )
        self.assertEqual(search.await_count, 1)

    async def test_sources_empty_is_parameter_error(self):
        with self.assertRaises(ValueError):
            await server.social_search("route", [])

    async def test_unknown_source_is_parameter_error(self):
        with self.assertRaises(ValueError):
            await server.social_search("route", ["unknown"])

    async def test_query_and_limit_boundaries(self):
        with patch.object(server, "search_web", new=AsyncMock(return_value={"ok": True, "data": []})) as web:
            result = await server.social_search("  route  ", ["web"], limit=999)
        web.assert_awaited_once_with("route", 20)
        self.assertEqual(result["results"]["web"]["status"], "empty")
        with self.assertRaises(ValueError):
            await server.social_search("   ", ["web"])
        with self.assertRaises(ValueError):
            await server.social_search("x" * (server.MAX_QUERY_LENGTH + 1), ["web"])

    async def test_one_source_failure_keeps_other_source_success(self):
        with patch.object(server, "search_xiaohongshu", new=AsyncMock(side_effect=RuntimeError("offline"))), patch.object(
            server, "search_web", new=AsyncMock(return_value={"ok": True, "data": [{"title": "trail"}]})
        ):
            result = await server.social_search("route", ["xiaohongshu", "web"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["xiaohongshu"]["status"], "unavailable")
        self.assertEqual(result["results"]["web"]["status"], "ok")

    async def test_duplicate_sources_are_deduplicated_and_ordered(self):
        with patch.object(server, "search_bilibili", new=AsyncMock(return_value={"ok": True, "data": []})) as bili, patch.object(
            server, "search_web", new=AsyncMock(return_value={"ok": True, "data": []})
        ) as web:
            result = await server.social_search("route", ["bilibili", "web", "bilibili"], limit=2)
        self.assertEqual(list(result["results"]), ["bilibili", "web"])
        bili.assert_awaited_once_with("route", 2)
        web.assert_awaited_once_with("route", 2)


class Issue2CommandContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_command_rejects_non_json_output(self):
        result = await server.run_command(sys.executable, "-c", "print('diagnostic')", expect_json=True)
        self.assertTrue(result["process_ok"])
        self.assertEqual(result["parse_error"]["code"], "invalid_json")

    async def test_text_command_keeps_diagnostic_text(self):
        result = await server.run_command(sys.executable, "-c", "print('diagnostic')")
        self.assertEqual(result["data"], "diagnostic")
        self.assertIsNone(result["parse_error"])

    async def test_business_is_error_is_not_inferred_from_text(self):
        raw = {"process_ok": True, "data": {"message": "error text"}, "parse_error": None}
        normalized = server.normalize_adapter_result("exa", raw)
        self.assertEqual(normalized["status"], "ok")

    async def test_is_error_is_structured_business_failure(self):
        raw = {"process_ok": True, "data": {"isError": True, "content": [{"type": "text", "text": "denied"}]}, "parse_error": None}
        normalized = server.normalize_adapter_result("exa", raw)
        self.assertEqual(normalized["status"], "unavailable")
        self.assertEqual(normalized["error"]["code"], "mcp_is_error")

    async def test_json_shape_error_is_parse_error(self):
        raw = {"process_ok": True, "data": 1, "parse_error": None}
        normalized = server.normalize_adapter_result("web", raw)
        self.assertEqual(normalized["status"], "parse_error")
        self.assertEqual(normalized["error"]["code"], "invalid_shape")

    async def test_mcp_content_blocks_are_preserved(self):
        data = {"isError": False, "content": [{"type": "text", "text": "result"}]}
        normalized = server.normalize_adapter_result(
            "exa", {"process_ok": True, "data": data, "parse_error": None}
        )
        self.assertEqual(normalized["status"], "ok")
        self.assertEqual(normalized["data"], data)


class Issue2ExaContractTests(unittest.TestCase):
    def test_exa_arguments_match_local_fixture(self):
        args = server.build_exa_search_args("route", 7)
        payload = json.loads(args[args.index("--args") + 1])
        self.assertEqual(set(payload), {"query", "numResults", "objective"})
        self.assertEqual(payload["query"], "route")
        self.assertEqual(payload["numResults"], 7)
        self.assertTrue(payload["objective"])


if __name__ == "__main__":
    unittest.main()

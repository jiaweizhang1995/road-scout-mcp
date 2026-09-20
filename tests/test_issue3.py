import unittest
from unittest.mock import AsyncMock, patch

import server


SIGNED_URL = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=fixture-token"


class Issue3NoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_note_rows_into_jev_candidate(self):
        raw = {
            "process_ok": True,
            "data": [
                {"field": "title", "value": "山路"},
                {"field": "author", "value": "旅行者"},
                {"field": "content", "value": "今天沿溪流走了两小时。"},
                {"field": "likes", "value": "12"},
                {"field": "collects", "value": "3"},
                {"field": "comments", "value": "2"},
            ],
            "parse_error": None,
        }
        with patch.object(server, "run_command", new=AsyncMock(return_value=raw)) as run:
            result = await server.xiaohongshu_note(SIGNED_URL)

        run.assert_awaited_once_with(
            server.OPENCLI,
            "xiaohongshu",
            "note",
            SIGNED_URL,
            "-f",
            "json",
            expect_json=True,
        )
        candidate = result["candidate"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(candidate["candidate_id"], "0123456789abcdef01234567")
        self.assertEqual(candidate["source"], "xiaohongshu")
        self.assertEqual(
            candidate["url"],
            "https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567"
            "?xsec_token=fixture-token&xsec_source=pc_search",
        )
        self.assertEqual(candidate["text"], "今天沿溪流走了两小时。")
        self.assertEqual(candidate["title"], "山路")
        self.assertEqual(candidate["likes"], "12")

    async def test_note_command_failure_is_explicit(self):
        raw = {
            "process_ok": False,
            "data": {"error": {"code": "AUTH_REQUIRED"}},
            "error": {"code": "command_failed", "message": "login required"},
        }
        with patch.object(server, "run_command", new=AsyncMock(return_value=raw)):
            result = await server.xiaohongshu_note(SIGNED_URL)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["candidate"]["text"], "")
        self.assertEqual(result["candidate"]["body_status"], "unavailable")

    async def test_missing_content_never_uses_title_as_text(self):
        raw = {
            "process_ok": True,
            "data": [
                {"field": "title", "value": "只有标题"},
                {"field": "author", "value": "作者"},
            ],
            "parse_error": None,
        }
        with patch.object(server, "run_command", new=AsyncMock(return_value=raw)):
            result = await server.xiaohongshu_note(SIGNED_URL)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["candidate"]["title"], "只有标题")
        self.assertEqual(result["candidate"]["text"], "")
        self.assertNotEqual(result["candidate"]["text"], result["candidate"]["title"])

    async def test_rejects_bare_id_or_unsigned_url(self):
        with self.assertRaises(ValueError):
            await server.xiaohongshu_note("0123456789abcdef01234567")
        with self.assertRaises(ValueError):
            await server.xiaohongshu_note("https://www.xiaohongshu.com/explore/0123456789abcdef01234567")


class Issue3SearchCandidateTests(unittest.TestCase):
    def test_duplicate_search_hits_are_deduplicated(self):
        first = [{"title": "山路", "author": "A", "url": SIGNED_URL, "likes": "1"}]
        second = [{"title": "山路", "author": "A", "url": SIGNED_URL.replace("fixture-token", "another-fixture-token"), "likes": "1"}]
        candidates = server.dedupe_xiaohongshu_search_results([first, second])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["text"], "")
        self.assertEqual(candidates[0]["body_status"], "not_read")

    def test_search_normalization_keeps_jev_candidate_fields(self):
        normalized = server.normalize_adapter_result(
            "xiaohongshu",
            {
                "process_ok": True,
                "data": [{"title": "山路", "author": "A", "url": SIGNED_URL}],
                "parse_error": None,
            },
        )
        candidate = normalized["data"][0]
        for field in ("candidate_id", "title", "source", "url", "author", "text", "body_status"):
            self.assertIn(field, candidate)

    def test_nearby_evidence_deduplicates_same_note_across_queries(self):
        candidate = {
            "candidate_id": "0123456789abcdef01234567",
            "url": SIGNED_URL,
            "source": "xiaohongshu",
            "text": "",
        }
        evidence = [
            {"results": {"xiaohongshu": {"data": [candidate]}}},
            {"results": {"xiaohongshu": {"data": [candidate]}}},
        ]
        server.dedupe_xiaohongshu_evidence(evidence)
        self.assertEqual(len(evidence[0]["results"]["xiaohongshu"]["data"]), 1)
        self.assertEqual(evidence[1]["results"]["xiaohongshu"]["data"], [])

    def test_fixtures_do_not_contain_real_credentials_or_signed_urls(self):
        with open(__file__, encoding="utf-8") as fixture:
            source = fixture.read()
        self.assertNotRegex(source, r"sk-[A-Za-z0-9]{20,}")
        self.assertNotRegex(source, r"eyJ[A-Za-z0-9_-]{20,}")
        self.assertIn("fixture-token", source)


if __name__ == "__main__":
    unittest.main()

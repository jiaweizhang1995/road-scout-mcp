import os
import unittest
from unittest.mock import patch

import server


def candidate(text: str, *, likes: str = "12") -> dict:
    return {
        "candidate_id": "candidate-1",
        "title": "溪边路线",
        "source": "xiaohongshu",
        "url": "https://example.invalid/note",
        "author": "作者",
        "text": text,
        "comments": [],
        "likes": likes,
        "collects": "2",
        "published_at": None,
        "body_status": "ok",
    }


def answers(firsthand: float, marketing: float, fit: float) -> dict:
    return {
        "candidate_0_firsthand": {"type": "noul", "noul": firsthand},
        "candidate_0_marketing": {"type": "noul", "noul": marketing},
        "candidate_0_fit": {"type": "score", "score": fit, "confidence": 0.8, "probabilities": {}},
    }


class Issue5RankingTests(unittest.TestCase):
    def test_detailed_firsthand_content_with_fit_ranks_supported(self):
        result = server.rank_jev_results(
            [candidate("我周六早上八点到达，导航最后三公里是窄路。停车场收费十元，沿溪流步行约四十分钟，雨后石阶很滑。")],
            answers(0.92, 0.08, 2.8),
        )[0]
        self.assertEqual(result["evidence_status"], "supported")
        self.assertGreater(result["ranking"], 0.5)
        self.assertIn("第一手体验", result["reason"])

    def test_marketing_without_experience_is_filtered(self):
        result = server.rank_jev_results(
            [candidate("全网最低价，限时团购！私信领取优惠券，店长推荐，欢迎加微信预订。")],
            answers(0.12, 0.96, 1.0),
        )[0]
        self.assertEqual(result["evidence_status"], "filtered")
        self.assertLess(result["ranking"], 0)

    def test_detailed_cooperation_content_is_kept_with_risk(self):
        result = server.rank_jev_results(
            [candidate("本篇为合作体验。我们下午三点到，停车位在后门，收费二十元；房间隔音一般，步道夜间没有照明，建议带头灯。")],
            answers(0.86, 0.86, 2.4),
        )[0]
        self.assertEqual(result["evidence_status"], "marketing_risk")
        self.assertGreater(result["ranking"], -0.1)
        self.assertIn("具体体验", result["reason"])

    def test_short_or_missing_body_is_insufficient(self):
        short = candidate("好美，推荐！")
        short["body_status"] = "ok"
        missing = candidate("这是一段看似很长但仍然只是泛泛推荐的文字。")
        missing["body_status"] = "unavailable"
        results = server.rank_jev_results([short, missing], {})
        self.assertTrue(all(item["evidence_status"] == "insufficient" for item in results))
        self.assertTrue(all(item["firsthand"] is None for item in results))

    def test_low_likes_do_not_override_specific_evidence(self):
        result = server.rank_jev_results(
            [candidate("我从北门进入，九点前不用排队；停车场离入口五百米，门票三十元。下午人变多，返程路段信号很弱。", likes="0")],
            answers(0.9, 0.1, 2.5),
        )[0]
        self.assertEqual(result["evidence_status"], "supported")

    def test_explicit_preferences_are_passed_to_fit_question(self):
        candidate_data = [candidate("我晚上九点到达，地铁站步行十分钟，现场很热闹但排队二十分钟。")]
        quiet_payload, _ = server.build_jev_payload(candidate_data, ["安静", "人少", "适合自驾"])
        lively_payload, _ = server.build_jev_payload(candidate_data, ["热闹", "公共交通方便"])
        self.assertEqual(quiet_payload["state"]["user_preferences"], ["安静", "人少", "适合自驾"])
        self.assertEqual(lively_payload["state"]["user_preferences"], ["热闹", "公共交通方便"])
        self.assertIn("热闹", str(lively_payload["questions"]))
        self.assertNotEqual(quiet_payload["questions"], lively_payload["questions"])


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, headers, json):
        self.payload = json
        return self.response


class Issue5JevIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_jev_tool_sends_original_text_and_three_dimensions(self):
        source_candidate = candidate("我从停车场走到溪边用了二十分钟，门票十元，雨后路滑，附近没有手机信号。", likes="0")
        client = FakeClient(FakeResponse({"model": "jev-test", "answers": answers(0.9, 0.1, 2.5)}))
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=False), patch.object(
            server.httpx, "AsyncClient", return_value=client
        ):
            result = await server.jev_rank_candidates([source_candidate], ["安静", "适合自驾"])
        self.assertTrue(result["ok"])
        self.assertEqual(client.payload["state"]["candidates"][0]["text"], source_candidate["text"])
        self.assertEqual(
            set(client.payload["questions"]),
            {"candidate_0_firsthand", "candidate_0_marketing", "candidate_0_fit"},
        )
        self.assertEqual(result["results"][0]["evidence_status"], "supported")


if __name__ == "__main__":
    unittest.main()

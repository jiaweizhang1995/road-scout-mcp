import os
import re
import time
import unittest
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import server


def signed_url(note_id: str, token: str = "fixture-token") -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source="


def public_url(note_id: str, token: str = "fixture-token") -> str:
    return (
        f"https://www.xiaohongshu.com/discovery/item/{note_id}"
        f"?xsec_token={quote(token, safe='')}&xsec_source=pc_search"
    )


def xhs_row(note_id: str, title: str, token: str = "fixture-token") -> dict:
    return {
        "rank": 1,
        "author": "旅行者",
        "title": title,
        "url": signed_url(note_id, token),
        "likes": "100",
        "published_at": "2026-09-01",
    }


def ok_raw(data) -> dict:
    return {"process_ok": True, "data": data, "parse_error": None, "ok": True}


def fail_raw(message: str = "adapter failed") -> dict:
    return {
        "process_ok": False,
        "data": None,
        "parse_error": None,
        "error": {"code": "command_failed", "message": message},
    }


def note_raw(title: str, content: str) -> dict:
    return ok_raw(
        [
            {"field": "title", "value": title},
            {"field": "author", "value": "作者"},
            {"field": "content", "value": content},
            {"field": "likes", "value": "12"},
        ]
    )


def comments_raw(texts: list[str]) -> dict:
    return ok_raw(
        [
            {
                "rank": index + 1,
                "author": "评论者",
                "text": text,
                "likes": 0,
                "time": "1天前",
                "is_reply": False,
                "reply_to": "",
            }
            for index, text in enumerate(texts)
        ]
    )


def web_raw(entries: list[tuple[str, str, str]]) -> dict:
    text = "\n".join(
        f"Title: {title}\nURL: {url}\nPublished: N/A\nAuthor: N/A\nHighlights:\n{body}"
        for title, url, body in entries
    )
    return ok_raw({"content": [{"type": "text", "text": text}]})


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeJevClient:
    """Answer Jev questions from a per-candidate scorer function."""

    def __init__(self, scorer):
        self.scorer = scorer
        self.payloads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, headers, json):
        self.payloads.append(json)
        answers = {}
        for key in json["questions"]:
            match = re.match(r"candidate_(\d+)_(firsthand|marketing|fit)", key)
            index, dimension = int(match.group(1)), match.group(2)
            firsthand, marketing, fit = self.scorer(json["state"]["candidates"][index])
            if dimension == "fit":
                answers[key] = {"type": "score", "score": fit}
            else:
                value = firsthand if dimension == "firsthand" else marketing
                answers[key] = {"type": "noul", "noul": value}
        return FakeResponse({"model": "jev-test", "answers": answers})


def default_scorer(candidate: dict) -> tuple[float, float, float]:
    blob = " ".join(
        [
            candidate.get("title") or "",
            candidate.get("text") or "",
            " ".join(str(item) for item in candidate.get("comment_evidence") or []),
        ]
    )
    marketing = 0.9 if re.search(r"团购|私信|合作|优惠|预订", blob) else 0.1
    if re.search(r"停车|门票|路线|公里|小时|排队|价格|收费|到达|爬升", blob):
        firsthand = 0.85
    elif len(blob) > 40:
        firsthand = 0.45
    else:
        firsthand = 0.2
    fit = 2.6 if re.search(r"山|草甸|民宿|村|溪|营地|徒步|小众", blob) else 1.4
    return firsthand, marketing, fit


GOOD_BODY = "周六早上八点到达山顶草甸，村口停车场收费十元，沿山脊徒步约三公里，人少安静。"
GAP_BODY = "周末去了山顶草甸，草甸非常开阔，风车很有辨识度，下午光线柔和，人也不多，适合放空。"


class Issue7FlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        *,
        xhs=(),
        notes=None,
        comments=(),
        bilibili=(),
        douyin=(),
        douyin_exc=None,
        web=(),
        gaode=None,
        jev_client=None,
        jev_off=False,
        origin=None,
        geocoder=None,
        **tool_kwargs,
    ):
        notes = notes or {}
        tool_kwargs.setdefault("request", "杭州附近小众自驾")
        tool_kwargs.setdefault("area_name", "杭州")

        def note_for(url):
            match = re.search(r"/(?:explore|note|search_result)/([a-f0-9]+)", url)
            return notes.get(match.group(1) if match else url, note_raw("默认标题", GOOD_BODY))

        douyin_mock = (
            AsyncMock(side_effect=douyin_exc)
            if douyin_exc
            else AsyncMock(return_value=ok_raw(list(douyin)))
        )
        comments_mock = AsyncMock(return_value=comments_raw(list(comments)))
        note_mock = AsyncMock(side_effect=note_for)
        gaode_mock = AsyncMock(return_value=gaode or {"ok": True, "items": []})

        async with AsyncExitStack() as stack:
            stack.enter_context(
                patch.object(server, "search_xiaohongshu", new=AsyncMock(return_value=ok_raw(list(xhs))))
            )
            stack.enter_context(
                patch.object(server, "search_bilibili", new=AsyncMock(return_value=ok_raw(list(bilibili))))
            )
            stack.enter_context(patch.object(server, "search_douyin", new=douyin_mock))
            stack.enter_context(
                patch.object(server, "search_web", new=AsyncMock(return_value=web_raw(list(web))))
            )
            stack.enter_context(patch.object(server, "fetch_xiaohongshu_note", new=note_mock))
            stack.enter_context(patch.object(server, "fetch_xiaohongshu_comments", new=comments_mock))
            stack.enter_context(patch.object(server, "fetch_gaode_food_ranking", new=gaode_mock))
            stack.enter_context(
                patch.object(server, "geocode_area", new=AsyncMock(return_value=origin))
            )
            geo_map = geocoder or {}
            stack.enter_context(
                patch.object(
                    server,
                    "geocode_place",
                    new=AsyncMock(side_effect=lambda place, area="": geo_map.get(place)),
                )
            )
            env = {"TYPESAFE_API_KEY": ""} if jev_off else {"TYPESAFE_API_KEY": "test-key"}
            stack.enter_context(patch.dict(os.environ, env))
            if jev_client is not None:
                stack.enter_context(
                    patch.object(server.httpx, "AsyncClient", return_value=jev_client)
                )
            result = await server.road_scout_recommend(**tool_kwargs)
        return result, {"notes": note_mock, "comments": comments_mock, "gaode": gaode_mock}

    async def test_full_flow_produces_recommendations(self):
        result, mocks = await self._run(
            xhs=[
                xhs_row("aa" * 12, "杭州出发山顶草甸徒步"),
                xhs_row("bb" * 12, "杭州周边小众溪谷营地"),
            ],
            notes={
                "aa" * 12: note_raw("龙溪草甸", GOOD_BODY),
                "bb" * 12: note_raw("小众溪谷", "周日到达溪谷营地，门口免费停车，门票二十元，下午人很少。"),
            },
            bilibili=[{"rank": 1, "title": "杭州周边越野地盘点", "author": "up主", "score": 100, "url": "https://www.bilibili.com/video/BV1"}],
            douyin=[{"rank": 1, "desc": "桐庐山野自驾，周末人少", "author": "dy", "url": "https://www.douyin.com/video/1", "likes": 5}],
            web=[("杭州周边山野攻略", "https://example.com/a", "介绍了几个山头，停车场免费。")],
            jev_client=FakeJevClient(default_scorer),
            max_results=5,
        )
        urls = [rec["url"] for rec in result["recommendations"]]
        self.assertIn(public_url("aa" * 12), urls)
        self.assertIn(public_url("bb" * 12), urls)
        self.assertGreaterEqual(len(result["recommendations"]), 2)
        for rec in result["recommendations"]:
            self.assertEqual(rec["evidence_status"], "supported")
            self.assertTrue(rec["url"].startswith("http"))
            self.assertTrue(rec["reason"])
            self.assertTrue(rec["key_evidence"])
            self.assertNotIn("raw_search_result", rec)
        self.assertEqual(mocks["notes"].await_count, 2)
        self.assertEqual(result["source_status"]["xiaohongshu"], "ok")

    async def test_one_source_failure_keeps_others(self):
        result, _ = await self._run(
            xhs=[xhs_row("cc" * 12, "山顶草甸")],
            notes={"cc" * 12: note_raw("龙溪草甸", GOOD_BODY)},
            douyin_exc=RuntimeError("douyin offline"),
            jev_client=FakeJevClient(default_scorer),
        )
        self.assertEqual(result["source_status"]["douyin"], "unavailable")
        self.assertTrue(any("douyin" in note for note in result["notes"]))
        self.assertEqual(len(result["recommendations"]), 1)

    async def test_missing_body_cannot_be_formally_recommended(self):
        result, _ = await self._run(
            xhs=[xhs_row("dd" * 12, "读取失败的笔记")],
            notes={"dd" * 12: fail_raw("login required")},
            jev_client=FakeJevClient(default_scorer),
        )
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(len(result["exploratory"]), 1)
        self.assertEqual(result["exploratory"][0]["evidence_status"], "insufficient")

    async def test_gap_candidate_triggers_one_comment_fetch(self):
        note_id = "ee" * 12
        jev = FakeJevClient(default_scorer)
        result, mocks = await self._run(
            xhs=[xhs_row(note_id, "山顶草甸放空")],
            notes={note_id: note_raw("山顶草甸", GAP_BODY)},
            comments=["村口就有停车场，收费十元，周末上午车位充足。"],
            jev_client=jev,
        )
        mocks["comments"].assert_awaited_once()
        called_url = mocks["comments"].await_args.args[0]
        self.assertEqual(called_url, signed_url(note_id))
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertTrue(any("停车" in e for e in result["recommendations"][0]["key_evidence"]))
        self.assertEqual(len(jev.payloads), 2)  # second pass re-ranks the updated candidate
        self.assertIn("comment_evidence", jev.payloads[1]["state"]["candidates"][0])

    async def test_complete_body_does_not_fetch_comments(self):
        result, mocks = await self._run(
            xhs=[xhs_row("ff" * 12, "山顶草甸")],
            notes={"ff" * 12: note_raw("山顶草甸", GOOD_BODY)},
            comments=["不应被拉取的评论"],
            jev_client=FakeJevClient(default_scorer),
        )
        mocks["comments"].assert_not_awaited()
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertEqual(result["stats"]["followups"], 0)

    async def test_marketing_without_firsthand_is_filtered(self):
        result, _ = await self._run(
            xhs=[
                xhs_row("11" * 12, "山顶草甸徒步"),
                xhs_row("22" * 12, "杭州民宿特惠"),
            ],
            notes={
                "11" * 12: note_raw("山顶草甸", GOOD_BODY),
                "22" * 12: note_raw("民宿特惠", "全网最低价限时团购，私信领取优惠券，加微信预订立减"),
            },
            jev_client=FakeJevClient(default_scorer),
        )
        urls = [rec["url"] for rec in result["recommendations"]]
        self.assertEqual(urls, [public_url("11" * 12)])
        self.assertEqual(result["stats"]["filtered"], 1)
        exploratory_urls = [item["url"] for item in result["exploratory"]]
        self.assertNotIn(public_url("22" * 12), exploratory_urls)

    async def test_marketing_risk_kept_with_risk_note(self):
        result, _ = await self._run(
            xhs=[xhs_row("33" * 12, "山居民宿合作体验")],
            notes={
                "33" * 12: note_raw(
                    "山居民宿",
                    "本篇为合作体验。下午三点到达山居民宿，停车位在后门，收费二十元；房间隔音一般，步道夜间无照明。",
                )
            },
            jev_client=FakeJevClient(default_scorer),
        )
        self.assertEqual(len(result["recommendations"]), 1)
        rec = result["recommendations"][0]
        self.assertEqual(rec["evidence_status"], "marketing_risk")
        self.assertTrue(any("营销" in risk for risk in rec["risks"]))

    async def test_max_results_caps_and_never_pads(self):
        places = ["龙溪草甸", "千岛湖", "大明山", "青山湖", "天目山", "径山寺"]
        rows = [xhs_row(f"{index:024x}", f"候选{index}") for index in range(6)]
        result, _ = await self._run(
            xhs=rows,
            notes={
                f"{index:024x}": note_raw(places[index], GOOD_BODY) for index in range(6)
            },
            jev_client=FakeJevClient(default_scorer),
            max_results=3,
        )
        self.assertEqual(len(result["recommendations"]), 3)

        result, _ = await self._run(
            xhs=[xhs_row("44" * 12, "好候选"), xhs_row("55" * 12, "坏候选")],
            notes={
                "44" * 12: note_raw("好候选", GOOD_BODY),
                "55" * 12: fail_raw("missing"),
            },
            jev_client=FakeJevClient(default_scorer),
            max_results=5,
        )
        self.assertEqual(len(result["recommendations"]), 1)

    async def test_same_note_deduped_across_queries(self):
        note_id = "66" * 12
        calls = iter(range(10))

        async def per_query(query, limit):
            return ok_raw([xhs_row(note_id, "山顶草甸", token=f"tok-{next(calls)}")])

        async with AsyncExitStack() as stack:
            stack.enter_context(patch.object(server, "search_xiaohongshu", new=AsyncMock(side_effect=per_query)))
            stack.enter_context(patch.object(server, "search_bilibili", new=AsyncMock(return_value=ok_raw([]))))
            stack.enter_context(patch.object(server, "search_douyin", new=AsyncMock(return_value=ok_raw([]))))
            stack.enter_context(patch.object(server, "search_web", new=AsyncMock(return_value=ok_raw({"content": []}))))
            note_mock = AsyncMock(return_value=note_raw("山顶草甸", GOOD_BODY))
            stack.enter_context(patch.object(server, "fetch_xiaohongshu_note", new=note_mock))
            stack.enter_context(patch.object(server, "fetch_xiaohongshu_comments", new=AsyncMock(return_value=comments_raw([]))))
            stack.enter_context(patch.object(server, "fetch_gaode_food_ranking", new=AsyncMock(return_value={"ok": True, "items": []})))
            stack.enter_context(patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}))
            stack.enter_context(patch.object(server.httpx, "AsyncClient", return_value=FakeJevClient(default_scorer)))
            result = await server.road_scout_recommend(request="杭州小众", area_name="杭州")
        self.assertEqual(result["stats"]["candidates"], 1)
        self.assertEqual(note_mock.await_count, 1)

    async def test_food_request_fetches_gaode(self):
        result, mocks = await self._run(
            xhs=[xhs_row("77" * 12, "山顶草甸")],
            notes={"77" * 12: note_raw("山顶草甸", GOOD_BODY)},
            gaode={"ok": True, "items": [{"rank": 1, "name": "本地面馆", "score": "综合分4.5", "tags": None, "highlight": None, "url": "https://www.amap.com/x"}]},
            jev_client=FakeJevClient(default_scorer),
            request="杭州 周末吃什么",
        )
        mocks["gaode"].assert_awaited_once()
        self.assertTrue(result["food"]["ok"])
        self.assertEqual(result["food"]["items"][0]["name"], "本地面馆")

    async def test_food_returned_even_when_social_pool_empty(self):
        result, mocks = await self._run(
            gaode={
                "ok": True,
                "items": [{"rank": 1, "name": "本地面馆", "score": "综合分4.5", "tags": None, "highlight": None, "url": "https://www.amap.com/x"}],
            },
            request="杭州吃什么",
        )
        mocks["gaode"].assert_awaited_once()
        self.assertEqual(result["recommendations"], [])
        self.assertTrue(result["food"]["ok"])
        self.assertEqual(result["food"]["items"][0]["name"], "本地面馆")

    async def test_recommendation_url_is_public_but_comments_use_signed(self):
        note_id = "88" * 12
        result, mocks = await self._run(
            xhs=[xhs_row(note_id, "山顶草甸放空", token="tok=fixture=")],
            notes={note_id: note_raw("山顶草甸", GAP_BODY)},
            comments=["村口就有停车场，收费十元。"],
            jev_client=FakeJevClient(default_scorer),
        )
        rec = result["recommendations"][0]
        self.assertEqual(rec["url"], public_url(note_id, "tok=fixture="))
        self.assertIn("%3D", rec["url"])
        # internal comment fetch still received the original signed URL
        self.assertEqual(mocks["comments"].await_args.args[0], signed_url(note_id, "tok=fixture="))

    async def test_radius_from_request_moves_far_candidate_to_exploratory(self):
        result, _ = await self._run(
            xhs=[
                xhs_row("a1" * 12, "龙溪草甸徒步"),
                xhs_row("b2" * 12, "千岛湖自驾"),
            ],
            notes={
                "a1" * 12: note_raw("龙溪草甸", GOOD_BODY),
                "b2" * 12: note_raw("千岛湖", "周六到达千岛湖，湖边免费停车，门票一百元，人少。"),
            },
            jev_client=FakeJevClient(default_scorer),
            request="杭州附近100公里 小众自驾",
            origin=(30.27, 120.15, "杭州"),
            geocoder={
                "龙溪草甸": (30.35, 120.25, "龙溪草甸"),
                "千岛湖": (29.60, 119.00, "千岛湖"),
            },
        )
        self.assertEqual(result["geo"]["radius_km"], 100.0)
        self.assertEqual(result["geo"]["origin"]["latitude"], 30.27)
        self.assertEqual(len(result["recommendations"]), 1)
        near = result["recommendations"][0]
        self.assertIsNotNone(near["distance_km"])
        self.assertLess(near["distance_km"], 50)
        self.assertEqual(len(result["exploratory"]), 1)
        far = result["exploratory"][0]
        self.assertGreater(far["distance_km"], 100)
        self.assertEqual(far["evidence_status"], "supported")
        self.assertTrue(any("超出" in risk for risk in far["risks"]))
        self.assertEqual(result["stats"]["out_of_range"], 1)

    async def test_out_of_range_candidate_skips_followup(self):
        note_id = "c3" * 12
        result, mocks = await self._run(
            xhs=[xhs_row(note_id, "千岛湖放空")],
            notes={note_id: note_raw("千岛湖", "周末去了千岛湖，湖面开阔，光线柔和，人也不多，适合发呆。")},
            comments=["不会被拉取的评论"],
            jev_client=FakeJevClient(default_scorer),
            request="杭州附近50公里 小众",
            origin=(30.27, 120.15, "杭州"),
            geocoder={"千岛湖": (29.60, 119.00, "千岛湖")},
        )
        mocks["comments"].assert_not_awaited()
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(len(result["exploratory"]), 1)
        self.assertGreater(result["exploratory"][0]["distance_km"], 50)

    async def test_explicit_radius_and_latlon_override_request_text(self):
        result, _ = await self._run(
            xhs=[xhs_row("d4" * 12, "龙溪草甸徒步")],
            notes={"d4" * 12: note_raw("龙溪草甸", GOOD_BODY)},
            jev_client=FakeJevClient(default_scorer),
            request="杭州附近500米 小众自驾",
            radius_km=50,
            latitude=30.27,
            longitude=120.15,
            geocoder={"龙溪草甸": (30.35, 120.25, "龙溪草甸")},
        )
        # explicit radius_km=50 wins over "500米" in the request
        self.assertEqual(result["geo"]["radius_km"], 50.0)
        self.assertEqual(result["geo"]["origin"]["label"], "指定坐标")
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertIsNotNone(result["recommendations"][0]["distance_km"])

    async def test_unresolved_distance_is_never_filtered(self):
        result, _ = await self._run(
            xhs=[xhs_row("e5" * 12, "龙溪草甸徒步")],
            notes={"e5" * 12: note_raw("龙溪草甸", GOOD_BODY)},
            jev_client=FakeJevClient(default_scorer),
            request="杭州附近100公里 小众",
            origin=(30.27, 120.15, "杭州"),
            geocoder={},  # place cannot be resolved
        )
        self.assertEqual(len(result["recommendations"]), 1)
        rec = result["recommendations"][0]
        self.assertIsNone(rec["distance_km"])
        self.assertTrue(any("距离未知" in risk for risk in rec["risks"]))

    async def test_radius_without_origin_leaves_note(self):
        result, _ = await self._run(
            xhs=[xhs_row("f6" * 12, "龙溪草甸徒步")],
            notes={"f6" * 12: note_raw("龙溪草甸", GOOD_BODY)},
            jev_client=FakeJevClient(default_scorer),
            request="附近100公里 小众地方",
            area_name="",
        )
        self.assertTrue(any("距离" in note for note in result["notes"]))
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertIsNone(result["recommendations"][0]["distance_km"])
        self.assertIsNone(result["geo"]["origin"])

    async def test_same_place_multiple_notes_collapse(self):
        result, _ = await self._run(
            xhs=[
                xhs_row("07" * 12, "龙溪草甸徒步记"),
                xhs_row("08" * 12, "龙溪草甸露营攻略"),
            ],
            notes={
                "07" * 12: note_raw("龙溪草甸", GOOD_BODY),
                "08" * 12: note_raw("龙溪草甸", "周日到达龙溪草甸，草地很大，停车免费，门票无。"),
            },
            jev_client=FakeJevClient(default_scorer),
        )
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertEqual(result["recommendations"][0]["mentions"], 2)


class Issue7GeoTests(unittest.TestCase):
    def test_parse_radius_kilometers(self):
        self.assertEqual(server._parse_radius_km("杭州附近100公里"), 100.0)
        self.assertEqual(server._parse_radius_km("周边 30km 自驾"), 30.0)
        self.assertEqual(server._parse_radius_km("50千米以内"), 50.0)

    def test_parse_radius_meters(self):
        self.assertEqual(server._parse_radius_km("附近500米"), 0.5)
        self.assertEqual(server._parse_radius_km("2公里"), 2.0)  # km wins over 米 in 公里

    def test_parse_radius_no_match(self):
        self.assertIsNone(server._parse_radius_km("附近有什么好玩的"))
        self.assertIsNone(server._parse_radius_km(""))

    def test_haversine_known_distance(self):
        # 杭州 -> 上海 ~165km
        distance = server._haversine_km(30.27, 120.15, 31.23, 121.47)
        self.assertTrue(150 < distance < 185)

    def test_dedupe_by_place_substring_collapse(self):
        def cand(place, rank):
            return {"_place": place, "ranking": rank}

        ranked = [cand("龙溪草甸", 0.9), cand("龙溪草甸景区", 0.7), cand("千岛湖", 0.5)]
        deduped = server._dedupe_by_place(ranked)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["_place"], "龙溪草甸")
        self.assertEqual(deduped[0]["mentions"], 2)

    def test_dedupe_by_place_keeps_placeless_and_distinct(self):
        ranked = [
            {"_place": "", "ranking": 0.9},
            {"_place": "甲山村", "ranking": 0.8},
            {"_place": "乙湖", "ranking": 0.7},  # len<3 -> never deduped
        ]
        self.assertEqual(len(server._dedupe_by_place(ranked)), 3)

    def test_wgs84_to_gcj02_shifts_inside_china(self):
        lat, lon = server._wgs84_to_gcj02(30.27, 120.15)
        self.assertNotEqual((lat, lon), (30.27, 120.15))
        # GCJ-02 offset is hundreds of meters, never tens of kilometers
        self.assertLess(server._haversine_km(30.27, 120.15, lat, lon), 2.0)

    def test_wgs84_to_gcj02_noop_outside_china(self):
        self.assertEqual(server._wgs84_to_gcj02(51.5, -0.12), (51.5, -0.12))

    def test_extract_place_never_rewrites_real_names(self):
        # 藏马山 must stay 藏马山 (char-level stripping would produce 马山 and
        # could match an unrelated POI); 西溪湿地公园 must not be emptied by
        # the bad-char filter (地 is a legitimate name character).
        self.assertEqual(
            server._extract_place({"title": "藏马山徒步攻略", "text": ""}), "藏马山"
        )
        self.assertEqual(
            server._extract_place({"title": "西溪湿地公园", "text": ""}), "西溪湿地公园"
        )

    def test_extract_place_text_fragments_rejected(self):
        # sentence fragments in the body are not place names
        self.assertEqual(
            server._extract_place({"title": "随便逛逛", "text": "沿着山脊走了很久"}), ""
        )
        # but a real place in the body still surfaces when the title has none
        self.assertEqual(
            server._extract_place({"title": "周末出游", "text": "去了西溪湿地公园，人少安静"}),
            "西溪湿地公园",
        )


class Issue7GeoAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved_geo_ts = dict(server._last_geo_call)

    async def asyncTearDown(self):
        server._last_geo_call.clear()
        server._last_geo_call.update(self._saved_geo_ts)

    def _nominatim_client(self, rows):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url, params=None):
                return FakeResponse(rows)

        return Client()

    async def test_nominatim_min_interval_enforced(self):
        server._last_geo_call["nominatim"] = time.monotonic()  # pretend a call just happened
        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock, patch.object(
            server.httpx, "AsyncClient", return_value=self._nominatim_client([])
        ):
            await server._nominatim_geocode("杭州", ("杭州",))
        sleep_mock.assert_awaited_once()
        self.assertGreater(sleep_mock.await_args.args[0], 0.5)

    async def test_nominatim_no_sleep_after_interval(self):
        server._last_geo_call.pop("nominatim", None)  # long ago
        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock, patch.object(
            server.httpx, "AsyncClient", return_value=self._nominatim_client([])
        ):
            await server._nominatim_geocode("杭州", ("杭州",))
        sleep_mock.assert_not_awaited()

    async def test_amap_retries_transient_failure(self):
        calls = []

        class FlakyClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url, params=None):
                calls.append(url)
                if len(calls) == 1:
                    raise httpx_error()
                return FakeResponse({"status": "1", "pois": []})

        def httpx_error():
            import httpx
            return httpx.ConnectError("boom")

        with patch.object(server, "AMAP_API_KEY", "key"), patch.object(
            server.httpx, "AsyncClient", return_value=FlakyClient()
        ), patch("asyncio.sleep", new=AsyncMock()):
            body = await server._amap_get("/v3/place/text", {})
        self.assertEqual(len(calls), 2)  # transient failure retried once
        self.assertEqual(body["status"], "1")

        class OkClient(FlakyClient):
            async def get(self, url, params=None):
                calls.append(url)
                return FakeResponse({"status": "1", "pois": [{"name": "鼓山", "location": "119.3,26.0"}]})

        calls.clear()
        with patch.object(server, "AMAP_API_KEY", "key"), patch.object(
            server.httpx, "AsyncClient", return_value=OkClient()
        ), patch("asyncio.sleep", new=AsyncMock()):
            body = await server._amap_get("/v3/place/text", {})
        self.assertEqual(body["pois"][0]["name"], "鼓山")
        self.assertEqual(len(calls), 1)

    async def test_explicit_origin_converted_to_gcj02_under_amap(self):
        with patch.object(server, "AMAP_API_KEY", "test-amap-key"):
            lat, lon, label = await server._resolve_origin(30.27, 120.15, "")
        self.assertEqual(label, "指定坐标")
        self.assertNotEqual((lat, lon), (30.27, 120.15))
        self.assertLess(server._haversine_km(30.27, 120.15, lat, lon), 2.0)

    async def test_explicit_origin_unchanged_under_nominatim(self):
        with patch.object(server, "AMAP_API_KEY", ""):
            lat, lon, _ = await server._resolve_origin(30.27, 120.15, "")
        self.assertEqual((lat, lon), (30.27, 120.15))

    async def test_amap_fuzzy_mismatch_rejected(self):
        body = {
            "status": "1",
            "pois": [{"name": "磨溪景区", "location": "119.413575,26.051775"}],
        }
        with patch.object(server, "AMAP_API_KEY", "key"), patch.object(
            server, "_amap_get", new=AsyncMock(return_value=body)
        ):
            # "龙溪草甸" must not silently become the unrelated 磨溪景区
            self.assertIsNone(await server.geocode_place("龙溪草甸", "福州"))

        body["pois"] = [{"name": "福州市鼓山旅游景区", "location": "119.375610,26.053221"}]
        with patch.object(server, "AMAP_API_KEY", "key"), patch.object(
            server, "_amap_get", new=AsyncMock(return_value=body)
        ):
            result = await server.geocode_place("鼓山", "福州")
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "福州市鼓山旅游景区")


class Issue7PublicUrlTests(unittest.TestCase):
    def test_search_result_url_becomes_discovery_item(self):
        url = (
            "https://www.xiaohongshu.com/search_result/6aa5b9ac000000002802c70d"
            "?xsec_token=ABwFzZtJo0HZhfis6qqKFMo4ZnD6isDCbJOd9roKWLcTM=&xsec_source="
        )
        self.assertEqual(
            server._public_url(url),
            "https://www.xiaohongshu.com/discovery/item/6aa5b9ac000000002802c70d"
            "?xsec_token=ABwFzZtJo0HZhfis6qqKFMo4ZnD6isDCbJOd9roKWLcTM%3D&xsec_source=pc_search",
        )

    def test_explore_and_already_public_urls_normalize(self):
        url = "https://www.xiaohongshu.com/explore/aa11bb22cc33dd44ee55ff66?xsec_token=tok==&xsec_source=pc_search"
        self.assertEqual(
            server._public_url(url),
            "https://www.xiaohongshu.com/discovery/item/aa11bb22cc33dd44ee55ff66"
            "?xsec_token=tok%3D%3D&xsec_source=pc_search",
        )
        # idempotent: a public URL rewrites to itself
        self.assertEqual(server._public_url(server._public_url(url)), server._public_url(url))

    def test_non_xhs_and_non_note_urls_pass_through(self):
        bilibili = "https://www.bilibili.com/video/BV129ReB1ExM"
        self.assertEqual(server._public_url(bilibili), bilibili)
        profile = "https://www.xiaohongshu.com/user/profile/597429cf6a6a69287949f707?xsec_token=abc="
        self.assertEqual(server._public_url(profile), profile)
        no_token = "https://www.xiaohongshu.com/explore/aa11bb22cc33dd44ee55ff66"
        self.assertEqual(server._public_url(no_token), no_token)
        self.assertIsNone(server._public_url(None))


if __name__ == "__main__":
    unittest.main()

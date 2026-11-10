import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import agent, macro, market, timeline
from src.config import Settings
from src.research import validate_research
from src.storage import Storage
from src.web import create_app

SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="secret", exa_api_key="key")
NOW = datetime(2026, 11, 6, 8, 30, tzinfo=market.TZ)


def reply(payload):
    return {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": [], "usage": {}}


class MacroTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "t.db")

    @patch("src.macro.market.now", return_value=NOW)
    @patch("src.macro.industry.holding_industries", return_value={"电池", "白酒"})
    @patch("src.agent.model_client.chat")
    @patch("src.macro.news.search_news")
    def test_collect_keeps_real_links_and_flags_unchecked_numbers(self, search, chat, *_):
        search.return_value = {"results": [{"title": "央行降准", "url": "https://news.test/a", "domain": "news.test", "date": "2026-11-05", "text": "下调 0.5 个百分点"}]}
        chat.return_value = reply({"events": [{"id": "N1", "kind": "货币政策", "title": "央行降准", "summary": "下调 0.5 个百分点，释放 9999 亿元",
                                               "industries": ["电池"], "reason": "资金成本下降传导到扩产融资"}]})
        self.assertEqual(macro.collect(self.store, SETTINGS), 1)
        # The same link found again the next day is not stored twice.
        self.assertEqual(macro.collect(self.store, SETTINGS), 0)
        [event] = self.store.macro_events("2026-01-01")
        self.assertEqual((event["url"], event["industries"]), ("https://news.test/a", ["电池"]))
        self.assertEqual(event["unverified"], ["9999"])

    def test_validator_rejects_what_the_model_cannot_choose(self):
        check = macro.validator({"N1"}, {"电池"}, 2)
        good = {"id": "N1", "kind": "贸易", "title": "t", "summary": "s", "industries": ["电池"], "reason": "r"}
        self.assertEqual(check({"events": [good]}, {})["events"][0]["industries"], ["电池"])
        for bad in ({**good, "id": "N9"}, {**good, "kind": "利好"}, {**good, "industries": ["不存在"]}):
            with self.subTest(bad=bad), self.assertRaises(agent.OutputError):
                check({"events": [bad]}, {})

    @patch("src.macro.market.now", return_value=NOW)
    @patch("src.macro.industry.holding_industries", return_value={"电池"})
    @patch("src.macro.news.search_news", side_effect=macro.news.NewsError("无法连接"))
    def test_search_failure_leaves_nothing_stored(self, *_):
        self.assertEqual(macro.collect(self.store, SETTINGS), 0)

    def test_api_requires_login_and_returns_holding_concepts(self):
        self.store.add("alice", "password")
        self.store.set_holdings("alice", [{"code": "sz300750"}])
        self.store.save_company_board("300750", "宁德时代", {"sector": "电力设备", "industry": {"code": "BK1", "name": "电池"},
                                                         "concepts": [{"code": "BK2", "name": "储能"}]})
        self.store.add_macro_events([{"date": market.now().date().isoformat(), "kind": "贸易", "title": "t", "summary": "s", "reason": "r",
                                      "industries": ["电池"], "url": "https://x.test/1", "domain": "x.test", "unverified": []}])
        client = TestClient(create_app(self.store, settings=SETTINGS), base_url="https://testserver")
        self.assertEqual(client.get("/api/macro").status_code, 401)
        client.post("/api/login", json={"username": "alice", "password": "password"})
        view = client.get("/api/macro").json()
        self.assertEqual((len(view["events"]), view["concepts"]), (1, {"sz300750": ["储能"]}))

    @patch("src.timeline.market.now", return_value=NOW)
    @patch("src.agent.model_client.chat")
    def test_reanalyze_rewrites_text_and_keeps_node_ids(self, chat, _):
        node = lambda art, summary: {"date": "2026-09-15", "kind": "公告", "major": True, "title": art, "summary": summary, "analysis": "旧",
                                     "url": f"https://x.test/{art}", "art_code": art, "unverified": []}
        self.store.save_timeline("sz300750", "2025-11-01", "2026-11-01", [], [{"title": "基金", "theme": "投资扩产", "status": "进行中", "summary": "工具调用次数受限",
                                                                                "error": "", "steps": [], "unverified": [], "nodes": [node("A", "工具调用次数受限"), node("B", "保留")]}])
        thread = self.store.threads("sz300750")[0]
        first, second = thread["nodes"]
        chat.return_value = reply({"summary": "公司参与投资基金", "status": "已完成", "nodes": [{"id": f"A{first['id']}", "summary": "参与投资", "analysis": "新"}], "news": []})
        self.assertEqual(timeline.reanalyze(thread["id"], SETTINGS, self.store), 1)
        after = self.store.threads("sz300750")[0]
        self.assertEqual((after["summary"], after["status"]), ("公司参与投资基金", "已完成"))
        self.assertEqual([n["id"] for n in after["nodes"]], [first["id"], second["id"]])
        self.assertEqual([n["summary"] for n in after["nodes"]], ["参与投资", "保留"])

    def test_research_outlook_is_checked_like_other_conclusions(self):
        refs = {"R1": {"value": "12.5"}}
        base = {"title": "t", "paragraphs": [], "table": {}, "gaps": []}
        item = {"text": "若价格维持 12.5 元", "basis": "公告披露", "confidence": "中", "refs": ["R1"]}
        self.assertEqual(validate_research({**base, "outlook": [item]}, refs)["outlook"][0]["numbers"][0]["verification"], "已核对")
        self.assertEqual(validate_research(base, refs)["outlook"], [])
        for bad in ({**item, "confidence": "必涨"}, {**item, "refs": ["R9"]}, {"text": "缺依据", "confidence": "低", "refs": []}):
            with self.subTest(bad=bad), self.assertRaises(agent.OutputError):
                validate_research({**base, "outlook": [bad]}, refs)


if __name__ == "__main__":
    unittest.main()

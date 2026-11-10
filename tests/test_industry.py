import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import events, industry, market
from src.config import Settings
from src.storage import Storage
from src.web import create_app

SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="secret")


def boards(sector, code, name, concepts=()):
    return {"sector": sector, "industry": {"code": code, "name": name}, "concepts": [{"code": c, "name": n} for c, n in concepts]}


def link(name, side, sec="", kind="铜箔"):
    return {"side": side, "name": name, "sec_code": sec, "category": kind if side == "上游" else "", "segment": "" if side == "上游" else kind,
            "status": "确认", "date": "2026-10-01", "quote": f"{name}原文", "note": "", "signal": "", "source": "", "url": f"https://x/{name}",
            "unverified": [], "stage": "材料" if side == "上游" else "", "dependent": False}


class IndustryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "test.db")

    def test_parsers_keep_level_two_industry_and_precise_concepts(self):
        payload = {"ssbk": [{"BOARD_CODE": "1200", "BOARD_NAME": "电力设备", "BOARD_RANK": 1},
                            {"BOARD_CODE": "1033", "BOARD_NAME": "电池", "IS_PRECISE": 0, "BOARD_RANK": 2},
                            {"BOARD_CODE": "596", "BOARD_NAME": "融资融券", "IS_PRECISE": 0, "BOARD_RANK": 22},
                            {"BOARD_CODE": "968", "BOARD_NAME": "固态电池", "IS_PRECISE": "1", "BOARD_RANK": 35}]}
        self.assertEqual(events.parse_boards(payload), boards("电力设备", "BK1033", "电池", [("BK0968", "固态电池")]))
        changes = events.parse_board_changes({"data": {"diff": [{"f12": "BK1033", "f3": 1.634}, {"f12": "BK0968", "f3": "-"}]}})
        self.assertEqual(changes, {"BK1033": "1.63"})
        with self.assertRaises(events.EventError):
            events.parse_boards({"result": None})

    def test_layers_put_upstream_left_and_survive_cycles(self):
        level = industry.layers(["铜", "铜箔", "电池", "车", "A", "B"],
                                [("铜", "铜箔"), ("铜箔", "电池"), ("电池", "车"), ("铜", "电池"), ("A", "B"), ("B", "A")])
        self.assertEqual([level[n] for n in ("铜", "铜箔", "电池", "车")], [0, 1, 2, 3])
        self.assertEqual(abs(level["A"] - level["B"]), 1)

    @patch("src.industry.market.search_symbols", return_value=[("sh601127", "赛力斯"), ("sz000001", "平安银行")])
    @patch("src.industry.events.fetch_boards")
    def test_graph_aggregates_holdings_chains_into_industries(self, fetch, search):
        table = {"300750": boards("电力设备", "BK1033", "电池", [("BK0968", "固态电池"), ("BK0151", "甘肃板块")]),
                 "601212": boards("有色金属", "BK0478", "工业金属", [("BK0968", "固态电池"), ("BK0151", "甘肃板块")]),
                 "600869": boards("电力设备", "BK1033", "电池", [("BK0968", "固态电池")]),
                 "601127": boards("汽车", "BK1029", "乘用车", [("BK0968", "固态电池")])}
        fetch.side_effect = lambda sec: table[sec]
        business = {"report_date": "", "segments": [], "regions": []}
        self.store.save_chain("sz300750", "宁德时代", business, [], [
            link("远东股份", "上游", "600869"), link("白银有色", "上游", "601212"), link("赛力斯", "下游", kind="动力电池系统"),
            link("某未上市公司", "下游")])
        self.store.save_chain("sh601212", "白银有色", business, [], [])
        self.assertEqual(industry.refresh(self.store), 4)
        self.assertEqual(industry.refresh(self.store), 0)
        result = industry.graph(self.store, ["sz300750", "sh601212", "sh600519"])
        nodes = {n["name"]: n for n in result["nodes"]}
        self.assertEqual([h["name"] for h in nodes["电池"]["holdings"]], ["宁德时代"])
        self.assertEqual([e["company"] for e in nodes["电池"]["inner"]], ["远东股份"])
        self.assertEqual({(e["from"], e["to"]) for e in result["edges"]}, {("工业金属", "电池"), ("电池", "乘用车")})
        self.assertEqual([nodes[n]["layer"] for n in ("工业金属", "电池", "乘用车")], [0, 1, 2])
        edge = next(e for e in result["edges"] if e["to"] == "乘用车")
        self.assertEqual((edge["evidence"][0]["company"], edge["evidence"][0]["url"]), ("赛力斯", "https://x/赛力斯"))

        app = create_app(self.store, lambda codes: {}, settings=SETTINGS)
        with TestClient(app, base_url="https://testserver") as client:
            self.assertEqual(client.get("/api/industry").status_code, 401)
            self.store.add("alice", "password")
            client.post("/api/login", json={"username": "alice", "password": "password"})
            self.store.set_holdings("alice", [{"code": "sz300750"}, {"code": "sh601212"}])
            with patch("src.web.events.fetch_board_changes", return_value={"BK1033": "1.63"}) as quotes:
                view = client.get("/api/industry").json()
                client.get("/api/industry")
            self.assertEqual((len(view["nodes"]), view["changes"]), (3, {"BK1033": "1.63"}))
            quotes.assert_called_once()

    @patch("src.industry.market.now", return_value=datetime(2026, 11, 3, 8, 30, tzinfo=market.TZ))
    @patch("src.agent.model_client.chat")
    @patch("src.industry.news.search_news")
    def test_key_items_and_daily_industry_news(self, search, chat, clock):
        self.store.save_company_board("300750", "宁德时代", boards("电力设备", "BK1033", "电池"))
        self.store.save_company_board("601212", "白银有色", boards("有色金属", "BK0478", "工业金属"))
        self.store.save_company_board("600869", "远东股份", boards("电力设备", "BK1033", "电池"))
        self.store.save_company_board("002079", "德福科技", boards("有色金属", "BK0478", "工业金属"))
        business = {"report_date": "", "segments": [], "regions": []}
        signal = {**link("德福科技", "上游", "002079"), "date": "2026-10-28", "signal": "铜价上涨，加工费下调"}
        self.store.save_chain("sz300750", "宁德时代", business, [], [
            {**link("远东股份", "上游", "600869"), "date": "2026-10-30"}, signal, {**link("旧供应商", "上游", "601212"), "date": "2026-09-01"}])
        self.store.add("alice", "password")
        self.store.set_holdings("alice", [{"code": "sz300750"}, {"code": "sh601212"}])
        search.return_value = {"results": [
            {"title": "铜价创新高", "url": "https://news/copper", "domain": "news", "date": "2026-11-02", "text": "沪铜主力合约涨至 8.6 万元/吨"},
            {"title": "某公司股价上涨", "url": "https://news/stock", "domain": "news", "date": "2026-11-02", "text": ""}]}
        chat.side_effect = lambda *a, **k: {"content": json.dumps({"news": [{"id": "N1", "summary": "沪铜涨至 8.6 万元/吨，创新高"}]} if "工业金属" in a[0][-1]["content"]
                                                                    else {"news": []}, ensure_ascii=False), "tool_calls": [], "usage": {}}
        self.assertEqual(industry.collect_news(self.store, SETTINGS), 1)
        self.assertEqual(industry.collect_news(self.store, SETTINGS), 1)
        self.assertEqual(len(self.store.industry_news("2026-10-20")), 1)

        nodes = {n["name"]: n for n in industry.graph(self.store, ["sz300750", "sh601212"])["nodes"]}
        metal, battery = nodes["工业金属"], nodes["电池"]
        # The supplier with a price signal and the industry news outrank older or plain items; the September link is outside the window.
        self.assertEqual([i["kind"] for i in metal["items"]], ["站点", "行业新闻"])
        self.assertEqual(metal["items"][0]["signal"], "铜价上涨，加工费下调")
        self.assertEqual(metal["items"][1]["title"], "沪铜涨至 8.6 万元/吨，创新高")
        self.assertEqual([i["title"] for i in battery["items"]], ["远东股份："])
        self.assertEqual((metal["reach"], battery["reach"]), (["sh601212", "sz300750"], ["sh601212", "sz300750"]))


if __name__ == "__main__":
    unittest.main()

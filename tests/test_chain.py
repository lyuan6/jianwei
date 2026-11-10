import json
import tempfile
from datetime import datetime
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import agent, chain, events, market
from src.config import Settings
from src.storage import Storage

SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="secret", exa_api_key="exa")
BUSINESS = {"zygcfx": [
    {"REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "旧业务", "MBI_RATIO": 1, "GROSS_RPOFIT_RATIO": 0.1},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "动力电池系统", "MBI_RATIO": 0.693801, "GROSS_RPOFIT_RATIO": 0.206287},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "其中:细分", "MBI_RATIO": 0.3, "GROSS_RPOFIT_RATIO": 0.2},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "储能电池系统", "MBI_RATIO": 0.192336, "GROSS_RPOFIT_RATIO": 0.239577},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "3", "ITEM_NAME": "境内", "MBI_RATIO": 0.685359, "GROSS_RPOFIT_RATIO": 0.212},
]}


def hit(name, code, day, text, art):
    return {"securityFullName": name, "date": day + " 00:00:00", "title": f"{name}:公告", "content": text,
            "url": f"http://data.eastmoney.com/notices/detail/{code}/{art}.html"}


SEARCH = {"result": {"noticeWeb": [
    hit("远东股份", "600869", "2026-10-27", "<em>宁德时代</em>为公司锂电铜箔业务主要客户，2024年采购铜丝4.2亿元", "AN1"),
    hit("远东股份", "600869", "2026-09-30", "<em>宁德时代</em>指宁德时代新能源科技股份有限公司", "AN2"),
    hit("远东股份", "600869", "2026-08-01", "<em>宁德时代</em>为公司锂电铜箔业务主要客户，2024年采购铜丝4.2亿元", "AN3"),
    hit("华夏沪深300ETF", "510330", "2026-10-20", "300750<em>宁德时代</em>2,462,597", "AN4"),
    hit("宁德时代", "300750", "2026-10-21", "<em>宁德时代</em>三季度报告", "AN5"),
    hit("新富科技", "873855", "2026-10-19", "液冷板毛利率大幅下降，系扩大与<em>宁德时代</em>的业务合作", "AN6"),
]}}
NEWS = {"results": [{"title": "宁德时代与赛力斯签署长期战略合作协议", "url": "https://news.example/a", "domain": "news.example",
                     "date": "2026-10-10", "text": "问界全系车型继续搭载"}]}


def reply(payload):
    return {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": [], "usage": {}}


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "test.db")
        # Board lookups for the industry map are covered in test_industry.
        refresh = patch("src.chain.industry.refresh", return_value=0)
        refresh.start()
        self.addCleanup(refresh.stop)

    def test_parsers_keep_other_companies_and_latest_business(self):
        rows = events.parse_mentions(SEARCH, "宁德时代", "300750")
        self.assertEqual([r["company"] for r in rows], ["远东股份", "远东股份", "远东股份", "新富科技"])
        self.assertEqual(rows[0]["url"], "https://data.eastmoney.com/notices/detail/600869/AN1.html")
        self.assertNotIn("<em>", rows[0]["text"])
        business = events.parse_business(BUSINESS)
        self.assertEqual(business["report_date"], "2026-06-30")
        self.assertEqual(business["segments"], [{"name": "动力电池系统", "pct": "69.4", "margin": "20.6"},
                                                {"name": "储能电池系统", "pct": "19.2", "margin": "24.0"}])
        self.assertEqual(business["regions"][0]["pct"], "68.5")
        self.assertEqual(chain.short_name("宇树科技-W"), "宇树科技")
        self.assertEqual(chain.short_name("*ST 某某"), "某某")

    @patch("src.chain.market.now", return_value=datetime(2026, 11, 3, 8, 30, tzinfo=market.TZ))
    @patch("src.agent.model_client.chat")
    @patch("src.chain.news.search_news", return_value=NEWS)
    @patch("src.chain.events.search_mentions")
    @patch("src.chain.events.fetch_business", return_value=events.parse_business(BUSINESS))
    @patch("src.chain.market.fetch_quotes", return_value={"sz300750": SimpleNamespace(name="宁德时代")})
    def test_build_saves_sourced_links_and_skips_failed_searches(self, quotes, business, search, news, chat, clock):
        rows = events.parse_mentions(SEARCH, "宁德时代", "300750")
        search.side_effect = lambda name, keyword, code, page: (_ for _ in ()).throw(events.EventError("超时")) if keyword == "客户" else rows
        chat.return_value = reply({"links": [
            {"id": "C1", "seller": "远东股份", "buyer": "宁德时代", "category": "锂电铜箔", "stage": "材料", "dependent": True, "status": "确认", "note": "主要客户，采购铜丝 4.2 亿元，合计 9 亿元", "signal": ""},
            {"id": "C3", "seller": "新富科技", "buyer": "宁德时代", "category": "热管理", "stage": "零部件", "status": "确认", "note": "液冷板", "signal": "毛利率大幅下降"},
            {"id": "N1", "seller": "宁德时代", "buyer": "赛力斯", "segment": "动力电池系统", "status": "确认", "note": "长期合作", "signal": ""},
            {"id": "C2", "seller": "宁德时代", "buyer": "宁德时代新能源科技股份有限公司", "status": "确认", "note": "", "signal": ""}]})
        result = chain.build("sz300750", SETTINGS, self.store)
        # A relation with this company on both sides is dropped.
        self.assertEqual((result["suppliers"], result["customers"]), (2, 1))
        self.assertEqual(sum(not s["ok"] for s in result["searched"]), 2)
        saved = self.store.chain("sz300750")
        self.assertEqual(saved["segments"][0]["name"], "动力电池系统")
        links = {l["name"]: l for l in saved["links"]}
        self.assertEqual(links["远东股份"]["quote"], "宁德时代为公司锂电铜箔业务主要客户，2024年采购铜丝4.2亿元")
        self.assertEqual(links["远东股份"]["unverified"], ["9"])
        self.assertEqual((links["赛力斯"]["side"], links["赛力斯"]["url"]), ("下游", "https://news.example/a"))
        self.assertEqual(links["新富科技"]["signal"], "毛利率大幅下降")
        self.assertEqual((links["远东股份"]["sec_code"], links["远东股份"]["stage"], links["远东股份"]["dependent"]), ("600869", "材料", True))
        self.assertEqual((links["新富科技"]["dependent"], links["赛力斯"]["stage"]), (False, ""))
        # Each company keeps at most two different paragraphs; the repeated text is dropped.
        prompt = chat.call_args.args[0][-1]["content"]
        self.assertEqual(prompt.count("远东股份"), 2)

        chat.return_value = reply({"links": []})
        chain.build("sz300750", SETTINGS, self.store)
        self.assertEqual(self.store.chain("sz300750")["links"], [])

    @patch("src.agent.model_client.chat")
    @patch("src.chain.news.search_news")
    @patch("src.chain.events.search_mentions")
    def test_daily_update_sends_only_new_paragraphs_and_keeps_caps(self, search, news, chat):
        def link(name, day, side="上游"):
            return {"side": side, "name": name, "sec_code": "", "category": "铜箔", "segment": "", "status": "确认", "date": day,
                    "quote": "", "note": "", "signal": "", "source": "", "url": f"https://x/{name}", "unverified": [],
                    "stage": "材料", "dependent": False}
        old = [link(f"旧{i}", f"2026-0{i % 9 + 1}-01") for i in range(chain.MAX_SUPPLIERS)]
        self.store.save_chain("sz300750", "宁德时代", events.parse_business(BUSINESS), [], old)
        rows = events.parse_mentions(SEARCH, "宁德时代", "300750")
        search.return_value = rows + [{**rows[0], "company": "旧1", "url": "https://x/other", "date": "2099-01-01"}]
        news.return_value = {"results": []}
        chat.return_value = reply({"links": [{"id": "C1", "seller": "远东股份", "buyer": "宁德时代", "category": "锂电铜箔", "stage": "材料",
                                              "status": "确认", "note": "", "signal": ""}]})
        with patch("src.chain.date") as day:
            day.fromtimestamp.return_value = datetime(2026, 10, 20).date()
            result = chain.update("sz300750", SETTINGS, self.store)
        # Paragraphs older than the last update and companies already on the map never reach the model.
        prompt = chat.call_args.args[0][-1]["content"]
        self.assertIn("远东股份", prompt)
        self.assertNotIn("旧1", prompt)
        self.assertEqual(result["added"], 1)
        names = [l["name"] for l in self.store.chain("sz300750")["links"] if l["side"] == "上游"]
        self.assertEqual(len(names), chain.MAX_SUPPLIERS)
        self.assertIn("远东股份", names)
        chat.reset_mock()
        search.return_value = []
        self.assertEqual(chain.update("sz300750", SETTINGS, self.store)["added"], 0)
        chat.assert_not_called()
        self.assertIsNone(chain.update("sh600519", SETTINGS, self.store))

    def test_validator_derives_side_from_seller_and_buyer(self):
        candidates = {"C1": {"company": "远东股份", "title": "", "text": "宁德时代为主要客户，向理想汽车供货"},
                      "N1": {"title": "宁德时代与赛力斯签约", "text": ""}}
        validate = chain.validator(candidates, ["动力电池系统"], "宁德时代")
        base = {"status": "确认", "note": "", "signal": ""}
        for bad in ({"links": [{"id": "C9", "seller": "远东股份", "buyer": "宁德时代", **base}]},
                    {"links": [{"id": "N1", "seller": "宁德时代", "buyer": "赛力斯", "segment": "手机", **base}]},
                    {"links": [{"id": "C1", "seller": "远东股份", "buyer": "宁德时代", "stage": "材料", **base, "status": "可能"}]},
                    {"links": [{"id": "C1", "seller": "远东股份", "buyer": "宁德时代", "stage": "成品", **base}]}, {}):
            with self.assertRaises(agent.OutputError):
                validate(bad, {})
        # Untraceable names, relations without this company and a second company from the same paragraph are dropped.
        links = validate({"links": [
            {"id": "C1", "seller": "远东股份", "buyer": "宁德时代", "category": "铜箔", "stage": "材料", **base},
            {"id": "C1", "seller": "宁德时代", "buyer": "理想汽车", **base},
            {"id": "N1", "seller": "宁德时代", "buyer": "理想汽车", **base},
            {"id": "N1", "seller": "赛力斯", "buyer": "问界", **base},
            {"id": "N1", "seller": "宁德时代", "buyer": "赛力斯", "segment": "动力电池系统", **base}]}, {})["links"]
        self.assertEqual([(l["side"], l["name"], l["category"], l["segment"]) for l in links],
                         [("上游", "远东股份", "铜箔", ""), ("下游", "赛力斯", "", "动力电池系统")])

if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import threading
import copy
from contextlib import ExitStack
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.config import Settings
from src.research import ask, ledger_reader, number_checks, validate_research
from src.storage import Storage
from src.tools import build_tools
from src import agent, events
from src.market import MarketError, now
from src.jobs import JobManager
from src.web import create_app

SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="secret")


class ResearchTests(unittest.TestCase):
    @patch("src.agent.model_client.chat")
    def test_research_uses_holdings_tool_and_validates_refs(self, chat):
        chat.side_effect = [
            {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_holdings", "arguments": "{}"}}], "usage": {}},
            {"content": json.dumps({"title": "持仓概览", "paragraphs": [{"text": "当前有一只持仓。", "refs": ["R1"]}], "table": {}, "gaps": []}, ensure_ascii=False), "tool_calls": [], "usage": {}},
        ]
        with patch("src.tools.market.fetch_quotes", return_value={"sz002079": SimpleNamespace(name="苏州固锝")}):
            result = ask("当前持仓有什么？", SETTINGS, [{"code": "sz002079", "qty": "5", "cost": "9"}])
        self.assertEqual(result["title"], "持仓概览")
        self.assertEqual(result["steps"][0]["tool"], "get_holdings")
        self.assertEqual(result["references"]["R1"][0]["code"], "sz002079")
        self.assertEqual(result["references"]["R1"][0]["name"], "苏州固锝")
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 4500)
        self.assertNotIn(SETTINGS.api_key, json.dumps(result))

    def test_research_reads_commitment_ledger_without_workpapers(self):
        item = {"code": "sz002079", "thread_title": "增持", "kind": "增持", "content": "控股股东增持", "amount": "5,000万元",
                "deadline_text": "", "deadline": "2026-01-01", "status": "进行中", "outcome": "", "announced": "2025-06-01",
                "source_title": "增持公告", "source_url": "https://example.test/a",
                "evidence": [{"id": 1, "date": "2025-07-01", "title": "进展", "url": "https://example.test/b"}], "steps": [{"tool": "x"}]}
        store = SimpleNamespace(commitments=lambda codes: [item] if codes == ["sz002079"] else [])
        read = ledger_reader(store, [{"code": "sz002079"}])
        [row] = read([])
        self.assertEqual(row["state"], "到期未披露")
        self.assertEqual(row["url"], "https://example.test/a")
        self.assertNotIn("steps", row)
        self.assertEqual(read(["sh600519"]), [])
        tools = build_tools(SETTINGS, holdings=[{"code": "sz002079"}], ledger=read)
        self.assertEqual(tools["get_commitments"].invoke({"codes": []})[0]["code"], "sz002079")
        self.assertNotIn("get_commitments", build_tools(SETTINGS))

    def test_holdings_stay_available_when_quotes_fail(self):
        holdings = [{"code": "sz002079", "qty": "5", "cost": "9"}]
        with patch("src.tools.market.fetch_quotes", side_effect=MarketError("行情源暂时不可用")):
            self.assertEqual(build_tools(SETTINGS, holdings=holdings)["get_holdings"].function(), holdings)

    def test_number_checks_accept_rounding_and_unwritten_sign(self):
        refs = {"R1": [{"return_pct": "-11.3401", "count": "11.6", "total": "5410"}]}
        checks = number_checks("跌11.34%，-11.34%，+11.34%，-11.40%，11.6，12，5410", ["R1"], refs)
        self.assertEqual({n["value"]: n["verification"] for n in checks},
                         {"11.34": "已核对", "-11.34": "已核对", "+11.34": "未核对", "-11.40": "未核对",
                          "11.6": "已核对", "12": "未核对", "5410": "已核对"})

    def test_invalid_research_refs_rejected(self):
        with self.assertRaises(agent.OutputError):
            validate_research({"title": "x", "paragraphs": [{"text": "x", "refs": ["R9"]}], "table": {}, "gaps": []}, {"R1": {}})

    def test_table_checks_keep_unknown_numbers_and_reject_malformed_refs(self):
        payload = {"title": "分析999", "paragraphs": [{"text": "占比11.91%，未知值999。", "refs": ["R1"]}],
                   "table": {"caption": "占比（百分数）", "columns": ["指标", "数值"],
                             "rows": [{"cells": ["占比", "11.91%"], "refs": ["R1"]}]}, "gaps": ["尚未核实999的依据。"]}
        refs = {"R1": [{"float_ratio_pct": "11.91"}]}
        checked = validate_research(payload, refs)
        self.assertEqual([n["verification"] for n in checked["paragraphs"][0]["numbers"]], ["已核对", "未核对"])
        self.assertIn("999", checked["paragraphs"][0]["text"])
        self.assertEqual(checked["table"]["rows"][0]["numbers"][0]["verification"], "已核对")
        for invalid in ([{}], ["R99"], "R1"):
            bad = copy.deepcopy(payload)
            bad["paragraphs"][0]["refs"] = invalid
            with self.assertRaises(agent.OutputError):
                validate_research(bad, refs)
        bad = copy.deepcopy(payload)
        bad["table"]["rows"][0]["cells"].pop()
        with self.assertRaises(agent.OutputError):
            validate_research(bad, refs)

    @patch("src.agent.model_client.chat")
    def test_eight_call_limit_and_cumulative_progress(self, chat):
        calls = [{"id": f"c{i}", "type": "function", "function": {"name": "get_holdings", "arguments": "{}"}} for i in range(9)]
        chat.side_effect = [{"content": None, "tool_calls": calls, "usage": {}},
                            {"content": '{"title":"结果","paragraphs":[],"table":{},"gaps":[]}', "tool_calls": [], "usage": {}}]
        progress = []
        result = ask("查看持仓", SETTINGS, [{"code": "sz002079"}], on_step=lambda steps: progress.append(steps))
        self.assertEqual(len(result["steps"]), 8)
        self.assertEqual([len(steps) for steps in progress], list(range(1, 9)))
        self.assertIsNone(chat.call_args.kwargs["tools"])

    @patch("src.agent.model_client.chat")
    def test_bad_structure_repairs_only_once(self, chat):
        bad = {"content": '{"title":"结果","paragraphs":[],"table":[],"gaps":[]}', "tool_calls": [], "usage": {}}
        good = {"content": '{"title":"结果","paragraphs":[],"table":{},"gaps":[]}', "tool_calls": [], "usage": {}}
        chat.side_effect = [bad, good]
        self.assertEqual(ask("问题", SETTINGS, [])["table"], {})
        self.assertEqual(chat.call_count, 2)
        chat.reset_mock()
        chat.side_effect = [bad, bad]
        with self.assertRaises(agent.OutputError):
            ask("问题", SETTINGS, [])
        self.assertEqual(chat.call_count, 2)


class Stage2ApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("测试不得发起网络请求")))
        self.stack.enter_context(patch("urllib.request.build_opener", side_effect=AssertionError("测试不得发起模型请求")))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "test.db")
        with patch("src.accounts.SCRYPT", {"n": 16, "r": 8, "p": 1}):
            self.password = self.store.add("alice", "password")
            self.store.add("bob", "password")
            self.store.add("team", "password")
        self.store.set_holdings("alice", [{"code": "sz002079", "qty": "5", "cost": "9"}])
        with patch("src.web.load_env"):
            self.app = create_app(self.store, lambda codes: {}, settings=SETTINGS,
                                  researcher=lambda question, settings, holdings, on_step: {"title": question, "paragraphs": [], "table": {}, "gaps": [], "steps": [], "references": {}, "usage": []})
        self.client = TestClient(self.app, base_url="https://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/login", json={"username": "alice", "password": "password"})

    def test_holdings_replace_reset_and_search(self):
        result = self.client.put("/api/holdings", json=[{"code": "sh600519", "qty": "1", "cost": "1500"}])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["holdings"][0]["code"], "sh600519")
        self.assertEqual(self.client.put("/api/holdings", json=[{"code": "sh000001"}]).status_code, 400)
        with patch("src.web.search_symbols", return_value=[("sz002079", "苏州固锝")]) as search:
            result = self.client.get("/api/search?q=固锝")
        self.assertEqual(result.json(), {"results": [{"code": "sz002079", "name": "苏州固锝"}]})
        search.assert_called_once_with("固锝")
        self.assertEqual(self.client.post("/api/holdings/reset").status_code, 200)
        self.assertEqual(len(self.client.get("/api/holdings").json()["holdings"]), 8)

    @patch("src.web.events.fetch_financials", return_value=[])
    @patch("src.web.events.fetch_forecasts", return_value=[])
    @patch("src.web.events.fetch_holder_changes", return_value=[])
    @patch("src.web.events.fetch_unlocks", return_value=[])
    @patch("src.web.events.fetch_announcements", return_value=[{"title": "公告", "date": "2026-10-26", "url": "https://example.test/a"}])
    @patch("src.web.fetch_history", return_value={"symbol": "sz002079", "bars": [], "source": "腾讯", "url": "https://example.test"})
    def test_stock_system_and_research_job(self, history, announcements, *other):
        result = self.client.get("/api/stock/sz002079")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["symbol"], "sz002079")
        self.assertEqual(result.json()["events"][0]["title"], "公告")
        system = self.client.get("/api/system").json()
        self.assertIn("usage", system)
        job = self.client.post("/api/research", json={"question": "持仓有什么变化？"})
        self.assertEqual(job.status_code, 200)
        job_id = job.json()["job_id"]
        self.app.state.jobs.executor.shutdown(wait=True)
        self.assertEqual(self.client.get("/api/jobs/" + job_id).json()["status"], "done")
        self.assertEqual(len(self.client.get("/api/research").json()["records"]), 1)

    def test_authentication_quota_and_account_isolation(self):
        from datetime import timedelta
        day = now().date().isoformat()
        old_day = (now().date() - timedelta(days=1)).isoformat()
        with self.store.db() as db:
            db.executemany("INSERT INTO usage VALUES (?, ?, 'research', ?)", [("alice", day, 30), ("team", day, 30), ("bob", old_day, 30)])
        response = self.client.post("/api/research", json={"question": "配额测试"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("研究", response.json()["detail"])
        self.client.post("/api/login", json={"username": "team", "password": "password"})
        unlimited = self.client.post("/api/research", json={"question": "team配额"})
        self.assertEqual(unlimited.status_code, 200)
        self.assertIsNone(self.client.get("/api/system").json()["limits"]["research"])
        self.client.post("/api/login", json={"username": "bob", "password": "password"})
        self.assertEqual(self.client.get("/api/research").json()["records"], [])
        self.assertEqual(self.client.get("/api/jobs/" + unlimited.json()["job_id"]).status_code, 404)
        self.assertEqual(self.client.post("/api/research", json={"question": "新一天"}).status_code, 200)
        self.assertEqual(self.client.post("/api/research", json={"question": "   "}).status_code, 400)
        self.client.post("/api/logout")
        for path in ("/api/research", "/api/search?q=测试", "/api/stock/sz002079", "/api/system"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.put("/api/holdings", json=[]).status_code, 401)
        self.assertEqual(self.client.post("/api/research", json={"question": "问题"}).status_code, 401)

    def test_stock_partial_failure_is_visible_and_invalid_code_never_fetches(self):
        with ExitStack() as stack:
            history = stack.enter_context(patch("src.web.fetch_history", side_effect=MarketError("日线不可用。")))
            for name in ("announcements", "holder_changes", "forecasts", "financials"):
                stack.enter_context(patch("src.web.events.fetch_" + name, return_value=[]))
            unlocks = stack.enter_context(patch("src.web.events.fetch_unlocks", return_value=[{"type":"解禁", "date":"2026-10-28", "shares_wan":"9660.1307"}]))
            self.assertEqual(self.client.get("/api/stock/002079").status_code, 400)
            self.assertEqual(self.client.get("/api/stock/sh000001").status_code, 400)
            history.assert_not_called()
            payload = self.client.get("/api/stock/sz002079").json()
            self.assertEqual(payload["errors"], [{"source": "日线", "detail": "日线不可用。"}])
            self.assertEqual(payload["events"][0]["shares_wan"], "9660.1307")
            self.client.get("/api/stock/sz002079")
            history.assert_called_once()
            unlocks.assert_called_once()
        sources = {item["name"]: item for item in self.client.get("/api/system").json()["sources"]}
        self.assertEqual(sources["腾讯行情"]["state"], "error")
        self.assertEqual(sources["东方财富"]["state"], "ok")
        self.assertEqual(sources["DeepSeek"]["state"], "unknown")
        self.assertNotIn("secret", json.dumps(sources))

    def test_holdings_keep_decimal_and_account_scoping_after_clear(self):
        self.client.put("/api/holdings", json=[{"code":"sz002079", "qty":"0.1234567890123456789", "cost":None}])
        self.assertEqual(self.client.get("/api/holdings").json()["holdings"][0]["qty"], "0.1234567890123456789")
        for invalid in ([{"code":"sz002079"}] * 31, [{"code":"sz002079", "qty":"NaN"}], [{"code":"sz002079", "cost":"1e999999"}]):
            self.assertEqual(self.client.put("/api/holdings", json=invalid).status_code, 400)
        self.assertEqual(len(self.client.get("/api/holdings").json()["holdings"]), 1)
        self.client.put("/api/holdings", json=[])
        self.assertEqual(self.client.get("/api/holdings").json()["holdings"], [])
        self.assertEqual(self.client.post("/api/research", json={"question":"问题"}).status_code, 400)
        self.assertEqual(len(self.store.holdings("bob")), 8)

    def test_research_progress_is_a_list_and_duplicate_does_not_charge_twice(self):
        entered, release = threading.Event(), threading.Event()
        def researcher(question, settings, holdings, *, on_step):
            on_step([{"tool":"get_holdings", "arguments":{}, "ref":"R1", "summary":"持仓", "elapsed_seconds":0, "error":False}])
            entered.set()
            release.wait(3)
            raise ValueError("测试错误")
        self.app.state.jobs.researcher = researcher
        try:
            job = self.client.post("/api/research", json={"question":"当前持仓"}).json()["job_id"]
            self.assertTrue(entered.wait(3))
            active = self.client.get("/api/jobs/" + job).json()
            self.assertEqual(active["steps"][0]["ref"], "R1")
            self.assertEqual(active["status"], "running")
            self.assertEqual(self.client.post("/api/research", json={"question":"当前持仓"}).json()["job_id"], job)
            self.assertEqual(self.client.get("/api/system").json()["usage"]["research"], 1)
        finally:
            release.set()
            self.app.state.jobs.close()
        failed = self.client.get("/api/jobs/" + job).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "测试错误")


if __name__ == "__main__":
    unittest.main()

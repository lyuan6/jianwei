import json
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.config import Settings
from src.jobs import JobManager
from src.model_client import ModelError
from src.storage import DEFAULT_HOLDINGS, QuotaExceeded, Storage, portfolio_key
from src import brief, market
from scripts.daily_brief import generate_daily, main as daily_main

DAY = "2026-10-26"
SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="test-secret")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "test.db")
        with patch("src.accounts.SCRYPT", {"n": 16, "r": 8, "p": 1}):
            self.store.add("alice", "test-password")
            self.store.add("bob", "test-password")

    def test_shared_wal_db_initializes_once_and_preserves_empty_holdings(self):
        with self.store.db() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        holdings = self.store.holdings("alice")
        self.assertEqual([(h["code"], h["qty"], h["cost"]) for h in holdings], DEFAULT_HOLDINGS)
        self.assertEqual(holdings, self.store.holdings("alice"))
        self.store.set_holdings("alice", [])
        self.assertEqual(Storage(self.store.path).holdings("alice"), [])
        self.assertEqual(len(self.store.holdings("bob")), 8)

    def test_holdings_decimal_validation_and_atomic_replacement(self):
        self.store.set_holdings("alice", [{"code": "sz002079", "qty": "0.1234567890123456789", "cost": "9.8600"}, {"code": "sh600519"}])
        self.assertEqual(self.store.holdings("alice")[0]["qty"], "0.1234567890123456789")
        self.assertIsNone(self.store.holdings("alice")[1]["cost"])
        for invalid in ([{"code": "sh000001"}], [{"code": "sz002079", "cost": "NaN"}], [{"code": "sz002079"}] * 31):
            with self.assertRaises(ValueError):
                self.store.set_holdings("alice", invalid)
        self.assertEqual(len(self.store.holdings("alice")), 2)

    def test_briefs_shared_by_sorted_codes_not_qty_or_cost(self):
        codes = ["sz002079", "sh600519"]
        self.store.save_brief(codes, DAY, {"events": []})
        self.assertEqual(portfolio_key(codes), portfolio_key(list(reversed(codes))))
        self.assertEqual(self.store.brief(list(reversed(codes)), DAY), {"events": []})
        self.assertIsNone(self.store.brief(["sz002079"], DAY))
        self.assertIsNone(self.store.brief(codes, "2026-10-27"))

    def test_quota_is_atomic_and_duplicate_running_jobs_are_reused(self):
        inputs = {"codes": ["sz002079"], "date": DAY}
        def create(_):
            return self.store.create_job("alice", "brief", inputs, DAY, 5)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(8)))
        self.assertEqual(len({row[0] for row in results}), 1)
        self.assertEqual(sum(row[1] for row in results), 1)
        first = results[0][0]
        self.assertIsNone(self.store.job(first, "bob"))
        self.store.update_job(first, status="done")
        for _ in range(4):
            job_id, _ = create(None)
            self.store.update_job(job_id, status="failed", error="测试失败")
        with self.assertRaises(QuotaExceeded):
            create(None)
        self.assertTrue(self.store.create_job("alice", "brief", {"date": "2026-10-27"}, "2026-10-27", 5)[1])
        self.assertTrue(self.store.create_job("alice", "brief", {"test": True}, DAY, None)[1])

    def test_recovery_marks_running_and_queued_failed_but_keeps_done(self):
        ids = [self.store.create_job("alice", "brief", {"n": i}, DAY, 5)[0] for i in range(3)]
        self.store.update_job(ids[0], status="running", steps=[{"ref": "R1"}])
        self.store.update_job(ids[2], status="done", result={"ok": True})
        self.store.recover_jobs()
        self.assertEqual([self.store.job(i, "alice")["status"] for i in ids], ["failed", "failed", "done"])
        self.assertEqual(self.store.job(ids[0], "alice")["steps"], [{"ref": "R1"}])

    def test_background_progress_and_failure_are_persisted(self):
        entered, release = threading.Event(), threading.Event()
        def generator(codes, settings, store, day, *, on_step):
            on_step([{"code": codes[0], "ref": "R1"}])
            entered.set()
            release.wait(3)
            raise ValueError("测试错误")
        manager = JobManager(self.store, SETTINGS, generator)
        try:
            job_id = manager.submit("alice", ["sz002079"], DAY, 5)
            self.assertTrue(entered.wait(3))
            active = self.store.job(job_id, "alice")
            self.assertEqual(active["status"], "running")
            self.assertEqual(active["steps"][0]["ref"], "R1")
        finally:
            release.set()
            manager.close()
        failed = self.store.job(job_id, "alice")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "测试错误")

    @patch("src.brief.generate_brief")
    def test_shared_generation_lock_computes_once(self, generate):
        generate.return_value = {"stocks": [], "events": [], "steps": []}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: brief.get_or_generate(["sz002079"], SETTINGS, self.store, DAY), range(2)))
        self.assertEqual(results[0], results[1])
        generate.assert_called_once()

    @patch("scripts.daily_brief.get_or_generate")
    def test_daily_deduplicates_skips_cached_and_disabled_accounts(self, generate):
        for name in ("alice", "bob"):
            self.store.set_holdings(name, [{"code": "sz002079"}])
        result = generate_daily(self.store, SETTINGS)
        self.assertEqual(result["generated"], 1)
        generate.assert_called_once()
        day = generate.call_args.args[3]
        self.store.save_brief(["sz002079"], day, {"events": []})
        generate.reset_mock()
        self.assertEqual(generate_daily(self.store, SETTINGS)["skipped"], 1)
        generate.assert_not_called()
        self.store.set_disabled("alice", True)
        self.store.set_disabled("bob", True)
        self.assertEqual(generate_daily(self.store, SETTINGS)["generated"], 0)

    @patch("scripts.daily_brief.get_or_generate")
    def test_daily_reports_failure_location_and_continues_other_portfolios(self, generate):
        self.store.set_holdings("alice", [{"code": "sz002079"}])
        self.store.set_holdings("bob", [{"code": "sh600519"}])
        def run(codes, settings, store, day, *, on_step):
            on_step([{"code": codes[0], "ref": "R1"}])
            if codes == ["sz002079"]:
                raise ModelError("模型响应未完整结束。")
            return {"events": []}
        generate.side_effect = run
        output = io.StringIO()
        with redirect_stdout(output):
            result = generate_daily(self.store, SETTINGS)
        self.assertEqual(result, {"generated": 1, "skipped": 0, "failed": 1})
        self.assertEqual(generate.call_count, 2)
        self.assertIn("最近底稿 sz002079，已记录 1 步", output.getvalue())
        self.assertIn("模型响应未完整结束", output.getvalue())

    @patch("scripts.daily_brief.load_env")
    @patch("scripts.daily_brief.get_or_generate")
    def test_daily_replays_given_dates_with_that_days_clock(self, generate, _):
        self.store.set_holdings("alice", [{"code": "sz002079"}])
        self.store.set_disabled("bob", True)
        seen = []
        generate.side_effect = lambda codes, settings, store, day, *, on_step: seen.append((day, market.now()))
        with patch("scripts.daily_brief.default_store", return_value=self.store), \
                patch("scripts.daily_brief.Settings.from_env", return_value=SETTINGS), redirect_stdout(io.StringIO()):
            self.assertEqual(daily_main(["2026-10-27", "2026-10-28"]), 0)
        self.assertEqual([day for day, _ in seen], ["2026-10-27", "2026-10-28"])
        self.assertEqual([clock.date().isoformat() for _, clock in seen], ["2026-10-27", "2026-10-28"])
        self.assertEqual(market.now().date(), datetime.now(market.TZ).date())

    @patch("scripts.daily_brief.get_or_generate", side_effect=RuntimeError("raw provider credentials"))
    def test_daily_hides_unexpected_errors_before_any_workpaper(self, generate):
        for name in ("alice", "bob"):
            self.store.set_holdings(name, [{"code": "sz002079"}])
        output = io.StringIO()
        with redirect_stdout(output):
            result = generate_daily(self.store, SETTINGS)
        self.assertEqual(result, {"generated": 0, "skipped": 0, "failed": 1})
        generate.assert_called_once()
        self.assertIn("尚无底稿", output.getvalue())
        self.assertIn("内部处理失败", output.getvalue())
        self.assertNotIn("raw provider credentials", output.getvalue())

    @patch("src.brief.market.fetch_quotes", return_value={"sz002079": SimpleNamespace(name="苏州固锝"), "sh600519": SimpleNamespace(name="贵州茅台")})
    @patch("src.brief.model_client.complete_json", return_value={"lede": "已核对事件。"})
    @patch("src.brief.analyze_stock")
    def test_report_counts_and_intro_only_receive_checked_figures(self, analyze, intro, quotes):
        checked = {"level": "high", "tag": "解禁", "date": DAY, "figures": [{"value": "11.91", "verification": "已核对"}], "title": "标题", "points": ["不传入导语的分析"]}
        unchecked = {**checked, "level": "mid", "figures": [{"value": "999", "verification": "未核对"}]}
        def stock(code, settings, *, on_step):
            on_step({"ref": "R1", "tool": "get_unlocks"})
            return {"code": code, "status": "完成", "events": [checked, unchecked], "steps": [], "references": {}}
        analyze.side_effect = stock
        updates = []
        report = brief.generate_brief(["sz002079", "sh600519"], SETTINGS, DAY, on_step=lambda steps: updates.append(list(steps)))
        self.assertEqual(report["title"], "今日 2 项重点、2 项留意")
        self.assertEqual(len(updates), 2)
        self.assertEqual([s["code"] for s in updates[-1]], ["sh600519", "sz002079"])
        payload = intro.call_args.args[0][-1]["content"]
        self.assertNotIn("999", payload)
        self.assertNotIn("不传入导语", payload)
        self.assertIn("11.91", payload)
        self.assertIn("苏州固锝", payload)
        self.assertNotIn("sz002079", payload)
        intro.assert_called_once()

    @patch("src.brief.market.fetch_quotes", side_effect=market.MarketError("行情源暂时不可用"))
    @patch("src.brief.model_client.complete_json", side_effect=ModelError("模型服务连接失败或超时。"))
    @patch("src.brief.analyze_stock")
    def test_one_failed_stock_or_intro_keeps_the_rest(self, analyze, intro, quotes):
        event = {"level": "high", "tag": "解禁", "date": DAY, "figures": [{"value": "11.91", "verification": "已核对"}]}
        def stock(code, settings, *, on_step):
            on_step({"ref": "R1", "tool": "get_unlocks"})
            if code == "sh600519":
                raise ModelError("模型响应未完整结束。")
            return {"code": code, "status": "完成", "events": [event], "steps": [], "references": {}}
        analyze.side_effect = stock
        report = brief.generate_brief(["sz002079", "sh600519"], SETTINGS, DAY)
        self.assertEqual(report["status"], "数据不全")
        failed = next(s for s in report["stocks"] if s["code"] == "sh600519")
        self.assertEqual((failed["status"], failed["error"], len(failed["steps"])), ("失败", "模型响应未完整结束。", 1))
        self.assertEqual(report["lede"], "重点：sz002079 解禁。")
        with self.assertRaises(ModelError):
            brief.generate_brief(["sh600519"], SETTINGS, DAY)

    @patch("src.brief.model_client.complete_json")
    @patch("src.brief.analyze_stock")
    def test_no_event_report_never_requests_intro_model(self, analyze, intro):
        analyze.return_value = {"code": "sz002079", "status": "无新事件", "events": [], "steps": [], "references": {}}
        report = brief.generate_brief(["sz002079"], SETTINGS, DAY)
        self.assertEqual(report["counts"], {"high": 0, "mid": 0, "low": 0})
        intro.assert_not_called()


if __name__ == "__main__":
    unittest.main()

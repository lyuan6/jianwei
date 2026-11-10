import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import evaluate
from src import market, model_client
from src.config import Settings

SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="test")
CASE = {"id": "unlock-1", "code": "sz001391", "type": "解禁", "as_of": "2026-09-28", "level": "high",
        "rule": "解禁占流通股超过 5%", "key_figures": ["577215.2279", "79.16"], "url": "https://data.eastmoney.com/dxf/q/001391.html"}
REFERENCES = {"R2": [{"date": "2026-09-28", "shares_wan": "577215.2279", "float_ratio_pct": "79.16",
                      "url": "https://data.eastmoney.com/dxf/q/001391.html"}]}


def event(level, figures, refs=("R2",)):
    return {"level": level, "tag": "解禁", "date": "2026-09-28", "title": "限售股解禁",
            "figures": [{"label": "指标", "value": value, "ref": "R2"} for value in figures],
            "points": ["解禁占解禁前流通股 79.16%"], "risk": "", "refs": list(refs)}


class EvaluateTests(unittest.TestCase):
    def test_score_checks_level_key_figures_and_unsupported_numbers(self):
        result = evaluate.score(CASE, [event("high", ["577215.2279万股", "12.5%"])], REFERENCES)
        self.assertTrue(result["level_ok"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["unsupported"], ["12.5%"])
        self.assertEqual(result["sources"], ["https://data.eastmoney.com/dxf/q/001391.html"])
        dated = {**REFERENCES, "R3": {"text": "公司已预约 2026 年 8 月 24 日披露半年报"}}
        figures = ["2026-09-28", "2026-08-24", "2026-08-25", "2026-08-24至2026-09-28", "2026-08-24至2026-08-25"]
        self.assertEqual(evaluate.score(CASE, [event("high", figures)], dated)["unsupported"],
                         ["2026-08-25", "2026-08-24至2026-08-25"])
        empty = evaluate.score(CASE, [{"level": "low", "tag": "人事", "title": "董事辞职", "figures": []}], REFERENCES)
        self.assertEqual((empty["level"], empty["level_ok"], empty["missing"]), (None, False, CASE["key_figures"]))

    def test_other_events_cannot_fill_missing_key_figures(self):
        target = event("high", ["79.16%"])
        unrelated = {**event("low", ["577215.2279", "12.5%"]), "tag": "财报", "title": "半年报"}
        result = evaluate.score(CASE, [target, unrelated], REFERENCES)
        self.assertEqual(result["missing"], ["577215.2279"])
        self.assertEqual(result["unsupported"], ["12.5%"])
        target["risk"] = "本次解禁 577215.2279 万股，关注流通变化。"
        self.assertEqual(evaluate.score(CASE, [target], REFERENCES)["missing"], [])

    def test_expected_share_units_match_an_answer_in_shares(self):
        case = next(case for case in json.loads(evaluate.CASES.read_text()) if case["id"] == "reduce-2")
        answer = {"level": "mid", "tag": "减持", "title": "股东减持结果", "points": [],
                  "figures": [{"value": "13,869,000股"}], "refs": []}
        self.assertEqual(evaluate.score(case, [answer], {})["missing"], ["3%"])
        answer["figures"].append({"value": "3.00%"})
        self.assertEqual(evaluate.score(case, [answer], {})["missing"], [])

    @patch("scripts.evaluate.load_env")
    @patch("scripts.evaluate.Settings.from_env", return_value=SETTINGS)
    @patch("scripts.evaluate.run")
    def test_selected_run_preserves_full_results_and_rejects_unknown_ids(self, run, settings, load_env):
        sample = evaluate.score(CASE, [event("high", ["577215.2279", "79.16%"])], REFERENCES)
        run.return_value = {"case": CASE, "system": {**sample, "error": None},
                            "baseline": {**sample, "error": None}}
        with tempfile.TemporaryDirectory() as directory:
            full = Path(directory) / "evaluation.json"
            full.write_text("saved full results")
            with patch.object(evaluate, "OUTPUT", full), redirect_stdout(io.StringIO()):
                evaluate.main(["unlock-1"])
                self.assertEqual(full.read_text(), "saved full results")
                selected = full.with_name("evaluation-selected.json")
                self.assertEqual(len(json.loads(selected.read_text())), 1)
                run.reset_mock()
                errors = io.StringIO()
                with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                    evaluate.main(["unlock-1", "unknown-case"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unknown-case", errors.getvalue())
                run.assert_not_called()
                self.assertEqual(full.read_text(), "saved full results")
                self.assertEqual(len(json.loads(selected.read_text())), 1)
                explicit = Path(directory) / "comparison.json"
                evaluate.main(["unlock-1", "--output", str(explicit)])
                self.assertEqual(json.loads(explicit.read_text())[0]["case"]["id"], "unlock-1")
                run.reset_mock()
                evaluate.main([])
                self.assertEqual(run.call_count, 20)
                self.assertEqual(len(json.loads(full.read_text())), 20)

    @patch("scripts.evaluate.model_client.complete_json")
    @patch("scripts.evaluate.brief.analyze_stock")
    def test_system_runs_on_case_date_and_baseline_has_no_tools(self, analyze, complete):
        seen = []
        def system(code, settings):
            seen.append(market.now().date().isoformat())
            return {"events": [event("high", ["79.16%"])], "steps": [{"error": False}], "references": REFERENCES}
        analyze.side_effect = system
        complete.return_value = {"events": [event("mid", ["60%"], refs=())]}
        result = evaluate.run(CASE, SETTINGS)
        self.assertEqual(seen, ["2026-09-28"])
        self.assertNotIn("tools", complete.call_args.kwargs)
        self.assertIn("2026-09-28", complete.call_args.args[0][-1]["content"])
        self.assertTrue(result["system"]["level_ok"])
        self.assertEqual(result["baseline"]["level"], "mid")
        self.assertEqual(result["baseline"]["unsupported"], ["60%"])
        self.assertEqual(result["baseline"]["sources"], [])

    @patch("scripts.evaluate.model_client.complete_json", side_effect=model_client.ModelError("模型服务连接失败或超时。"))
    @patch("scripts.evaluate.brief.analyze_stock", side_effect=ValueError("事件数据源暂时不可用。"))
    def test_failures_are_recorded_and_counted(self, analyze, complete):
        result = evaluate.run(CASE, SETTINGS)
        self.assertEqual(result["system"]["error"], "事件数据源暂时不可用。")
        self.assertEqual(result["baseline"]["error"], "模型服务连接失败或超时。")
        self.assertIn("失败 1 次", evaluate.summary([result], "system"))

    def test_fixture_cases_are_complete(self):
        cases = json.loads(evaluate.CASES.read_text())
        self.assertEqual(len(cases), 20)
        self.assertEqual({case["type"] for case in cases}, set(evaluate.KEYWORDS))
        for case in cases:
            self.assertIn(case["level"], ("high", "mid", "low"))
            self.assertTrue(case["url"].startswith("https://data.eastmoney.com/"))


if __name__ == "__main__":
    unittest.main()

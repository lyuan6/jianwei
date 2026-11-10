import io
import json
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from unittest.mock import patch

from src import agent, brief
from src.config import Settings
from src.market import TZ


SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="test-key")


def event():
    return {"level": "high", "tag": "解禁", "date": "2026-10-27", "title": "限售股解禁",
            "figures": [{"label": "占解禁前流通股比例（百分数）", "value": "11.91%", "ref": "R2"}],
            "points": ["解禁比例超过参考阈值，列为重点。"], "risk": "关注实际流通情况。", "refs": ["R2"]}


def reply(payload):
    return {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": [], "usage": {}}


class ValidationTests(unittest.TestCase):
    def test_figures_match_exact_numbers_with_units_and_grouping(self):
        for value, source, expected in [
            ("11.910 %", {"float_ratio_pct": "11.91"}, True),
            ("9,660.1307 万股", {"shares_wan": "9660.1307"}, True),
            ("1.2亿元", {"revenue_yi": "1.20"}, True),
            ("-51%", {"text": "净利润同比-51%。"}, True),
            ("1.91%", {"float_ratio_pct": "11.91"}, False),
            ("51%", {"yoy": "-51"}, False),
            ("300万股", {"shares_wan": "1300"}, False),
            ("2079", {"code": "002079", "url": "https://example.test/2079"}, False),
            ("12000万元", {"revenue_yi": "1.20"}, False),
            ("472.7万股", {"text": "通过大宗交易减持 4,727,000 股"}, True),
            ("-0.73至-0.66", {"low": "-0.73", "high": "-0.66"}, True),
            ("1.507018-1.665651亿元", {"text": "净利润为 15,070.18 万元到 16,656.51万元"}, True),
            ("8.55～9.93元/股", {"text": "减持价格区间 8.55～9.93元/股"}, True),
            ("44.23%～59.41%", {"yoy_low_pct": "44.23"}, False),
            ("55起", {"text": "上述案件共计 55 起"}, True),
            ("55件", {"text": "上述案件共计 54 件"}, False),
            ("2人", {"text": "本次辞职人数为2人"}, True),
            ("0股", {"text": "未直接或间接持有公司股份"}, False),
        ]:
            with self.subTest(value=value, source=source):
                self.assertEqual(brief.verify_figure(value, source), expected)

    def test_unmatched_figures_are_kept_and_marked_per_reference(self):
        item = event()
        item["figures"].append({"label": "股数（万股）", "value": "300万股", "ref": "R2"})
        checked = brief.validate_events({"events": [item]}, {"R1": [{"shares_wan": "300"}], "R2": [{"float_ratio_pct": "11.91"}]})
        figures = checked["events"][0]["figures"]
        self.assertEqual([figure["verification"] for figure in figures], ["已核对", "未核对"])
        self.assertEqual(figures[1]["value"], "300万股")

    def test_figure_ref_outside_event_refs_is_added(self):
        item = event()
        item["figures"][0]["ref"] = "R1"
        checked = brief.validate_events({"events": [item]}, {"R1": [{"float_ratio_pct": "11.91"}], "R2": []})
        self.assertEqual(checked["events"][0]["refs"], ["R2", "R1"])
        self.assertEqual(checked["events"][0]["figures"][0]["verification"], "已核对")

    def test_structure_and_references_rejected(self):
        for field, value in (("level", "urgent"), ("date", "昨天"), ("points", "文字"), ("refs", ["R99"]),
                             ("figures", [{"label": "比例", "value": "1", "ref": "R9"}])):
            item = event()
            item[field] = value
            with self.subTest(field=field), self.assertRaises(agent.OutputError):
                brief.validate_events({"events": [item]}, {"R1": [], "R2": []})
        with self.assertRaises(agent.OutputError):
            brief.validate_events({"events": [{"title": "缺字段"}]}, {})


class SingleStockTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.sources = {}
        for name in ("announcements", "unlocks", "holder_changes", "forecasts", "financials"):
            self.sources[name] = self.stack.enter_context(patch("src.tools.events.fetch_" + name, return_value=[]))
        self.chat = self.stack.enter_context(patch("src.agent.model_client.chat"))
        self.stack.enter_context(patch("src.market.now", return_value=datetime(2026, 10, 26, tzinfo=TZ)))

    def test_empty_window_skips_model_and_old_financials(self):
        self.sources["financials"].return_value = [{"date": "2026-10-18", "period": "2026-09-30"}]
        result = brief.analyze_stock("sz002079", SETTINGS)
        self.assertEqual((result["status"], result["events"]), ("无新事件", []))
        self.assertEqual(len(result["steps"]), 5)
        self.assertEqual(result["references"]["R5"], [])
        self.chat.assert_not_called()
        for name, kwargs in (("announcements", {"days": 3}), ("unlocks", {"past_days": 30, "future_days": 30}),
                             ("holder_changes", {"days": 7}), ("forecasts", {"days": 7}), ("financials", {"periods": 4})):
            self.sources[name].assert_called_once_with("sz002079", **kwargs)

    def test_recent_disclosure_triggers_analysis_even_for_old_quarter(self):
        self.sources["financials"].return_value = [{"date": "2026-10-19", "period": "2026-06-30", "revenue_yi": "1.20"}]
        self.chat.return_value = reply({"events": []})
        result = brief.analyze_stock("sz002079", SETTINGS)
        self.chat.assert_called_once()
        self.assertEqual(result["references"]["R5"][0]["revenue_yi"], "1.20")

    def test_failed_prefetch_is_not_no_event(self):
        self.sources["announcements"].side_effect = ValueError("公告接口不可用。")
        self.chat.return_value = reply({"events": []})
        result = brief.analyze_stock("sz002079", SETTINGS)
        self.assertEqual(result["status"], "数据不全")
        self.assertTrue(result["steps"][0]["error"])
        self.chat.assert_called_once()
        self.sources["announcements"].assert_called_once()

    def test_single_stock_follows_tool_request_and_preserves_source_and_figure(self):
        self.sources["unlocks"].return_value = [{"date": "2026-10-27", "float_ratio_pct": "11.91", "url": "https://data.eastmoney.com/dxf/q/002079.html"}]
        item = event()
        item["refs"].append("R6")
        item["figures"].append({"label": "未知金额（亿元）", "value": "999", "ref": "R6"})
        self.chat.side_effect = [
            {"content": None, "tool_calls": [{"id": "read1", "type": "function", "function": {
                "name": "read_announcement", "arguments": '{"art_code":"AN1"}'}}], "usage": {}},
            reply({"events": [item]}),
        ]
        with patch("src.tools.events.fetch_announcement_content", return_value={"text": "解禁安排", "pdf_url": "https://pdf.dfcfw.com/AN1.pdf"}) as read:
            result = brief.analyze_stock("sz002079", SETTINGS)
        read.assert_called_once_with(art_code="AN1")
        self.assertEqual(result["status"], "完成")
        self.assertEqual(len(result["steps"]), 6)
        self.assertEqual(result["steps"][-1]["ref"], "R6")
        self.assertEqual(result["references"]["R6"]["pdf_url"], "https://pdf.dfcfw.com/AN1.pdf")
        self.assertEqual([f["verification"] for f in result["events"][0]["figures"]], ["已核对", "未核对"])

    def test_invalid_reference_is_repaired_once_then_fails_if_still_invalid(self):
        self.sources["unlocks"].return_value = [{"float_ratio_pct": "11.91"}]
        invalid = event()
        invalid["refs"] = ["R99"]
        self.chat.side_effect = [reply({"events": [invalid]}), reply({"events": [event()]})]
        self.assertEqual(brief.analyze_stock("sz002079", SETTINGS)["events"][0]["level"], "high")
        self.assertEqual(self.chat.call_count, 2)
        self.chat.reset_mock()
        self.chat.side_effect = [reply({"events": [invalid]})] * 2
        with self.assertRaises(agent.OutputError):
            brief.analyze_stock("sz002079", SETTINGS)
        self.assertEqual(self.chat.call_count, 2)

    def test_cli_prints_json_with_workpaper(self):
        self.sources["unlocks"].return_value = [{"float_ratio_pct": "11.91"}]
        self.chat.return_value = reply({"events": [event()]})
        output = io.StringIO()
        with patch("src.brief.load_env"), patch("src.brief.Settings.from_env", return_value=SETTINGS), redirect_stdout(output):
            brief.main(["sz002079"])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["code"], "sz002079")
        self.assertEqual(payload["events"][0]["level"], "high")
        self.assertEqual(len(payload["steps"]), 5)
        self.assertNotIn(SETTINGS.api_key, output.getvalue())


if __name__ == "__main__":
    unittest.main()

import gzip
import http.client
import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

from src.events import (EventError, _get_json, search_mentions, _num, fetch_announcement_content, fetch_announcements, fetch_financials,
                        fetch_forecasts, fetch_holder_changes, parse_announcement_content,
                        parse_announcements, parse_financials, parse_forecasts, parse_holder_changes,
                        parse_unlocks, stock_code)
from src.market import TZ


class EventParsingTests(unittest.TestCase):
    def test_num_keeps_fixed_point_and_rejects_invalid(self):
        self.assertEqual(_num(300), "300")
        self.assertEqual(_num(76.8578), "76.8578")
        self.assertEqual(_num(442137424000, "0.01", 100000000), "4421.37")
        for value in (None, "", "abc", float("nan"), float("inf")):
            self.assertEqual(_num(value), "")

    def test_stock_code_rejects_indices_and_ambiguous_codes(self):
        self.assertEqual(stock_code("sz300750"), "300750")
        self.assertEqual(stock_code("600519.SH"), "600519")
        with self.assertRaises(EventError):
            stock_code("sh000001")
        with self.assertRaises(ValueError):
            stock_code("300750")

    def test_announcements_filter_by_date_and_skip_malformed(self):
        payload = {"data": {"list": [
            {"art_code": "AN1", "title": "三季度报告", "notice_date": "2026-10-21 00:00:00",
             "columns": [{"column_name": "三季度报告全文"}]},
            {"art_code": "AN0", "title": "旧公告", "notice_date": "2026-09-01 00:00:00", "columns": []},
            {"art_code": "AN9", "title": "截止日之后", "notice_date": "2026-10-25", "columns": []},
            {"art_code": "", "title": "缺编号", "notice_date": "2026-10-22"},
            {"art_code": "AN2", "title": "股东减持计划", "notice_date": "2026-10-23", "columns": "bad"},
            "not a dict"]}}
        result = parse_announcements(payload, "300750", "2026-10-15", "2026-10-24")
        self.assertEqual([a["art_code"] for a in result], ["AN2", "AN1"])
        self.assertEqual(result[1]["categories"], ["三季度报告全文"])
        self.assertTrue(result[0]["url"].startswith("https://data.eastmoney.com/notices/detail/300750/"))
        with self.assertRaises(EventError):
            parse_announcements({"data": None}, "300750", "2026-10-15", "2026-10-24")

    def test_announcement_content_requires_text_and_trusted_pdf(self):
        content = parse_announcement_content({"data": {"notice_title": "减持计划", "notice_content": "  股东拟减持\n不超过1%  ",
                                                       "attach_url": "https://pdf.dfcfw.com/pdf/H2_AN1.pdf"}})
        self.assertEqual(content["text"], "股东拟减持 不超过1%")
        self.assertTrue(content["pdf_url"].startswith("https://pdf.dfcfw.com/"))
        other = parse_announcement_content({"data": {"notice_content": "正文", "attach_url": "https://evil.example/x.pdf"}})
        self.assertEqual(other["pdf_url"], "")
        long = parse_announcement_content({"data": {"notice_content": "字" * 50}}, limit=10)
        self.assertTrue(long["truncated"])
        self.assertEqual(len(long["text"]), 10)
        with self.assertRaises(EventError):
            parse_announcement_content({"data": {"notice_content": "   "}})

    @patch("src.events._get_json")
    def test_announcement_content_rejects_bad_codes_before_request(self, get):
        for value in ("../x", "AN1?x=1", "", None, "A" * 81):
            with self.assertRaises(EventError):
                fetch_announcement_content(value)
        get.assert_not_called()

    def test_unlock_ratios_are_converted_to_percent_with_window(self):
        rows = [{"FREE_DATE": "2026-10-27 00:00:00", "ABLE_FREE_SHARES": 9660.1307, "FREE_RATIO": 0.119095260147,
                 "TOTAL_RATIO": 0.106331895841, "LIFT_MARKET_CAP": 78440.261284, "FREE_SHARES_TYPE": "定向增发机构配售股份"},
                {"FREE_DATE": "2027-06-01", "ABLE_FREE_SHARES": 1, "FREE_RATIO": 0.5},
                {"FREE_DATE": "2026-10-28", "ABLE_FREE_SHARES": None, "FREE_RATIO": 0.5}]
        result = parse_unlocks(rows, "002079", "2026-10-19", "2026-11-25")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["float_ratio_pct"], "11.91")
        self.assertEqual(result[0]["total_ratio_pct"], "10.63")
        self.assertEqual(result[0]["market_cap_yi"], "7.84")
        self.assertEqual(result[0]["shares_wan"], "9660.1307")

    def test_holder_changes_keep_source_units_and_valid_directions(self):
        rows = [{"NOTICE_DATE": "2026-10-24", "HOLDER_NAME": "中国中信集团有限公司", "DIRECTION": "减持", "CHANGE_NUM": 300,
                 "CHANGE_FREE_RATIO": 0.04, "HOLD_RATIO": 1.6, "START_DATE": "2026-10-22", "END_DATE": "2026-10-22"},
                {"NOTICE_DATE": "2026-10-24", "HOLDER_NAME": "某人", "DIRECTION": "转让", "CHANGE_NUM": 1},
                {"NOTICE_DATE": "2026-09-01", "HOLDER_NAME": "旧", "DIRECTION": "增持", "CHANGE_NUM": 1}]
        result = parse_holder_changes(rows, "601212", "2026-10-01", "2026-10-28")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "股东减持")
        self.assertEqual(result[0]["shares_wan"], "300")
        self.assertEqual(result[0]["float_ratio_pct"], "0.04")
        self.assertEqual(result[0]["period"], ["2026-10-22", "2026-10-22"])

    def test_forecast_amounts_convert_to_yi(self):
        rows = [{"NOTICE_DATE": "2025-01-03", "REPORT_DATE": "2024-12-31", "PREDICT_FINANCE": "营业收入",
                 "PREDICT_TYPE": "略增", "PREDICT_AMT_LOWER": 173800000000, "PREDICT_AMT_UPPER": 173800000000,
                 "ADD_AMP_LOWER": 15.44, "ADD_AMP_UPPER": None}]
        result = parse_forecasts(rows, "600519", "2025-01-01", "2025-01-31")
        self.assertEqual(result[0]["amount_low_yi"], "1738.00")
        self.assertEqual(result[0]["yoy_low_pct"], "15.44")
        self.assertEqual(result[0]["yoy_high_pct"], "")

    def test_financials_sorted_and_missing_fields_blank(self):
        rows = [{"REPORTDATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 100000000},
                {"REPORTDATE": "2026-09-30", "NOTICE_DATE": "2026-10-21", "TOTAL_OPERATE_INCOME": 442137424000,
                 "PARENT_NETPROFIT": 64530000000, "YSTZ": 56.19, "SJLTZ": 31.6, "XSMLL": 22.72},
                {"REPORTDATE": None}]
        result = parse_financials(rows, "300750")
        self.assertEqual([r["period"] for r in result], ["2026-09-30", "2026-06-30"])
        self.assertEqual(result[0]["revenue_yi"], "4421.37")
        self.assertEqual(result[0]["net_profit_yoy_pct"], "31.60")
        self.assertEqual(result[1]["net_profit_yi"], "")
        self.assertEqual(result[1]["date"], "2026-06-30")


class ReplayCutoffTests(unittest.TestCase):
    @patch("src.market.now", return_value=datetime(2026, 8, 1, 9, tzinfo=TZ))
    @patch("src.events._get_json", return_value={"data": {"list": []}, "result": None})
    def test_requests_stop_at_the_replay_date(self, get, _):
        fetch_announcements("sh601318", days=3)
        fetch_holder_changes("sh601318", days=7)
        fetch_forecasts("sh601318", days=7)
        fetch_financials("sh601318")
        urls = [unquote(call.args[0]) for call in get.call_args_list]
        self.assertIn("begin_time=2026-07-29&end_time=2026-08-01", urls[0])
        for url in urls[1:]:
            self.assertIn("(NOTICE_DATE<='2026-08-01')", url)


class EventTransportTests(unittest.TestCase):
    def response(self, body=None, error=None):
        handle = MagicMock()
        handle.__enter__.return_value = handle
        if error:
            handle.read.side_effect = error
        else:
            handle.read.return_value = body
        return handle

    @patch("urllib.request.urlopen")
    def test_truncated_body_retried_once(self, urlopen):
        urlopen.side_effect = [self.response(error=http.client.IncompleteRead(b"{")),
                               self.response(json.dumps({"ok": 1}).encode())]
        self.assertEqual(_get_json("https://datacenter-web.eastmoney.com/x"), {"ok": 1})
        self.assertEqual(urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_repeated_truncation_and_bad_json_raise_event_error(self, urlopen):
        urlopen.side_effect = [self.response(error=http.client.IncompleteRead(b"{"))] * 2
        with self.assertRaises(EventError):
            _get_json("https://datacenter-web.eastmoney.com/x")
        urlopen.side_effect = [self.response(b"<html>")]
        with self.assertRaises(EventError):
            _get_json("https://datacenter-web.eastmoney.com/x")

    @patch("urllib.request.urlopen")
    def test_mention_search_is_retried_once(self, urlopen):
        body = json.dumps({"result": {"noticeWeb": []}}).encode()
        urlopen.side_effect = [TimeoutError(), self.response(b"x(" + body + b")")]
        self.assertEqual(search_mentions("宁德时代", "供货", "300750"), [])
        urlopen.side_effect = [TimeoutError(), TimeoutError()]
        with self.assertRaises(EventError):
            search_mentions("宁德时代", "供货", "300750")
        self.assertEqual(urlopen.call_count, 4)

    @patch("urllib.request.urlopen")
    def test_gzip_and_jsonp_bodies_are_unwrapped(self, urlopen):
        urlopen.side_effect = [self.response(gzip.compress(b'{"zygcfx": []}')), self.response(b'x({"result": 1})')]
        self.assertEqual(_get_json("https://emweb.securities.eastmoney.com/x"), {"zygcfx": []})
        self.assertEqual(_get_json("https://search-api-web.eastmoney.com/x", jsonp=True), {"result": 1})


if __name__ == "__main__":
    unittest.main()

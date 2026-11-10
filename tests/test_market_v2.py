import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

from src.market import (MarketError, comparison, parse_history, parse_quotes,
                        resolve_symbol, parse_suggestions, search_symbols, local_search, TZ)


def history(symbol="sh000001", closes=(100, 90, 110)):
    return {"symbol":symbol, "source":"测试来源", "url":"https://gu.qq.com/"+symbol,
            "fetched_at":"2026-10-12T16:00:00+08:00", "adjustment":"指数点位",
            "bars":[{"date":f"2026-10-{day:02}", "open":str(c), "close":str(c), "high":str(c),
                     "low":str(c), "volume":"100"} for day,c in zip([8,9,12],closes)]}


class MarketTests(unittest.TestCase):
    def test_search_distinguishes_ambiguous_codes_and_filters_unsupported_types(self):
        payload = 'v_hint=' + json.dumps('sh~000001~上证指数~szzs~ZS^sz~000001~平安银行~payh~GP-A^jj~000001~基金~jj~KJ^hk~00700~腾讯~tx~GP')
        self.assertEqual(parse_suggestions(payload), [("sh000001", "上证指数"), ("sz000001", "平安银行")])
        with self.assertRaises(MarketError):
            parse_suggestions('alert("bad")')
        self.assertIn(("sz300750", "sz300750"), local_search("sz300750"))

    @patch("src.market._get")
    def test_search_uses_fixed_endpoint_and_bounds_input(self, get):
        get.return_value = 'v_hint=' + json.dumps('sz~300750~宁德时代~ndsd~GP-A')
        self.assertIn(("sz300750", "宁德时代"), search_symbols("宁德"))
        self.assertTrue(get.call_args.args[0].startswith("https://smartbox.gtimg.cn/s3/?q="))
        get.reset_mock()
        self.assertEqual(search_symbols("https://bad.example"), [])
        get.assert_not_called()

    def test_symbols_are_unambiguous_and_bounded(self):
        self.assertEqual(resolve_symbol("600519.SH"),"sh600519")
        self.assertEqual(resolve_symbol("沪深300"),"sh000300")
        for value in ("000001", "https://bad.example", "sh000001,sz399001", "hk00700", "sh900001"):
            with self.assertRaises(MarketError): resolve_symbol(value)

    def test_quote_timestamp_and_percent_validation(self):
        fields=[""]*38
        for k,v in {1:"上证指数",2:"000001",3:"101",4:"100",30:"20261012150000",32:"1"}.items(): fields[k]=v
        text='v_sh000001="'+"~".join(fields)+'";'
        q=parse_quotes(text,["sh000001"],"2026-10-12T16:00:00+08:00")["sh000001"]
        self.assertEqual(q.change_pct,"1")
        fields[32]="20"
        with self.assertRaises(MarketError): parse_quotes('v_sh000001="'+"~".join(fields)+'";',["sh000001"],q.fetched_at)
        fields[32]="1";fields[30]="20271012150000"
        with self.assertRaises(MarketError): parse_quotes('v_sh000001="'+"~".join(fields)+'";',["sh000001"],q.fetched_at)

    def test_invalid_history_rejected_and_unadjusted_stock_uses_day(self):
        rows=[["2026-10-08","100","110","111","99","50"]]
        self.assertEqual(len(parse_history({"data":{"sh000001":{"day":rows}}},"sh000001")),1)
        for invalid in (rows*2, [["2026-10-08","100","110","90","99","50"]], [["2026-10-08","NaN","110","111","99","50"]]):
            with self.assertRaises(MarketError): parse_history({"data":{"sh000001":{"day":invalid}}},"sh000001")
        later=[["2026-10-08","100","110","111","99","50"],["2026-10-09","110","120","121","109","50"]]
        with patch("src.market.now", return_value=datetime(2026,10,8,15,tzinfo=TZ)):
            self.assertEqual([b["date"] for b in parse_history({"data":{"sh000001":{"day":later}}},"sh000001")],["2026-10-08"])
        # 新股没有除权时腾讯只返回 day，此时与前复权一致。
        self.assertEqual(len(parse_history({"data":{"sh688836":{"day":rows}}},"sh688836")),1)
        adjusted=[["2026-10-08","50","55","56","49","50"]]
        self.assertEqual(parse_history({"data":{"sz000333":{"day":rows,"qfqday":adjusted}}},"sz000333")[0]["close"],"55")
        with self.assertRaises(MarketError): parse_history({"data":{"sz000333":{}}},"sz000333")

    def test_common_dates_returns_drawdown_and_insufficient_data(self):
        one=history();two=history("sh000300",(200,180,220))
        summary,points=comparison({"sh000001":one,"sh000300":two},30,date(2026,10,12))
        self.assertEqual(summary[0]["return_pct"],"10.0000")
        self.assertEqual(summary[0]["max_drawdown_pct"],"-10.0000")
        self.assertEqual(points[0]["indexed"],"100.0000")
        two["bars"]=two["bars"][-1:]
        with self.assertRaises(MarketError): comparison({"sh000001":one,"sh000300":two},30,date(2026,10,12))


if __name__ == "__main__":
    unittest.main()

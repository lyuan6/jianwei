"""Public eastmoney A-share event data: announcements, unlocks, holder changes, forecasts, financials.

Parsers are separate from fetchers so they can be tested offline. Units follow the source:
shares in 万股, ratios in percent, amounts converted from 元 to 亿元 with Decimal.
"""
import gzip
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from src import market
from src.market import is_index, resolve_symbol

SOURCE = "东方财富公开数据"
ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann?"
ANN_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann?"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get?"
SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp?"
BUSINESS_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?"
BOARDS_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?"
BOARD_QUOTE_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get?"
# 全文检索也会命中基金季报的持仓表、债券和指数文件，它们不是产业链关系。
NOT_COMPANY = ("基金", "ETF", "LOF", "债", "可转换", "指数", "联接", "REIT")
YI = Decimal("100000000")


class EventError(ValueError):
    pass


def _get_json(url, attempts=2, *, jsonp=False):
    # Only the fixed endpoints above are requested; never accept model-provided URLs.
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                data = response.read(3_000_001)
            break
        except http.client.IncompleteRead:
            # Connections to this source are sometimes cut mid-body; one fresh request usually succeeds.
            if attempt == attempts - 1:
                raise EventError("事件数据传输中断，请稍后重试。") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise EventError("事件数据源暂时不可用，请稍后重试。") from None
    if len(data) > 3_000_000:
        raise EventError("事件数据响应过大，已停止处理。")
    try:
        # F10 接口对较大的响应不论请求头都返回 gzip。
        text = (gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data).decode("utf-8")
        return json.loads(text[text.index("(") + 1:text.rindex(")")] if jsonp else text)
    except (ValueError, UnicodeError, OSError, EOFError):
        raise EventError("事件数据格式无效。") from None


def stock_code(symbol):
    symbol = resolve_symbol(symbol)
    if is_index(symbol):
        raise EventError("指数没有公告和股东事件，请选择个股。")
    return symbol[2:]


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _num(value, places=None, scale=None):
    """Exact Decimal string from a source number; empty string when absent or invalid."""
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return ""
    if not number.is_finite():
        return ""
    if scale:
        number = number / scale
    # Fixed-point text: normalize() alone would turn 300 into "3E+2".
    return str(number.quantize(Decimal(places))) if places else format(number.normalize(), "f")


def _rows(payload):
    if not isinstance(payload, dict):
        raise EventError("事件数据结构无效。")
    result = payload.get("result")
    # The datacenter returns result=null when a stock simply has no records.
    if result is None:
        return []
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise EventError("事件数据结构无效。")
    return [row for row in rows if isinstance(row, dict)]


def _window(days):
    today = market.now().date()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _datacenter(report, code, sort_column, *, ascending=False, size=20, until=None):
    # 截止日期交给接口过滤，回放过去的日期时第一页才不会被之后的记录占满。
    extra = f"(NOTICE_DATE<='{until}')" if until else ""
    params = urllib.parse.urlencode({"reportName": report, "columns": "ALL", "pageSize": size, "pageNumber": 1,
                                     "sortColumns": sort_column, "sortTypes": 1 if ascending else -1,
                                     "filter": f'(SECURITY_CODE="{code}")' + extra})
    return _rows(_get_json(DATACENTER_URL + params))


def parse_announcements(payload, code, since, until):
    try:
        items = payload["data"]["list"]
        if not isinstance(items, list):
            raise TypeError
    except (KeyError, TypeError):
        raise EventError("公告接口返回结构无效。") from None
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        day, title, art_code = _day(item.get("notice_date")), item.get("title"), item.get("art_code")
        if not day or not since <= day <= until or not isinstance(title, str) or not isinstance(art_code, str) or not title or not art_code:
            continue
        columns = item.get("columns") if isinstance(item.get("columns"), list) else []
        categories = [c["column_name"][:30] for c in columns if isinstance(c, dict) and isinstance(c.get("column_name"), str)]
        result.append({"type": "公告", "code": code, "date": day, "title": title[:200], "categories": categories,
                       "art_code": art_code[:80], "source": SOURCE,
                       "url": f"https://data.eastmoney.com/notices/detail/{code}/{urllib.parse.quote(art_code[:80])}.html"})
    return sorted(result, key=lambda x: x["date"], reverse=True)


def fetch_announcements(symbol, days=7, size=30):
    code = stock_code(symbol)
    since, until = _window(days)
    params = urllib.parse.urlencode({"sr": -1, "page_size": max(1, min(size, 50)), "page_index": 1, "ann_type": "A",
                                     "client_source": "web", "stock_list": code, "begin_time": since, "end_time": until})
    return parse_announcements(_get_json(ANN_URL + params), code, since, until)


def fetch_announcement_history(symbol, days=365, pages=5):
    code = stock_code(symbol)
    since, until = _window(days)
    result, seen = [], set()
    for page in range(1, pages + 1):
        params = urllib.parse.urlencode({"sr": -1, "page_size": 100, "page_index": page, "ann_type": "A",
                                         "client_source": "web", "stock_list": code, "begin_time": since, "end_time": until})
        payload = _get_json(ANN_URL + params)
        for item in parse_announcements(payload, code, since, until):
            if item["art_code"] not in seen:
                seen.add(item["art_code"])
                result.append(item)
        if len(payload["data"]["list"]) < 100:
            break
    return result


def parse_announcement_content(payload, limit=6000):
    try:
        data = payload["data"]
        text = data.get("notice_content") or ""
        title = data.get("notice_title") or ""
    except (KeyError, TypeError, AttributeError):
        raise EventError("公告正文结构无效。") from None
    if not isinstance(text, str) or not text.strip():
        raise EventError("公告没有可读取的正文，请查看原文 PDF。")
    text = " ".join(text.split())
    return {"title": str(title)[:200], "text": text[:limit], "truncated": len(text) > limit,
            "pdf_url": data.get("attach_url") if str(data.get("attach_url", "")).startswith("https://pdf.dfcfw.com/") else ""}


def fetch_announcement_content(art_code):
    if not isinstance(art_code, str) or not art_code.isalnum() or len(art_code) > 80:
        raise EventError("公告编号无效。")
    params = urllib.parse.urlencode({"art_code": art_code, "client_source": "web", "page_index": 1})
    return parse_announcement_content(_get_json(ANN_CONTENT_URL + params))


def parse_unlocks(rows, code, start, end):
    result = []
    for row in rows:
        day = _day(row.get("FREE_DATE"))
        shares = _num(row.get("ABLE_FREE_SHARES") or row.get("CURRENT_FREE_SHARES"))
        if not day or not start <= day <= end or not shares:
            continue
        # FREE_RATIO is relative to float before the release; TOTAL_RATIO to total shares.
        result.append({"type": "解禁", "code": code, "date": day, "shares_wan": shares,
                       "float_ratio_pct": _num(row.get("FREE_RATIO"), "0.01", Decimal("0.01")),
                       "total_ratio_pct": _num(row.get("TOTAL_RATIO"), "0.01", Decimal("0.01")),
                       "market_cap_yi": _num(row.get("LIFT_MARKET_CAP"), "0.01", Decimal(10000)),
                       "share_type": str(row.get("FREE_SHARES_TYPE") or "")[:60], "source": SOURCE,
                       "url": f"https://data.eastmoney.com/dxf/q/{code}.html"})
    return sorted(result, key=lambda x: x["date"])


def fetch_unlocks(symbol, past_days=7, future_days=30):
    code = stock_code(symbol)
    today = market.now().date()
    rows = _datacenter("RPT_LIFT_STAGE", code, "FREE_DATE", size=30)
    return parse_unlocks(rows, code, (today - timedelta(days=past_days)).isoformat(),
                         (today + timedelta(days=future_days)).isoformat())


def parse_holder_changes(rows, code, since, until):
    result = []
    for row in rows:
        day, direction, name = _day(row.get("NOTICE_DATE")), row.get("DIRECTION"), row.get("HOLDER_NAME")
        shares = _num(row.get("CHANGE_NUM"))
        if not day or not since <= day <= until or direction not in {"增持", "减持"} or not isinstance(name, str) or not shares:
            continue
        result.append({"type": "股东" + direction, "code": code, "date": day, "holder": name[:100],
                       "direction": direction, "shares_wan": shares,
                       "float_ratio_pct": _num(row.get("CHANGE_FREE_RATIO")),
                       "hold_ratio_after_pct": _num(row.get("HOLD_RATIO")),
                       "period": [_day(row.get("START_DATE")), _day(row.get("END_DATE"))],
                       "source": SOURCE, "url": f"https://data.eastmoney.com/executive/gdzjc/{code}.html"})
    return sorted(result, key=lambda x: x["date"], reverse=True)


def fetch_holder_changes(symbol, days=30):
    code = stock_code(symbol)
    since, until = _window(days)
    rows = _datacenter("RPT_SHARE_HOLDER_INCREASE", code, "NOTICE_DATE", until=until)
    return parse_holder_changes(rows, code, since, until)


def parse_forecasts(rows, code, since, until):
    result = []
    for row in rows:
        day, period, metric = _day(row.get("NOTICE_DATE")), _day(row.get("REPORT_DATE")), row.get("PREDICT_FINANCE")
        if not day or not period or not since <= day <= until or not isinstance(metric, str) or not metric:
            continue
        result.append({"type": "业绩预告", "code": code, "date": day, "period": period, "metric": metric[:60],
                       "forecast_type": str(row.get("PREDICT_TYPE") or "")[:20],
                       "amount_low_yi": _num(row.get("PREDICT_AMT_LOWER"), "0.01", YI),
                       "amount_high_yi": _num(row.get("PREDICT_AMT_UPPER"), "0.01", YI),
                       "yoy_low_pct": _num(row.get("ADD_AMP_LOWER"), "0.01"),
                       "yoy_high_pct": _num(row.get("ADD_AMP_UPPER"), "0.01"),
                       "content": str(row.get("PREDICT_CONTENT") or "")[:400], "source": SOURCE,
                       "url": f"https://data.eastmoney.com/bbsj/yjyg/{code}.html"})
    return sorted(result, key=lambda x: x["date"], reverse=True)


def fetch_forecasts(symbol, days=30):
    code = stock_code(symbol)
    since, until = _window(days)
    rows = _datacenter("RPT_PUBLIC_OP_NEWPREDICT", code, "NOTICE_DATE", until=until)
    return parse_forecasts(rows, code, since, until)


def parse_financials(rows, code):
    result = []
    for row in rows:
        period = _day(row.get("REPORTDATE"))
        if not period:
            continue
        result.append({"type": "财报", "code": code, "period": period, "date": _day(row.get("NOTICE_DATE")) or period,
                       "revenue_yi": _num(row.get("TOTAL_OPERATE_INCOME"), "0.01", YI),
                       "net_profit_yi": _num(row.get("PARENT_NETPROFIT"), "0.01", YI),
                       "revenue_yoy_pct": _num(row.get("YSTZ"), "0.01"),
                       "net_profit_yoy_pct": _num(row.get("SJLTZ"), "0.01"),
                       "revenue_qoq_pct": _num(row.get("YSHZ"), "0.01"),
                       "net_profit_qoq_pct": _num(row.get("SJLHZ"), "0.01"),
                       "gross_margin_pct": _num(row.get("XSMLL"), "0.01"),
                       "roe_pct": _num(row.get("WEIGHTAVG_ROE"), "0.01"), "eps": _num(row.get("BASIC_EPS"), "0.01"),
                       "note": "营收、净利润为报告期累计值；环比字段为东方财富口径的单季环比。",
                       "source": SOURCE, "url": f"https://data.eastmoney.com/bbsj/{code}.html"})
    return sorted(result, key=lambda x: x["period"], reverse=True)


def fetch_financials(symbol, periods=4):
    code = stock_code(symbol)
    rows = _datacenter("RPT_LICO_FN_CPD", code, "REPORTDATE", size=max(1, min(periods, 8)), until=market.now().date().isoformat())
    return parse_financials(rows, code)


def parse_mentions(payload, name, own_code):
    """全文检索结果里，其他上市公司公告中提到 name 的段落。"""
    try:
        items = payload["result"]["noticeWeb"] or []
    except (KeyError, TypeError):
        raise EventError("公告检索结构无效。") from None
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        company, url = str(item.get("securityFullName") or ""), str(item.get("url") or "")
        text = " ".join(str(item.get("content") or "").replace("<em>", "").replace("</em>", "").split())
        parts = url.split("/notices/detail/")
        code = parts[1].split("/")[0] if len(parts) == 2 else ""
        day = _day(item.get("date"))
        if (not code.isdigit() or len(code) != 6 or code == own_code or not day or name not in text
                or not company or any(word in company for word in NOT_COMPANY)):
            continue
        result.append({"company": company[:40], "sec_code": code, "date": day, "title": str(item.get("title") or "")[:120],
                       "text": text[:400], "url": "https://" + url.split("://", 1)[-1]})
    return result


def search_mentions(name, keyword, own_code, page=1):
    param = {"uid": "", "keyword": f"{name} {keyword}", "type": ["noticeWeb"], "client": "web", "clientVersion": "curr",
             "clientType": "web", "param": {"noticeWeb": {"preTag": "", "postTag": "", "pageSize": 50, "pageIndex": page}}}
    url = SEARCH_URL + urllib.parse.urlencode({"cb": "x", "param": json.dumps(param, ensure_ascii=False)})
    # 全文检索接口常在握手或读取时超时，负责人同意对它单独重试一次。
    try:
        payload = _get_json(url, jsonp=True)
    except EventError:
        payload = _get_json(url, jsonp=True)
    return parse_mentions(payload, name, own_code)


def _percent(value):
    text = _num(value)
    return str((Decimal(text) * 100).quantize(Decimal("0.1"))) if text else ""


def parse_business(payload):
    """最近一期主营构成：按产品（没有时按行业）和按地区，比例为百分数。"""
    rows = payload.get("zygcfx") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise EventError("主营构成结构无效。")
    rows = [row for row in rows if isinstance(row, dict) and row.get("REPORT_DATE")]
    if not rows:
        return {"report_date": "", "segments": [], "regions": []}
    latest = max(row["REPORT_DATE"] for row in rows)
    rows = [row for row in rows if row["REPORT_DATE"] == latest]

    def items(kind):
        # “其中：”开头的是上一行的细分，和其他行重复计算。
        return [{"name": str(row.get("ITEM_NAME") or "")[:40], "pct": _percent(row.get("MBI_RATIO")),
                 "margin": _percent(row.get("GROSS_RPOFIT_RATIO"))}
                for row in rows if row.get("MAINOP_TYPE") == kind and not str(row.get("ITEM_NAME") or "").startswith("其中")
                and row.get("ITEM_NAME")]
    return {"report_date": _day(latest), "segments": items("2") or items("1"), "regions": items("3")}


def fetch_business(symbol):
    code = resolve_symbol(symbol)
    return parse_business(_get_json(BUSINESS_URL + urllib.parse.urlencode({"code": code.upper()})))


def parse_boards(payload):
    """F10 板块列表：排名 1、2 是一级、二级行业；标为精准的才是概念板块，指数、资金、风格类标签不算。"""
    rows = payload.get("ssbk") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise EventError("板块数据结构无效。")

    def board(row):
        code, name = str(row.get("BOARD_CODE") or ""), row.get("BOARD_NAME")
        return {"code": f"BK{int(code):04d}", "name": str(name)[:20]} if code.isdigit() and isinstance(name, str) and name else None

    rows = [row for row in rows if isinstance(row, dict)]
    ranked = {row.get("BOARD_RANK"): board(row) for row in rows}
    return {"sector": (ranked.get(1) or {}).get("name", ""), "industry": ranked.get(2),
            "concepts": [b for b in (board(row) for row in rows if str(row.get("IS_PRECISE")) == "1") if b]}


def fetch_boards(code):
    """code 为 6 位证券代码；北交所以 92、4、8 开头。"""
    prefix = "BJ" if code.startswith("92") or code[0] in "48" else "SH" if code[0] in "569" else "SZ"
    return parse_boards(_get_json(BOARDS_URL + urllib.parse.urlencode({"code": prefix + code})))


def parse_board_changes(payload):
    try:
        rows = payload["data"]["diff"]
    except (KeyError, TypeError):
        raise EventError("板块行情结构无效。") from None
    return {row["f12"]: _num(row.get("f3"), "0.01") for row in rows or [] if isinstance(row, dict) and row.get("f12") and _num(row.get("f3"))}


def fetch_board_changes(codes):
    """板块当日涨跌幅（百分数），按代码批量取。"""
    result = {}
    codes = sorted(set(codes))
    for i in range(0, len(codes), 50):
        params = urllib.parse.urlencode({"fltt": 2, "fields": "f12,f3", "secids": ",".join(f"90.{c}" for c in codes[i:i + 50])})
        result |= parse_board_changes(_get_json(BOARD_QUOTE_URL + params))
    return result

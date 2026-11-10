"""Tencent public quote and day-bar adapter, with Decimal return/drawdown calculations."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "腾讯证券公开行情"
DEFAULTS = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
INDICES = {**DEFAULTS,
            "sh000300": "沪深300", "sh000905": "中证500", "sh000688": "科创50"}
CATALOG = {**INDICES, "sh000016": "上证50", "sh000852": "中证1000", "sz000333": "美的集团",
           "sh600519": "贵州茅台", "sz000001": "平安银行", "sh601318": "中国平安"}
ALIASES = {"上证": "sh000001", "沪指": "sh000001", "深成指": "sz399001", "创业板": "sz399006",
           "沪深 300": "sh000300", "科创板50": "sh000688"}


class MarketError(ValueError):
    pass


def decimal_value(value):
    """Parse a plain decimal string; upstream exponent or NaN values are rejected."""
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        raise ValueError("非十进制数值")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError("非十进制数值") from None


def now():
    return datetime.now(TZ)


def resolve_symbol(value):
    value = value.strip()
    for symbol, name in CATALOG.items():
        if value == name:
            return symbol
    if value in ALIASES:
        return ALIASES[value]
    symbol = value.lower()
    suffix = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", symbol)
    if suffix:
        symbol = suffix[2] + suffix[1]
    if is_index(symbol):
        return symbol
    if re.fullmatch(r"(?:sh6\d{5}|sz(?:00|30)\d{4}|bj(?:4\d{5}|8\d{5}|92\d{4}))", symbol):
        return symbol
    raise MarketError("请输入支持的名称或带交易所代码，如 sh600519、sz000333；裸代码可能有歧义。")


def is_index(symbol):
    return symbol in INDICES or symbol in {"sh000016", "sh000852"}


def source_url(symbol):
    return "https://gu.qq.com/" + resolve_symbol(symbol)


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: str
    previous: str
    change_pct: str
    as_of: str
    fetched_at: str


def _get(url, encoding="utf-8"):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise MarketError("行情响应过大，已停止处理。")
        return data.decode(encoding)
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError):
        raise MarketError("行情源暂时不可用，请稍后刷新。") from None


def local_search(query):
    term = query.strip().lower()
    matches = [(symbol, name) for symbol, name in CATALOG.items()
               if term in symbol.lower() or term in name.lower() or ALIASES.get(query) == symbol]
    try:
        symbol = resolve_symbol(query)
        if symbol not in dict(matches):
            matches.append((symbol, CATALOG.get(symbol, symbol)))
    except MarketError:
        pass
    return matches[:12]


def parse_suggestions(text):
    match = re.fullmatch(r'\s*v_hint=("[^\r\n]*");?\s*', text)
    if not match:
        raise MarketError("搜索服务暂时不可用，请输入完整交易所代码。")
    try:
        content = json.loads(match[1])
    except (ValueError, TypeError):
        raise MarketError("搜索结果格式异常，请稍后重试。") from None
    result = {}
    for item in content.split("^"):
        parts = item.split("~")
        if len(parts) < 5 or parts[4] not in {"GP-A", "ZS"}:
            continue
        try:
            symbol = resolve_symbol(parts[0] + parts[1])
        except MarketError:
            continue
        name = parts[2]
        if 1 <= len(name) <= 60 and not re.search(r'[<>\x00-\x1f]', name):
            result[symbol] = name
    return list(result.items())[:12]


def search_symbols(query):
    query = query.strip()
    if not query or len(query) > 40 or not re.fullmatch(r'[\w\u4e00-\u9fff. *-]+', query):
        return local_search("") if not query else []
    params = urllib.parse.urlencode({"q": query, "t": "all"})
    remote = parse_suggestions(_get("https://smartbox.gtimg.cn/s3/?" + params, "gb18030"))
    return list(dict([*local_search(query), *remote]).items())[:12]


def parse_quotes(text, symbols, fetched_at):
    quotes = {}
    for symbol, content in re.findall(r'v_([a-z]{2}\d{6})="([^"\r\n]*)"', text):
        if symbol not in symbols:
            continue
        fields = content.split("~")
        try:
            if len(fields) < 35 or fields[2] != symbol[2:] or not fields[1]:
                raise ValueError()
            price, previous = decimal_value(fields[3]), decimal_value(fields[4])
            if price <= 0 or previous <= 0:
                raise ValueError()
            timestamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
            retrieved = datetime.fromisoformat(fetched_at)
            if timestamp > retrieved + timedelta(minutes=5):
                raise ValueError()
            reported = decimal_value(fields[32])
            calculated = (price / previous - 1) * 100
            if abs(reported - calculated) > Decimal("0.03"):
                raise ValueError()
            quotes[symbol] = Quote(symbol, fields[1][:60], str(price), str(previous),
                                   str(reported), timestamp.isoformat(), fetched_at)
        except (ValueError, IndexError, ArithmeticError):
            continue
    if not quotes:
        raise MarketError("未取得有效报价；请检查代码、交易状态或稍后重试。")
    return quotes


def short_name(name):
    # 行情简称带 *ST、-W、-U 等标记，公告正文里写的是不带标记的名字。
    return re.sub(r"[-－][A-Z]+$|^\*?ST", "", name.replace(" ", ""))


def fetch_quotes(symbols):
    symbols = tuple(dict.fromkeys(resolve_symbol(s) for s in symbols))
    if not 1 <= len(symbols) <= 12:
        raise MarketError("每次获取 1—12 个标的。")
    return parse_quotes(_get("https://qt.gtimg.cn/q=" + ",".join(symbols), "gb18030"),
                        symbols, now().isoformat(timespec="seconds"))


def parse_history(payload, symbol):
    try:
        data = payload["data"][symbol]
        # Indices always use day; A shares prefer qfqday but fall back to day
        # when Tencent omits adjusted bars (new stocks without ex-rights events).
        if is_index(symbol):
            rows = data.get("day")
        else:
            rows = data.get("qfqday") or data.get("day")
        if not isinstance(rows, list) or not rows:
            raise ValueError()
        bars, seen = [], set()
        for raw in rows:
            day = date.fromisoformat(raw[0])
            # 回放过去的日期时丢掉之后的日线；接口的结束日期参数会漏掉当天，所以在本地截断。
            if day > now().date():
                continue
            values = [decimal_value(str(value)) for value in raw[1:6]]
            if len(values) != 5 or day in seen:
                raise ValueError()
            opening, close, high, low, volume = values
            if min(opening, close, high, low) <= 0 or volume < 0 or not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError()
            seen.add(day)
            bars.append(dict(zip(["date", "open", "close", "high", "low", "volume"], [day.isoformat(), *map(str, values)])))
        if not bars:
            raise ValueError()
        return sorted(bars, key=lambda bar: bar["date"])
    except (KeyError, TypeError, ValueError, IndexError, ArithmeticError):
        raise MarketError("日线数据缺失、复权口径不符或字段校验失败，未展示该序列。") from None


def fetch_history(symbol):
    symbol = resolve_symbol(symbol)
    query = urllib.parse.urlencode({"param": f"{symbol},day,,,320,qfq"})
    try:
        payload = json.loads(_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + query))
    except json.JSONDecodeError:
        raise MarketError("日线接口未返回有效 JSON。") from None
    return {"symbol": symbol, "bars": parse_history(payload, symbol), "fetched_at": now().isoformat(timespec="seconds"),
            "source": SOURCE, "url": source_url(symbol), "adjustment": "指数点位" if is_index(symbol) else "前复权"}



def comparison(histories, days, end=None):
    """Align common dates; compute exact returns and rebased points on that same range."""
    end = end or now().date()
    start = end - timedelta(days=days)
    maps = {s: {b["date"]: b for b in h["bars"] if start.isoformat() <= b["date"] <= end.isoformat()}
            for s, h in histories.items()}
    if not maps:
        return [], []
    common = sorted(set.intersection(*(set(items) for items in maps.values())))
    if len(common) < 2:
        raise MarketError("共同日期不足两个交易日，无法计算区间收益。")
    summary, points = [], []
    for symbol, data in maps.items():
        first, last = Decimal(data[common[0]]["close"]), Decimal(data[common[-1]]["close"])
        if first <= 0:
            raise MarketError("比较基准必须大于零。")
        peak = first
        drawdown = Decimal(0)
        for day in common:
            close = Decimal(data[day]["close"])
            peak = max(peak, close)
            drawdown = min(drawdown, (close / peak - 1) * 100)
            points.append({"date": day, "symbol": symbol, "indexed": str((close / first * 100).quantize(Decimal("0.0001")))})
        summary.append({"symbol": symbol, "start": common[0], "end": common[-1], "observations": len(common),
                        "return_pct": str(((last / first - 1) * 100).quantize(Decimal("0.0001"))),
                        "max_drawdown_pct": str(drawdown.quantize(Decimal("0.0001"))),
                        "adjustment": histories[symbol]["adjustment"]})
    return summary, points

"""智能体工具注册表，复用已有数据源和 Decimal 计算。"""

import json
from dataclasses import dataclass
from typing import Callable

from src import events, market, news


def summarize(result):
    if isinstance(result, list):
        titles = [item.get("title") or item.get("type", "") for item in result[:3]]
        return f"{len(result)} 条记录" + ("：" + "；".join(titles) if any(titles) else "")
    if "results" in result:
        return summarize(result["results"])
    if "text" in result:
        return f"{result.get('title', '公告')}：正文 {len(result['text'])} 字" + ("（已截断）" if result.get("truncated") else "")
    if "return_pct" in result:
        return f"{result['start']} 至 {result['end']}，收益 {result['return_pct']}%，最大回撤 {result['max_drawdown_pct']}%"
    return json.dumps(result, ensure_ascii=False)[:240]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    function: Callable
    summary: Callable = summarize

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def invoke(self, arguments):
        _validate(arguments, self.parameters)
        return self.function(**arguments)


def _validate(value, schema):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("工具参数必须是 JSON 对象。")
        if set(value) - schema["properties"].keys() or set(schema["required"]) - value.keys():
            raise ValueError("工具参数缺少必填字段或包含未知字段。")
        for key, item in value.items():
            _validate(item, schema["properties"][key])
    elif kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("工具文本参数不能为空。")
    elif kind == "integer":
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise ValueError(f"工具整数参数应在 {schema['minimum']} 至 {schema['maximum']} 之间。")
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError("比较需要 1 至 12 个标的。")
        for item in value:
            _validate(item, schema["items"])


def get_price_history(code, days):
    history = market.fetch_history(code)
    summary, _ = market.comparison({history["symbol"]: history}, days)
    return {**summary[0], "source": history["source"], "url": history["url"],
            "unit": "收益、回撤为百分数；价格为点" if market.is_index(history["symbol"]) else "收益、回撤为百分数；价格为元/股",
            "first_close": next(bar["close"] for bar in history["bars"] if bar["date"] == summary[0]["start"]),
            "last_close": next(bar["close"] for bar in history["bars"] if bar["date"] == summary[0]["end"])}


def compare(codes, days):
    histories = {code: market.fetch_history(code) for code in dict.fromkeys(map(market.resolve_symbol, codes))}
    summary, _ = market.comparison(histories, days)
    return [{**row, "source": histories[row["symbol"]]["source"], "url": histories[row["symbol"]]["url"],
             "unit": "百分数"} for row in summary]


def named_holdings(holdings):
    # 持仓只存代码；没有名称时模型会凭代码猜公司，曾把宇树科技写成德邦科技。行情失败时持仓照常可用。
    try:
        quotes = market.fetch_quotes([holding["code"] for holding in holdings])
    except market.MarketError:
        return holdings
    return [{**holding, "name": quotes[holding["code"]].name} if holding["code"] in quotes else holding for holding in holdings]


def build_tools(settings, *, holdings=None, ledger=None):
    text = {"type": "string"}
    code = {"type": "string", "description": "带交易所前缀的代码，如 sz002079、sh600519；不要传裸代码。"}
    days = {"type": "integer", "minimum": 1, "maximum": 365}
    window = {"type": "integer", "minimum": 0, "maximum": 365}
    definitions = [
        ("list_announcements", "公告标题、日期、分类、编号及来源链接。", {"code": code, "days": days}, events.fetch_announcements),
        ("read_announcement", "按公告编号读正文，最多 6000 字。", {"art_code": text}, events.fetch_announcement_content),
        ("get_unlocks", "解禁记录；股数为万股，比率为百分数，市值为亿元。float_ratio_pct 相对解禁前流通股。",
         {"code": code, "past_days": window, "future_days": window}, events.fetch_unlocks),
        ("get_holder_changes", "股东增减持；股数为万股，比率为百分数。", {"code": code, "days": days}, events.fetch_holder_changes),
        ("get_forecasts", "业绩预告；金额为亿元，同比为百分数。", {"code": code, "days": days}, events.fetch_forecasts),
        ("get_financials", "主要财务指标；金额为亿元，比率为百分数，EPS 为元/股。",
         {"code": code, "periods": {"type": "integer", "minimum": 1, "maximum": 8}}, events.fetch_financials),
        ("get_price_history", "日线摘要、程序计算的区间收益与最大回撤；days 为自然日。",
         {"code": code, "days": days}, get_price_history),
        ("compare", "按共同交易日期计算多只股票或指数的同期收益；days 为自然日。",
         {"codes": {"type": "array", "items": code, "minItems": 1, "maxItems": 12}, "days": days}, compare),
        ("search_news", "搜索事件相关新闻，返回标题、日期、域名、摘要和实际来源链接。",
         {"query": text, "days": days}, lambda query, days: news.search_news(query, settings.exa_api_key, days=days)),
    ]
    registry = {}
    for name, description, properties, function in definitions:
        # 数据源函数沿用 symbol 参数名，模型统一使用 code。
        if name in {"list_announcements", "get_unlocks", "get_holder_changes", "get_forecasts", "get_financials"}:
            source = function
            function = lambda code, _source=source, **kwargs: _source(code, **kwargs)
        registry[name] = Tool(name, description, {"type": "object", "properties": properties,
                                               "required": list(properties), "additionalProperties": False}, function)
    if holdings is not None:
        registry["get_holdings"] = Tool("get_holdings", "读取当前用户持仓，只用于研究。",
                                       {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                                       lambda: named_holdings(holdings))
    if ledger is not None:
        registry["get_commitments"] = Tool("get_commitments", "读取承诺台账：承诺内容、数额、期限、状态、进展和来源；状态和到期提醒由程序判断。codes 为空时读全部持仓。",
                                          {"type": "object", "properties": {"codes": {"type": "array", "items": code, "minItems": 0, "maxItems": 12}},
                                           "required": ["codes"], "additionalProperties": False}, ledger)
    return registry

"""单股事件分析。命令行：python -m src.brief sz002079。"""

import argparse
import fcntl
import json
import re
from datetime import date, timedelta
from decimal import Decimal

from src import agent, market, model_client, prompts
from src.config import Settings, load_env
from src.tools import build_tools

NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
UNIT = r"(?:亿元|万元|元/股|人民币元|元|亿股|万股|股|个百分点|百分数|%|％|倍|点|起|件|人)?"
FIGURE = re.compile(rf"\s*({NUMBER})\s*({UNIT})\s*(?:(?:至|到|~|～|-)\s*({NUMBER})\s*({UNIT})\s*)?")
SCALE = {"万": (Decimal(10_000),), "亿": (Decimal(10_000), Decimal(100_000_000))}
TEXT_NUMBER = re.compile(rf"(?<![\w.+-])({NUMBER})(?![\d.,\w])", re.ASCII)
METADATA = {"url", "pdf_url", "code", "symbol", "art_code", "date", "period", "start", "end", "fetched_at", "source", "domain", "error"}


def _numbers(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in METADATA:
                yield from _numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _numbers(item)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        for match in TEXT_NUMBER.finditer(str(value)):
            yield Decimal(match[1].replace(",", ""))


def verify_figure(value, result):
    match = FIGURE.fullmatch(value)
    if not match:
        return False
    found = set(_numbers(result))
    # 区间两端都要找到；只在末尾写单位时，单位对两端都适用。
    bounds = [(match[1], match[2] or match[4] or "")] + ([(match[3], match[4] or "")] if match[3] else [])
    for number, unit in bounds:
        amount = Decimal(number.replace(",", ""))
        # 公告常写“4,727,000 股”“15,070.18 万元”，回答写“472.7万股”“1.507018亿元”，是同一个数。
        if not {amount, *(amount * scale for scale in SCALE.get(unit[:1], ()))} & found:
            return False
    return True


def validate_events(payload, references):
    events = payload.get("events")
    if not isinstance(events, list):
        raise agent.OutputError("缺少 events 数组。")
    checked = []
    for event in events:
        required = ("level", "tag", "date", "title", "figures", "points", "risk", "refs")
        if not isinstance(event, dict) or not set(required) <= event.keys():
            raise agent.OutputError("事件缺少必要字段。")
        if any(not isinstance(event[key], str) for key in ("level", "tag", "date", "title", "risk")):
            raise agent.OutputError("事件的级别、标签、日期、标题和风险须为文本。")
        if event["level"] not in {"high", "mid", "low"}:
            raise agent.OutputError("level 只能为 high、mid 或 low。")
        try:
            date.fromisoformat(event["date"])
        except ValueError:
            raise agent.OutputError("事件日期须为 YYYY-MM-DD。") from None
        if not isinstance(event["points"], list) or not all(isinstance(point, str) for point in event["points"]):
            raise agent.OutputError("points 须为文本数组。")
        refs = event["refs"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in references for ref in refs):
            raise agent.OutputError("事件 refs 必须使用本次运行的引用编号。")
        if not isinstance(event["figures"], list):
            raise agent.OutputError("figures 须为数组。")
        figures = []
        for figure in event["figures"]:
            if not isinstance(figure, dict) or any(not isinstance(figure.get(key), str) for key in ("label", "value", "ref")):
                raise agent.OutputError("每个 figure 需要文本 label、value、ref。")
            if figure["ref"] not in references:
                raise agent.OutputError("figure 的 ref 必须使用本次运行的引用编号。")
            if figure["ref"] not in refs:
                refs = [*refs, figure["ref"]]
            figures.append({key: figure[key] for key in ("label", "value", "ref")})
            figures[-1]["verification"] = "已核对" if verify_figure(figure["value"], references[figure["ref"]]) else "未核对"
        checked.append({**{key: event[key] for key in required}, "figures": figures, "refs": refs})
    return {"events": checked}


def analyze_stock(code, settings, *, on_step=None):
    code = market.resolve_symbol(code)
    if market.is_index(code):
        raise ValueError("请选择个股进行事件分析。")
    tools, paper = build_tools(settings), agent.Workpaper(on_step)
    today = market.now().date()
    since = (today - timedelta(days=7)).isoformat()
    initial = []
    for name, arguments in [
        ("list_announcements", {"code": code, "days": 3}),
        ("get_unlocks", {"code": code, "past_days": 30, "future_days": 30}),
        ("get_holder_changes", {"code": code, "days": 7}),
        ("get_forecasts", {"code": code, "days": 7}),
        ("get_financials", {"code": code, "periods": 4}),
    ]:
        # 财报按披露日期筛新事件，不能按季度末日期判断。
        recent = (lambda rows: [row for row in rows if since <= row["date"] <= today.isoformat()]) if name == "get_financials" else None
        initial.append(paper.execute(tools, name, arguments, result_filter=recent))
    if all(not item["result"] for item in initial):
        return {"code": code, "status": "无新事件", "events": [], "steps": paper.steps,
                "references": paper.references, "usage": []}
    task = f"股票 {code}，检查日期 {today.isoformat()}。公告近 3 天、解禁前后 30 天、增减持/业绩预告/新财报近 7 天。\n" + prompts.BRIEF
    result = agent.run(task, [{"role": "user", "content": "程序预取数据（财报已按披露日期筛选近 7 天）：\n" + json.dumps(initial, ensure_ascii=False)}],
                       tools, 6, settings=settings, workpaper=paper, validate=validate_events)
    return {"code": code, "status": "数据不全" if any(step["error"] for step in paper.steps) else "完成",
            **result["result"], "steps": result["steps"], "references": result["references"], "usage": result["usage"]}


def _stock_names(codes):
    # 导语用公司名；行情接口失败不值得丢掉晨报，退回代码。
    try:
        quotes = market.fetch_quotes(codes)
    except market.MarketError:
        return {}
    return {code: market.short_name(quote.name) for code, quote in quotes.items()}


def _fallback_lede(events, names):
    parts = []
    for level, name in (("high", "重点"), ("mid", "留意")):
        items = [f"{names.get(event['code'], event['code'])} {event['tag']}" for event in events if event["level"] == level]
        if items:
            parts.append(f"{name}：" + "、".join(items))
    return "；".join(parts) + "。" if parts else "今日仅有一般事项。"


def generate_brief(codes, settings, day, *, on_step=None):
    stocks, steps = [], []
    for code in sorted(set(codes)):
        def record(step):
            steps.append({"code": code, **step})
            if on_step is not None:
                on_step(steps)
        # 单只股票失败只影响这一只；全部失败时整个任务失败，不缓存空晨报。
        try:
            stocks.append(analyze_stock(code, settings, on_step=record))
        except ValueError as exc:
            error = exc
            stocks.append({"code": code, "status": "失败", "error": str(exc), "events": [], "usage": [],
                           "steps": [step for step in steps if step["code"] == code], "references": {}})
    if all(stock["status"] == "失败" for stock in stocks):
        raise error
    events = [{"code": stock["code"], **event} for stock in stocks for event in stock["events"]]
    counts = {level: sum(event["level"] == level for event in events) for level in ("high", "mid", "low")}
    incomplete = any(stock["status"] in ("数据不全", "失败") for stock in stocks)
    # 导语只接收全部 figures 已核对的事件及其数值，不传未经核对的分析段落。
    names = _stock_names(codes) if events else {}
    verified = [{"name": names.get(event["code"], event["code"]), "level": event["level"], "tag": event["tag"], "date": event["date"],
                 "figures": event["figures"]} for event in events
                if all(figure["verification"] == "已核对" for figure in event["figures"])]
    if not events:
        lede = "部分股票未完成分析，暂不能确认是否有新事件。" if incomplete else "持仓窗口内没有新事件。"
    else:
        lede = _fallback_lede(events, names)
    if verified:
        # 导语失败不值得丢掉已完成的各股分析，改用程序生成的导语。
        try:
            intro = model_client.complete_json([
                {"role": "system", "content": prompts.INTRO},
                {"role": "user", "content": json.dumps(verified, ensure_ascii=False)},
            ], settings)
            if isinstance(intro.get("lede"), str):
                lede = intro["lede"]
        except model_client.ModelError:
            pass
    return {"date": day, "title": f"今日 {counts['high']} 项重点、{counts['mid']} 项留意", "lede": lede,
            "counts": counts, "stocks": stocks, "events": events, "steps": steps,
            "status": "数据不全" if incomplete else "完成"}


def get_or_generate(codes, settings, store, day, *, on_step=None):
    from src.storage import portfolio_key
    # 文件锁覆盖 web 线程与定时脚本；进程退出会自动释放，不留永久的“生成中”记录。
    lock = store.path.parent / f"brief-{portfolio_key(codes)}-{day}.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        cached = store.brief(codes, day)
        if cached is not None:
            return cached
        result = generate_brief(codes, settings, day, on_step=on_step)
        store.save_brief(codes, day, result)
        from src import timeline
        for stock in result["stocks"]:
            # 脉络归类失败不影响已保存的晨报。
            try:
                timeline.attach(stock["code"], stock, settings, store)
            except ValueError as exc:
                print(f"{stock['code']} 脉络归类失败：{exc}")
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="分析单只股票并输出事件 JSON 和工作底稿")
    parser.add_argument("code", help="带交易所的股票代码，如 sz002079")
    args = parser.parse_args(argv)
    load_env()
    try:
        result = analyze_stock(args.code, Settings.from_env())
    except ValueError as exc:
        parser.exit(1, f"分析失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

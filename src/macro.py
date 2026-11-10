"""宏观事件：检索全国和全球层面的政策、利率、贸易、大宗商品和地缘事件，模型挑出后标注相关行业。

命令行：python -m src.macro --months 3  回填近几个月；定时任务每天只看近两天。
"""

import argparse
import json
from datetime import date, timedelta

from src import agent, industry, market, news, prompts
from src.brief import _numbers
from src.config import Settings, load_env
from src.timeline import _text, unverified

QUERIES = ("中国人民银行 货币政策 利率 降准", "美联储 利率决议", "国务院 财政政策 产业政策 出台", "关税 出口管制 贸易措施",
           "国际油价 大宗商品 铜价 锂价 大幅波动", "地缘冲突 供应链 中断")
KINDS = ("货币政策", "财政产业政策", "贸易", "大宗商品", "地缘")
MAX_INDUSTRIES = 6


def validator(ids, names, limit):
    def validate(payload, references):
        items = payload.get("events")
        if not isinstance(items, list) or len(items) > limit:
            raise agent.OutputError(f"events 须为最多 {limit} 条的数组。")
        checked = []
        for item in items:
            if not isinstance(item, dict) or item.get("id") not in ids:
                raise agent.OutputError("id 必须是输入里的新闻编号。")
            if item.get("kind") not in KINDS:
                raise agent.OutputError("kind 只能是：" + "、".join(KINDS) + "。")
            related = item.get("industries")
            if not isinstance(related, list) or len(related) > MAX_INDUSTRIES or any(name not in names for name in related):
                raise agent.OutputError(f"industries 须为输入里的行业，最多 {MAX_INDUSTRIES} 个。")
            checked.append({"id": item["id"], "kind": item["kind"], "industries": related,
                            "title": _text(item.get("title"), "title"), "summary": _text(item.get("summary"), "summary"),
                            "reason": _text(item.get("reason", ""), "reason")})
        return {"events": checked}
    return validate


def pick(start, end, names, settings, limit):
    """一个时间段：每个主题检索一次，合在一起让模型挑。返回事件列表。"""
    rows, seen = {}, set()
    for query in QUERIES:
        # 单个主题检索失败只少一部分候选，其他主题照常。
        try:
            found = news.search_news(query, settings.exa_api_key, since=start, until=end, num=5, max_chars=400)["results"]
        except ValueError as exc:
            print(f"{query} 检索失败：{exc}")
            continue
        for row in found:
            if row["url"] not in seen and row["date"]:
                seen.add(row["url"])
                rows[f"N{len(rows) + 1}"] = row
    if not rows:
        return []
    payload = {"period": f"{start} 至 {end}", "industries": sorted(names),
               "news": [{"id": k, "title": r["title"], "date": r["date"], "domain": r["domain"], "text": r["text"]} for k, r in rows.items()]}
    chosen = agent.complete([{"role": "user", "content": prompts.MACRO.format(limit=limit) + "\n" + json.dumps(payload, ensure_ascii=False)}],
                            settings, validator(set(rows), names, limit))["result"]["events"]
    events = []
    for item in chosen:
        row = rows[item["id"]]
        events.append({"date": row["date"], "kind": item["kind"], "title": item["title"], "summary": item["summary"],
                       "reason": item["reason"], "industries": item["industries"], "url": row["url"], "domain": row["domain"],
                       "unverified": unverified(item["summary"] + " " + item["reason"], set(_numbers([row])))})
    return events


def collect(store, settings, *, days=2, limit=2):
    """定时任务：看近两天，多数日子没有合适的事件。返回新增条数。"""
    today = market.now().date()
    try:
        events = pick((today - timedelta(days=days)).isoformat(), today.isoformat(), industry.holding_industries(store), settings, limit)
    except ValueError as exc:
        print(f"宏观事件未整理：{exc}")
        return 0
    return store.add_macro_events(events)


def backfill(store, settings, months=3):
    """按半个月一段回填，避免一次检索只拿到最新几天。返回新增条数。"""
    today, names, added = market.now().date(), industry.holding_industries(store), 0
    end: date = today
    for _ in range(months * 2):
        start = end - timedelta(days=15)
        try:
            events = pick(start.isoformat(), end.isoformat(), names, settings, 3)
        except ValueError as exc:
            print(f"{start} 至 {end} 未整理：{exc}")
            events = []
        count = store.add_macro_events(events)
        print(f"{start} 至 {end}：{len(events)} 条，新增 {count} 条。")
        added += count
        end = start
    return added


def main(argv=None):
    from src.accounts import default_store
    from src.storage import Storage
    parser = argparse.ArgumentParser(description="回填或更新宏观事件")
    parser.add_argument("--months", type=int, default=0, help="回填近几个月；不填只看近两天")
    args = parser.parse_args(argv)
    load_env()
    store, settings = Storage(default_store().path), Settings.from_env()
    print(f"新增 {backfill(store, settings, args.months) if args.months else collect(store, settings)} 条宏观事件。")


if __name__ == "__main__":
    main()

"""Exa search smoke test for Chinese A-share news. Prints results and cost, never the key.

    python -m scripts.check_exa                  # 5 queries, type=auto
    python -m scripts.check_exa fast auto deep   # compare search types on 2 queries
"""
import re
import sys

from src.config import Settings, load_env
from src.news import NewsError, search_news

# Real events from the last two weeks, checked against eastmoney data on 2026-10-26.
QUERIES = [
    "宁德时代 2026年三季度报告 营收 净利润",
    "宇树科技 股价 涨停",
    "白银有色 中信集团 减持",
    "苏州固锝 限售股 解禁",
    "贵州茅台 最新消息",
]
# One "why" question and one plain event, to see whether a heavier search type adds anything.
COMPARE_QUERIES = ["宇树科技 10月23日 20cm涨停 原因", "白银有色 中信集团 减持"]
CJK = re.compile(r"[一-鿿]")


def show(query, result):
    rows = result["results"]
    chinese = sum(bool(CJK.search(r["title"])) for r in rows)
    resolved = f"，实际类型 {result['resolved_type']}" if result["resolved_type"] else ""
    print(f"\n== {query}")
    print(f"{len(rows)} 条，中文标题 {chinese} 条，耗时 {result['elapsed_seconds']} 秒，"
          f"费用 {result['cost_usd'] if result['cost_usd'] is not None else '未返回'} 美元{resolved}")
    for r in rows:
        print(f"  {r['date'] or '无日期'}  {r['domain']}  {r['title'][:50]}")
        if r["text"]:
            print(f"      {r['text'][:90]}")
    for key, value in result["other"].items():
        print(f"  [{key}] {value}")
    return chinese


def main():
    load_env()
    try:
        key = Settings.from_env().exa_api_key
    except ValueError as exc:
        sys.exit(f".env 配置有误：{exc}")
    if not key:
        sys.exit("未找到 EXA_API_KEY，请在 .env 中填写。")
    types = sys.argv[1:] or ["auto"]
    queries = QUERIES if len(types) == 1 else COMPARE_QUERIES
    summary = []
    for search_type in types:
        print(f"\n######## type={search_type}")
        found = chinese = failed = 0
        cost, elapsed = 0.0, []
        for query in queries:
            try:
                result = search_news(query, key, search_type=search_type)
            except NewsError as exc:
                print(f"\n== {query}\n失败：{exc}")
                failed += 1
                continue
            chinese += show(query, result)
            found += len(result["results"])
            cost += result["cost_usd"] or 0
            elapsed.append(result["elapsed_seconds"])
        summary.append((search_type, found, chinese, failed, sum(elapsed) / len(elapsed) if elapsed else 0, cost))
    print("\n类型      结果  中文  失败  平均耗时(秒)  费用(美元)")
    for search_type, found, chinese, failed, seconds, cost in summary:
        print(f"{search_type:<9} {found:>4} {chinese:>5} {failed:>5} {seconds:>12.2f} {cost:>11.4f}")


if __name__ == "__main__":
    main()

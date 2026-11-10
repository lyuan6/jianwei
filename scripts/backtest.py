"""判断回测：把“今天”拨回事件结果公布之前，只用当时能取到的数据让研究智能体判断走向。不写数据库。

    .venv/bin/python -m scripts.backtest 输出路径 [案例 id ...]
"""
import json
import re
import sys
from datetime import datetime
from unittest.mock import patch

from src.config import Settings, load_env
from src.research import ask

TEMPLATE = "站在今天判断：{event}接下来最可能走向哪种结果——按计划完成、延期或变更、还是未完成或终止？给出你的判断、依据、最大的不确定点和接下来要观察的信号。不判断股价。"
CASES = [
    ("up-1", "sz002079", "苏州固锝", "2026-04-02", "控股股东苏州通博 6 个月内增持 5,000 万至 1 亿元的计划，", "按计划完成"),
    ("up-2", "sz300760", "迈瑞医疗", "2025-11-27", "董事长李西廷 6 个月内增持公司股份的计划，", "按计划完成"),
    ("up-3", "sz002714", "牧原股份", "2026-06-26", "董事和高管 6 个月内增持 4 亿至 5 亿元的计划，", "按计划完成"),
    ("up-4", "sz000725", "京东方Ａ", "2026-07-29", "控股股东北京电控增持 5 亿至 10 亿元的计划，", "按计划完成"),
    ("down-1", "sz000725", "京东方Ａ", "2026-01-06", "控股子公司京东方能源科技在北交所公开发行上市的申请，", "终止"),
    ("down-2", "sz002594", "比亚迪", "2025-11-28", "2025 年员工持股计划第一个解锁期（以 2024 年营收为基数，2025 年营收增长不低于 10%）的解锁条件，", "未完成"),
    ("down-3", "sz002475", "立讯精密", "2025-11-15", "收购闻泰科技消费电子业务资产包中尚未完成交割的部分（印度闻泰），", "终止"),
]


def run(case, settings):
    cid, code, name, day, event, actual = case
    moment = datetime.fromisoformat(day + "T20:00:00+08:00")
    question = TEMPLATE.format(event=f"{name}（{code}）" + event)
    with patch("src.market.now", return_value=moment), patch("src.research.now", return_value=moment):
        try:
            result, error = ask(question, settings, [{"code": code, "qty": "100", "cost": "1"}]), ""
        except Exception as exc:
            result, error = {}, repr(exc)
    refs = json.dumps(result.get("references", {}), ensure_ascii=False)
    # 泄漏检查：工具结果里的日期不应晚于提问日
    later = sorted({d for d in re.findall(r'"(?:date|publish_date|published|notice_date)":\s*"(\d{4}-\d{2}-\d{2})', refs) if d > day})
    return {"id": cid, "name": name, "code": code, "as_of": day, "event": event, "actual": actual, "question": question,
            "error": error, "later_dates": later, "title": result.get("title"),
            "paragraphs": [p["text"] for p in result.get("paragraphs", [])],
            "outlook": [{k: o[k] for k in ("text", "basis", "confidence")} for o in result.get("outlook", [])],
            "gaps": result.get("gaps"), "steps": [(s["tool"], s["summary"][:80], s["error"]) for s in result.get("steps", [])]}


def main():
    output, only = sys.argv[1], set(sys.argv[2:])
    load_env()
    settings = Settings.from_env()
    rows = []
    for case in CASES:
        if only and case[0] not in only:
            continue
        rows.append(run(case, settings))
        with open(output, "w", encoding="utf-8") as file:
            json.dump(rows, file, ensure_ascii=False, indent=1)
        print(case[0], case[2], case[3], rows[-1]["error"][:100] or "完成", "泄漏日期", rows[-1]["later_dates"], flush=True)


if __name__ == "__main__":
    main()

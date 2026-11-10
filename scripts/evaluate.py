"""评测：每个案例回放到事件当天运行见微，再让同一模型不带工具直接回答，对比级别和数字。

    .venv/bin/python -m scripts.evaluate [案例 id ...] [--output 输出路径]
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src import brief, model_client, prompts
from src.config import ROOT, Settings, load_env

CASES = ROOT / "tests" / "fixtures" / "eval_cases.json"
OUTPUT = ROOT / "output" / "evaluation.json"
KEYWORDS = {"解禁": ("解禁",), "减持": ("减持",), "业绩预告": ("预告", "预增", "预减", "首亏", "扭亏"),
            "财报": ("财报", "报告", "业绩"), "股价异动": ("异动", "异常波动"), "人事": ("辞职", "辞任", "离任", "人事")}
LEVELS = {"high": "重点", "mid": "留意", "low": "一般", None: "未提及"}


def task(case):
    return f"股票 {case['code']}，检查日期 {case['as_of']}。公告近 3 天、解禁前后 30 天、增减持/业绩预告/新财报近 7 天。\n" + prompts.BRIEF


def baseline(case, settings):
    result = model_client.complete_json([{"role": "system", "content": prompts.AGENT},
                                         {"role": "user", "content": task(case)}], settings)
    events = result.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise model_client.ModelError("输出缺少 events 数组。")
    return {"events": events}


def urls(value):
    if isinstance(value, dict):
        own = [item for key, item in value.items() if key in ("url", "pdf_url") and item]
        return own + [url for item in value.values() for url in urls(item)]
    if isinstance(value, list):
        return [url for item in value for url in urls(item)]
    return []


def supported(value, references):
    try:
        days = [date.fromisoformat(part) for part in re.split(r"\s*(?:至|到|~|～)\s*", value.strip())]
    except ValueError:
        return brief.verify_figure(value, references)
    # 公告正文里的日期常写成“2026 年 8 月 24 日”。
    text = re.sub(r"\s", "", json.dumps(references, ensure_ascii=False))
    return all(day.isoformat() in text or f"{day.year}年{day.month}月{day.day}日" in text for day in days)


def score(case, events, references):
    match = next((event for event in events if any(
        word in str(event.get("tag", "")) + str(event.get("title", "")) for word in KEYWORDS[case["type"]])), None)
    figures = [str(figure.get("value", "")) for event in events for figure in event.get("figures") or []
               if isinstance(figure, dict)]
    said = [match.get("title", ""), *(match.get("points") or []), match.get("risk", ""),
            *[str(figure.get("value", "")) for figure in match.get("figures") or []
              if isinstance(figure, dict)]] if match else []
    sources = list(dict.fromkeys(url for ref in (match.get("refs") or []) if ref in references
                                 for url in urls(references[ref]))) if match else []
    # 无来源数字：在见微本次取得的工具结果里找不到的数字，两组用同一份证据比较。
    return {"level": match.get("level") if match else None,
            "level_ok": bool(match) and match.get("level") == case["level"],
            "missing": [n for n in case["key_figures"] if not brief.verify_figure(n, said)],
            "unsupported": [value for value in figures if not supported(value, references)],
            "sources": sources}


def run(case, settings):
    # 把“今天”设成事件当天晚上，数据只取到那天为止。
    with patch("src.market.now", return_value=datetime.fromisoformat(case["as_of"] + "T20:00:00+08:00")):
        try:
            system = brief.analyze_stock(case["code"], settings)
        except ValueError as exc:
            system = {"events": [], "steps": [], "references": {}, "error": str(exc)}
    try:
        plain = baseline(case, settings)
    except ValueError as exc:
        plain = {"events": [], "error": str(exc)}
    references = system["references"]
    return {"case": case,
            "system": {**score(case, system["events"], references), "error": system.get("error"),
                       "steps": len(system["steps"]), "tool_errors": sum(step["error"] for step in system["steps"]),
                       "events": system["events"]},
            "baseline": {**score(case, plain["events"], references), "error": plain.get("error"), "events": plain["events"]},
            "references": references}


def summary(results, side):
    rows = [result[side] for result in results]
    figures = sum(len(result["case"]["key_figures"]) for result in results)
    return (f"级别一致 {sum(row['level_ok'] for row in rows)}/{len(rows)}，"
            f"关键数字提到 {figures - sum(len(row['missing']) for row in rows)}/{figures}，"
            f"无来源数字 {sum(len(row['unsupported']) for row in rows)} 个，失败 {sum(bool(row['error']) for row in rows)} 次")


def main(argv=None):
    parser = argparse.ArgumentParser(description="回放真实事件并对比带工具和不带工具的模型回答")
    parser.add_argument("cases", nargs="*", help="案例编号；省略时运行全部案例")
    parser.add_argument("--output", type=Path, help="结果 JSON 路径")
    args = parser.parse_args(argv)
    cases = json.loads(CASES.read_text())
    selected = set(args.cases)
    unknown = selected - {case["id"] for case in cases}
    if unknown:
        parser.error("案例编号不存在：" + "、".join(sorted(unknown)))
    cases = [case for case in cases if not selected or case["id"] in selected]
    output = args.output or (OUTPUT.with_name("evaluation-selected.json") if selected else OUTPUT)
    load_env()
    settings = Settings.from_env()
    if settings.mode != "api":
        sys.exit("需要配置 APP_MODE=api 和模型参数。")
    results = []
    for case in cases:
        result = run(case, settings)
        results.append(result)
        print(f"{case['id']} 标准 {LEVELS[case['level']]} | 见微 {LEVELS[result['system']['level']]} "
              f"缺 {result['system']['missing']} 无来源 {result['system']['unsupported']} | "
              f"直接问 {LEVELS[result['baseline']['level']]} 缺 {result['baseline']['missing']} "
              f"无来源 {result['baseline']['unsupported']}", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("见微：" + summary(results, "system"))
    print("直接问：" + summary(results, "baseline"))
    print(f"明细：{output}")


if __name__ == "__main__":
    main()

"""持仓研究问答，保留未核对数字和证据缺口。"""

from decimal import Decimal

from src import agent, commitments, prompts
from src.brief import TEXT_NUMBER, _numbers
from src.market import now
from src.tools import build_tools


def check_refs(refs, references):
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in references for ref in refs):
        raise agent.OutputError("研究引用必须是本次运行的 R 编号数组。")
    return refs


def _matches(written, number):
    """工具给四位小数，回答常写两位；“跌11.34%”不带符号，工具值是 -11.3401。写了符号就要一致，整数只认相等。"""
    value = Decimal(written.replace(",", ""))
    if written[0] not in "+-":
        number = abs(number)
    places = value.as_tuple().exponent
    return number == value or (places < 0 and abs(number - value) <= Decimal(5).scaleb(places - 1))


def number_checks(text, refs, references):
    available = set(_numbers([references[ref] for ref in refs]))
    values = dict.fromkeys(match[1] for match in TEXT_NUMBER.finditer(text))
    return [{"value": value, "verification": "已核对" if any(_matches(value, number) for number in available) else "未核对"}
            for value in values]


def validate_research(payload, references):
    if not isinstance(payload.get("title"), str) or not payload["title"].strip():
        raise agent.OutputError("研究结果缺少 title。")
    paragraphs = payload.get("paragraphs")
    if not isinstance(paragraphs, list):
        raise agent.OutputError("研究结果缺少 paragraphs 数组。")
    checked = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict) or not isinstance(paragraph.get("text"), str):
            raise agent.OutputError("每段研究结论需要 text。")
        refs = check_refs(paragraph.get("refs"), references)
        checked.append({"text": paragraph["text"], "refs": refs,
                        "numbers": number_checks(paragraph["text"], refs, references)})
    gaps = payload.get("gaps")
    if not isinstance(gaps, list) or not all(isinstance(gap, str) for gap in gaps):
        raise agent.OutputError("gaps 必须是文本数组。")
    table = payload.get("table")
    if not isinstance(table, dict):
        raise agent.OutputError("无表格时 table 为 {}，有表格时需要 caption、columns、rows。")
    checked_table = {}
    if table:
        columns, rows = table.get("columns"), table.get("rows")
        if (not isinstance(table.get("caption"), str) or not isinstance(columns, list) or not columns
                or not all(isinstance(column, str) for column in columns) or not isinstance(rows, list)):
            raise agent.OutputError("表格需要 caption 文本、columns 文本数组和 rows 数组。")
        checked_rows = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("cells"), list)
                    or len(row["cells"]) != len(columns) or not all(isinstance(cell, str) for cell in row["cells"])):
                raise agent.OutputError("表格每行需要与 columns 等长的 cells 文本数组。")
            refs = check_refs(row.get("refs"), references)
            checked_rows.append({"cells": row["cells"], "refs": refs,
                                 "numbers": number_checks(" ".join(row["cells"]), refs, references)})
        checked_table = {"caption": table["caption"], "columns": columns, "rows": checked_rows}
    outlook = payload.get("outlook", [])
    if not isinstance(outlook, list):
        raise agent.OutputError("outlook 必须是数组。")
    checked_outlook = []
    for item in outlook:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("basis"), str):
            raise agent.OutputError("每条预判需要 text 和 basis。")
        if item.get("confidence") not in ("高", "中", "低"):
            raise agent.OutputError("confidence 只能是高、中、低。")
        refs = check_refs(item.get("refs"), references)
        checked_outlook.append({"text": item["text"], "basis": item["basis"], "confidence": item["confidence"], "refs": refs,
                                "numbers": number_checks(item["text"] + " " + item["basis"], refs, references)})
    return {"title": payload["title"], "paragraphs": checked, "table": checked_table, "gaps": gaps, "outlook": checked_outlook}


def ledger_reader(store, holdings):
    """只给模型台账本身；工作底稿 steps 很长，也不是证据。"""
    def read(codes):
        today = now().date().isoformat()
        return [{"code": item["code"], "thread": item["thread_title"], "kind": item["kind"], "content": item["content"],
                 "amount": item["amount"], "deadline_text": item["deadline_text"], "deadline": item["deadline"],
                 "state": item["state"], "outcome": item["outcome"], "alert": item["alert"], "date": item["announced"],
                 "title": item["source_title"], "url": item["source_url"],
                 "evidence": [{"date": node["date"], "title": node["title"], "url": node["url"]} for node in item["evidence"]]}
                for item in commitments.ledger(store.commitments(codes or [holding["code"] for holding in holdings]), today)]
    return read


def ask(question, settings, holdings, *, on_step=None, store=None):
    # 排列序号和添加时间是界面用的，给模型只会被写进回答（曾出现“第 0 号标的”）。
    visible = [{key: value for key, value in holding.items() if key not in ("position", "added_at")} for holding in holdings]
    tools = build_tools(settings, holdings=visible, ledger=ledger_reader(store, holdings) if store else None)
    steps = []
    def record(step):
        steps.append(step)
        if on_step is not None:
            on_step(list(steps))
    task = prompts.RESEARCH + f"\n今天是 {now().date().isoformat()}。用户问题：" + question
    # 多家公司对比加上预判，2200 个 token 写不完，会被截断成失败。
    result = agent.run(task, [], tools, 8, settings=settings, workpaper=agent.Workpaper(record), validate=validate_research,
                       max_tokens=4500, timeout=150)
    return {**result["result"], "steps": result["steps"], "references": result["references"], "usage": result["usage"]}

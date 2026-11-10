"""承诺台账：从事项节点中抽出可以事后检验的承诺并跟踪兑现。

命令行：python -m src.commitments sz002079 …  为已有脉络补齐承诺；--stats 打印全部股票的统计。
"""

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal

from src import agent, market, prompts
from src.brief import TEXT_NUMBER, _numbers
from src.config import Settings, load_env
from src.timeline import _tools, unverified

ALERT_DAYS = 30
RECENT_DAYS = 14


def _text(value, field, required=False):
    if not isinstance(value, str) or required and not value.strip():
        raise agent.OutputError(f"{field} 须为{'非空' if required else ''}文本。")
    return value.strip()


def validator(ids):
    def validate(payload, references):
        items = payload.get("commitments")
        if not isinstance(items, list):
            raise agent.OutputError("缺少 commitments 数组。")
        checked = []
        for item in items:
            if not isinstance(item, dict) or item.get("kind") not in prompts.COMMITMENT_KINDS:
                raise agent.OutputError("kind 只能是：" + "、".join(prompts.COMMITMENT_KINDS) + "。")
            if item.get("status") not in prompts.COMMITMENT_STATUSES:
                raise agent.OutputError("status 只能是：" + "、".join(prompts.COMMITMENT_STATUSES) + "。")
            if item.get("node") not in ids:
                raise agent.OutputError("node 必须是输入里的节点编号。")
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list) or any(node not in ids for node in evidence):
                raise agent.OutputError("evidence 只能使用输入里的节点编号。")
            deadline = _text(item.get("deadline", ""), "deadline")
            if deadline:
                try:
                    deadline = date.fromisoformat(deadline).isoformat()
                except ValueError:
                    raise agent.OutputError("deadline 须为 YYYY-MM-DD 或空。") from None
            checked.append({"kind": item["kind"], "content": _text(item.get("content"), "content", True),
                            "amount": _text(item.get("amount", ""), "amount"), "deadline_text": _text(item.get("deadline_text", ""), "deadline_text"),
                            "deadline": deadline, "node": item["node"], "status": item["status"],
                            "outcome": _text(item.get("outcome", ""), "outcome"), "evidence": list(dict.fromkeys(evidence))})
        return {"commitments": checked}
    return validate


def _known_numbers(nodes):
    # 节点摘要里已经核对过的数字也算来源；之前标为未核对的不算。
    found = set(_numbers([{"title": node["title"]} for node in nodes]))
    for node in nodes:
        skip = {Decimal(value.replace(",", "")) for value in node["unverified"]}
        found |= {Decimal(match[1].replace(",", "")) for match in TEXT_NUMBER.finditer(node["summary"] + " " + node["analysis"])} - skip
    return found


def review(code, thread, settings, store, *, on_step=None):
    """重新审阅一个事项的全部节点，整体替换它的承诺。单个事项失败只记录错误。"""
    ids = {f"N{node['id']}": node for node in thread["nodes"]}
    nodes = [{"id": key, "date": node["date"], "kind": node["kind"], "title": node["title"], "summary": node["summary"],
              "analysis": node["analysis"], "art_code": node["art_code"]} for key, node in ids.items()]
    paper = agent.Workpaper(on_step)
    context = f"股票 {code}，今天 {market.now().date().isoformat()}。事项：{thread['title']}（{thread['status']}）\n{thread['summary']}\n"
    try:
        run = agent.run(prompts.COMMITMENT, [{"role": "user", "content": context + json.dumps(nodes, ensure_ascii=False)}],
                        _tools(settings, ("read_announcement",)), 3, settings=settings, workpaper=paper, validate=validator(set(ids)))
        items, error = run["result"]["commitments"], ""
    except ValueError as exc:
        items, error = [], str(exc)
    available = set(_numbers(paper.references)) | _known_numbers(thread["nodes"])
    commitments = [{**item, "node_id": ids[item["node"]]["id"], "evidence": [ids[node]["id"] for node in item["evidence"]],
                    "unverified": unverified(" ".join((item["content"], item["amount"], item["outcome"])), available)}
                   for item in items]
    store.save_commitments(thread["id"], code, commitments, paper.steps, error)
    return commitments


def review_stock(code, settings, store, *, only_new=False, on_step=None):
    done = store.reviewed_threads(code) if only_new else set()
    threads = [thread for thread in store.threads(code) if thread["id"] not in done]
    steps = []
    for thread in threads:
        def record(step, title=thread["title"]):
            steps.append({"code": code, "thread": title, **step})
            if on_step is not None:
                on_step(steps)
        review(code, thread, settings, store, on_step=record)
    return {"threads": len(threads), "steps": steps}


def ledger(items, today):
    """给每条承诺加上展示状态和提醒；到期未披露由程序按期限判断，不交给模型。"""
    result = []
    for item in items:
        state = item["status"]
        if state == "进行中" and item["deadline"] and item["deadline"] < today:
            state = "到期未披露"
        alert = ""
        if state == "到期未披露":
            alert = f"已于 {item['deadline']} 到期，未见结果公告"
        elif state == "进行中" and item["deadline"]:
            left = (date.fromisoformat(item["deadline"]) - date.fromisoformat(today)).days
            if left <= ALERT_DAYS:
                alert = f"{left} 天后到期"
        elif state in ("延期", "变更", "未兑现", "终止"):
            latest = max([node["date"] for node in item["evidence"]] or [item["announced"]])
            if latest >= (date.fromisoformat(today) - timedelta(days=RECENT_DAYS)).isoformat():
                alert = f"{latest} {state}"
        result.append({**item, "state": state, "alert": alert})
    return result


def stats(items):
    counts = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return counts


def main(argv=None):
    from src.accounts import default_store
    from src.storage import Storage
    parser = argparse.ArgumentParser(description="为已有脉络补齐承诺台账，或打印统计")
    parser.add_argument("codes", nargs="*", help="带交易所的股票代码；不填则处理所有已有脉络的股票")
    parser.add_argument("--stats", action="store_true", help="只打印统计")
    args = parser.parse_args(argv)
    load_env()
    store = Storage(default_store().path)
    codes = args.codes or store.timeline_codes()
    if not args.stats:
        settings = Settings.from_env()
        for code in codes:
            result = review_stock(code, settings, store, only_new=True)
            print(f"{code}：审阅 {result['threads']} 个事项，{len(result['steps'])} 步底稿")
    items = ledger(store.commitments(codes), market.now().date().isoformat())
    print(f"{len(codes)} 只股票，{len(items)} 条承诺：" + "，".join(f"{k} {v}" for k, v in sorted(stats(items).items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()

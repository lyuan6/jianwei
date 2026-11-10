"""事件脉络：一年公告分组成事项、晨报事件归入事项、用户发起的关联分析。命令行：python -m src.timeline sz002079。"""

import argparse
import fcntl
import json
import re
from datetime import date, timedelta
from decimal import Decimal

from src import agent, events, market, news, prompts
from src.brief import TEXT_NUMBER, _numbers
from src.config import Settings, load_env
from src.tools import build_tools

# 附属文件只是主公告的配套材料，单独成节点会把脉络拉得很碎。
SKIP = {"法律意见书", "保荐/核查意见", "专项说明/独立意见", "调研活动", "管理办法/制度", "议事规则/实施细则",
        "独立董事述职报告", "独立董事候选人声明", "独立董事提名人声明", "公司章程", "公司章程修订", "内部控制报告",
        "股东大会资料", "ESG公告", "审计报告", "股权激励对象名单", "召开股东大会通知", "跟踪评级公告", "自查报告",
        "年度报告摘要", "半年度报告摘要", "年度报告全文(英文)"}
SKIP_TITLES = ("翌日披露报表", "英文版", "摘要", "保荐书", "法律意见书", "申报稿", "注册稿", "上会稿", "审计报告", "自查表", "核查意见")
MUST = ("限售股份上市流通", "减持", "增持", "回购", "股权激励", "员工持股", "投资", "增资", "签订协议", "合同",
        "重组", "收购", "出售", "诉讼", "仲裁", "处罚", "立案", "业绩预告", "业绩快报", "募集资金", "权益变动",
        "向特定对象发行", "报告全文")
STATUSES = ("进行中", "已完成", "未兑现或变更", "终止")
VERDICTS = ("相关", "可能相关", "无明显关联")
MAX_THREADS = 8
THREAD_TOOLS = ("read_announcement", "search_news", "get_price_history")
LINK_TOOLS = ("list_announcements", "read_announcement", "search_news", "get_price_history")
DATE_TEXT = re.compile(r"\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日|\d{4}-\d{2}-\d{2}")
SCALES = (Decimal(1), Decimal(10_000), Decimal(100_000_000))


def short_title(title):
    # 东方财富标题前缀是公司简称，模型输入里去掉以节省篇幅。
    return title.split(":", 1)[-1]


def candidates(announcements):
    result = []
    for item in sorted(announcements, key=lambda x: x["date"]):
        categories = item["categories"] or ["其他"]
        if all(c in SKIP for c in categories) or any(t in item["title"] for t in SKIP_TITLES):
            continue
        text = "".join(categories) + short_title(item["title"])
        result.append({**item, "id": f"A{len(result) + 1}", "must": any(k in text for k in MUST)})
    return result


def unverified(text, available):
    values = dict.fromkeys(match[1] for match in TEXT_NUMBER.finditer(DATE_TEXT.sub("", text)))
    return [value for value in values
            if not {Decimal(value.replace(",", "")) * scale for scale in SCALES} & available]


def _tools(settings, names):
    tools = build_tools(settings)
    return {name: tools[name] for name in names}


def _text(value, field):
    if not isinstance(value, str):
        raise agent.OutputError(f"{field} 须为文本。")
    return value.strip()


def group_validator(ids):
    def validate(payload, references):
        threads = payload.get("threads")
        if not isinstance(threads, list):
            raise agent.OutputError("缺少 threads 数组。")
        used, checked = set(), []
        for thread in threads[:MAX_THREADS]:
            if not isinstance(thread, dict) or not _text(thread.get("title"), "title"):
                raise agent.OutputError("每个事项需要 title。")
            if thread.get("theme") not in prompts.THEMES:
                raise agent.OutputError("theme 不在可选主题中。")
            nodes, major = thread.get("nodes"), thread.get("major", [])
            if not isinstance(nodes, list) or not nodes or any(node not in ids for node in nodes):
                raise agent.OutputError("nodes 必须是输入里的公告编号。")
            if not isinstance(major, list) or any(node not in nodes for node in major):
                raise agent.OutputError("major 必须是该事项 nodes 中的编号。")
            nodes = [node for node in dict.fromkeys(nodes) if node not in used]
            used.update(nodes)
            if nodes:
                checked.append({"title": thread["title"].strip()[:40], "theme": thread["theme"], "nodes": nodes,
                                "major": [node for node in major if node in nodes]})
        return {"threads": checked}
    return validate


def thread_validator(ids):
    def validate(payload, references):
        if payload.get("status") not in STATUSES:
            raise agent.OutputError("status 只能是：" + "、".join(STATUSES) + "。")
        summary = _text(payload.get("summary"), "summary")
        nodes = payload.get("nodes", [])
        if not isinstance(nodes, list) or any(not isinstance(node, dict) or node.get("id") not in ids for node in nodes):
            raise agent.OutputError("nodes 只能使用输入里的编号。")
        checked_nodes = {node["id"]: {"summary": _text(node.get("summary", ""), "summary"),
                                      "analysis": _text(node.get("analysis", ""), "analysis")} for node in nodes}
        news = payload.get("news", [])
        if not isinstance(news, list):
            raise agent.OutputError("news 须为数组。")
        checked_news = []
        for item in news:
            result = references.get(item.get("ref")) if isinstance(item, dict) else None
            rows = result.get("results", []) if isinstance(result, dict) else []
            found = next((row for row in rows if row["url"] == item.get("url")), None)
            if found is None:
                raise agent.OutputError("news 的 ref 和 url 必须来自同一条 search_news 结果。")
            checked_news.append({**found, "analysis": _text(item.get("analysis", ""), "analysis")})
        return {"summary": summary, "status": payload["status"], "nodes": checked_nodes, "news": checked_news}
    return validate


def _news_date(row, fallback):
    try:
        return date.fromisoformat(row["date"]).isoformat()
    except ValueError:
        return fallback


def initialize(code, settings, store, *, on_step=None):
    code = market.resolve_symbol(code)
    if market.is_index(code):
        raise ValueError("指数没有事件脉络，请选择个股。")
    lock = store.path.parent / f"timeline-{code}.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if store.timeline_stock(code) is not None:
            return {"code": code, "threads": None, "steps": [], "usage": []}
        return _initialize(code, settings, store, on_step)


def _initialize(code, settings, store, on_step):
    today = market.now().date()
    since, until = (today - timedelta(days=365)).isoformat(), today.isoformat()
    announcements = events.fetch_announcement_history(code)
    items = candidates(announcements)
    ids = {item["id"]: item for item in items}
    steps, usage, groups = [], [], []
    if items:
        lines = "\n".join(f"{item['id']} {item['date']} [{'/'.join(item['categories']) or '其他'}] {short_title(item['title'])}"
                          + (" 必须" if item["must"] else "") for item in items)
        grouped = agent.complete([{"role": "user", "content": prompts.TIMELINE_GROUP + f"\n股票 {code}，{since} 至 {until}：\n" + lines}],
                                 settings, group_validator(ids), max_tokens=4000)
        usage += grouped["usage"]
        groups = grouped["result"]["threads"]
    tools = _tools(settings, THREAD_TOOLS)
    threads = []
    for group in groups:
        def record(step, title=group["title"]):
            steps.append({"code": code, "thread": title, **step})
            if on_step is not None:
                on_step(steps)
        nodes = [ids[node] for node in group["nodes"]]
        brief = [{"id": node["id"], "date": node["date"], "title": short_title(node["title"]), "art_code": node["art_code"],
                  "major": node["id"] in group["major"]} for node in nodes]
        paper = agent.Workpaper(record)
        # 单个事项分析失败只影响这一条，分组结果照常保存。
        try:
            run = agent.run(prompts.TIMELINE_THREAD, [{"role": "user", "content": f"股票 {code}，事项：{group['title']}（{group['theme']}）\n"
                                                       + json.dumps(brief, ensure_ascii=False)}],
                            tools, 4, settings=settings, workpaper=paper, validate=thread_validator(set(group["nodes"])))
            analysis, error = run["result"], ""
            usage += run["usage"]
        except ValueError as exc:
            analysis, error = {"summary": "", "status": "进行中", "nodes": {}, "news": []}, str(exc)
        available = set(_numbers([paper.references, brief]))
        thread_nodes = []
        for node in nodes:
            written = analysis["nodes"].get(node["id"], {"summary": "", "analysis": ""})
            thread_nodes.append({"date": node["date"], "kind": "公告", "major": node["id"] in group["major"],
                                 "title": short_title(node["title"]), **written, "url": node["url"], "art_code": node["art_code"],
                                 "unverified": unverified(written["summary"] + " " + written["analysis"], available)})
        for row in analysis["news"]:
            thread_nodes.append({"date": _news_date(row, nodes[-1]["date"]), "kind": "新闻", "major": False,
                                 "title": row["title"] or row["domain"], "summary": "", "analysis": row["analysis"],
                                 "url": row["url"], "art_code": None, "unverified": unverified(row["analysis"], available)})
        threads.append({"title": group["title"], "theme": group["theme"], "status": analysis["status"],
                        "summary": analysis["summary"], "error": error, "steps": paper.steps,
                        "unverified": unverified(analysis["summary"], available), "nodes": thread_nodes})
    store.save_timeline(code, since, until, announcements, threads)
    from src import commitments
    def record(more):
        if on_step is not None:
            on_step(steps + more)
    steps += commitments.review_stock(code, settings, store, on_step=record)["steps"]
    # 新闻旁枝只是补充，检索不可用时脉络照常完成。
    try:
        branch_stock(code, settings, store)
    except ValueError as exc:
        print(f"{code} 新闻旁枝未补充：{exc}")
    from src import chain
    # 上下游同样是补充，失败不影响已经保存的脉络。
    try:
        chain.build(code, settings, store)
    except ValueError as exc:
        print(f"{code} 上下游未整理：{exc}")
    return {"code": code, "threads": len(threads), "steps": steps, "usage": usage}


def branch_validator(ids):
    def validate(payload, references):
        items = payload.get("news")
        if not isinstance(items, list) or len(items) > 2:
            raise agent.OutputError("news 须为最多 2 条的数组。")
        if any(not isinstance(item, dict) or item.get("id") not in ids for item in items):
            raise agent.OutputError("id 必须是输入里的新闻编号。")
        return {"news": [{"id": item["id"], "summary": _text(item.get("summary", ""), "summary"),
                          "analysis": _text(item.get("analysis", ""), "analysis")} for item in items]}
    return validate


def branch(code, company, thread, settings):
    """为事项检索它发生期间的第三方报道，由模型挑出最多两条有代表性的作为旁枝。返回新节点。"""
    nodes = official(thread)
    since = (date.fromisoformat(nodes[0]["date"]) - timedelta(days=7)).isoformat()
    found = news.search_news(f"{company} {thread['title']}", settings.exa_api_key, num=8, max_chars=500, since=since)["results"]
    known = {node["url"] for node in thread["nodes"]}
    rows = {f"N{i + 1}": row for i, row in enumerate(row for row in found if row["url"] not in known)}
    if not rows:
        return []
    payload = {"thread": {"title": thread["title"], "status": thread["status"], "summary": thread["summary"],
                          "nodes": [f"{node['date']} {node['title']}" for node in nodes]},
               "news": [{"id": key, "title": row["title"], "date": row["date"], "domain": row["domain"], "text": row["text"]}
                        for key, row in rows.items()]}
    picked = agent.complete([{"role": "user", "content": prompts.NEWS_BRANCH + "\n" + json.dumps(payload, ensure_ascii=False)}],
                            settings, branch_validator(set(rows)))["result"]["news"]
    result = []
    for item in picked:
        row = rows[item["id"]]
        available = set(_numbers([row]))
        result.append({"date": _news_date(row, nodes[-1]["date"]), "kind": "新闻", "major": False,
                       "title": row["title"] or row["domain"], "summary": item["summary"], "analysis": item["analysis"],
                       "url": row["url"], "art_code": None,
                       "unverified": unverified(item["summary"] + " " + item["analysis"], available)})
    return result


def official(thread):
    return [node for node in thread["nodes"] if node["kind"] == "公告"]


def branch_stock(code, settings, store):
    company = market.fetch_quotes([code])[code].name
    counts = []
    for thread in store.threads(code):
        if not official(thread):
            continue
        # 单个事项检索或挑选失败只跳过这一条。
        try:
            found = branch(code, company, thread, settings)
        except ValueError as exc:
            print(f"{code} {thread['title']}：{exc}")
            continue
        store.add_branches(thread["id"], found)
        counts.append(len(found))
    return counts


def attach_validator(thread_ids, count):
    def validate(payload, references):
        items = payload.get("items")
        if not isinstance(items, list):
            raise agent.OutputError("缺少 items 数组。")
        checked = []
        for item in items:
            if not isinstance(item, dict) or type(item.get("event")) is not int or not 0 <= item["event"] < count:
                raise agent.OutputError("event 必须是输入事件的序号。")
            thread = item.get("thread")
            if thread is not None and thread not in thread_ids:
                raise agent.OutputError("thread 必须是已有事项 id 或 null。")
            if thread is None and (not _text(item.get("title"), "title") or item.get("theme") not in prompts.THEMES):
                raise agent.OutputError("新事项需要 title 和可选范围内的 theme。")
            if item.get("status") not in STATUSES or not isinstance(item.get("major"), bool):
                raise agent.OutputError("每项需要 status 和布尔值 major。")
            checked.append({"event": item["event"], "thread": thread, "title": (item.get("title") or "").strip()[:40],
                            "theme": item.get("theme") or "其他", "status": item["status"], "major": item["major"]})
        return {"items": checked}
    return validate


def event_source(event, references):
    """事件引用的原始记录；列表结果优先取与事件同一天的那条。"""
    rows = []
    for ref in event["refs"]:
        result = references.get(ref)
        found = result if isinstance(result, list) else result.get("results", [result]) if isinstance(result, dict) else []
        rows += [row for row in found if isinstance(row, dict) and str(row.get("url", "")).startswith("https://")]
    row = next((row for row in rows if row.get("date") == event["date"]), rows[0] if rows else None)
    if row is None:
        return None
    official = "eastmoney.com" in row["url"]
    return {"kind": "公告" if official else "新闻", "url": row["url"], "art_code": row.get("art_code")}


def attach(code, stock, settings, store):
    """晨报分析完一只股票后，把新事件归入已有事项。"""
    timeline = store.timeline_stock(code)
    if timeline is None or not stock["events"]:
        return []
    threads = store.threads(code)
    known = {node["url"] for thread in threads for node in thread["nodes"]}
    fresh = []
    for event in stock["events"]:
        source = event_source(event, stock["references"])
        # 初始化已覆盖到 until 当天；回放旧日期或多个组合重复分析时不重复收录。
        if source and event["date"] > timeline["until"] and source["url"] not in known:
            fresh.append((event, source))
            known.add(source["url"])
    if not fresh:
        return []
    payload = {"threads": [{"id": thread["id"], "title": thread["title"], "theme": thread["theme"], "status": thread["status"],
                            "recent": [f"{node['date']} {node['title']}" for node in thread["nodes"][-3:]]} for thread in threads],
               "events": [{"event": i, "date": event["date"], "tag": event["tag"], "title": event["title"]}
                          for i, (event, _) in enumerate(fresh)]}
    result = agent.complete([{"role": "user", "content": prompts.TIMELINE_ATTACH + "\n" + json.dumps(payload, ensure_ascii=False)}],
                            settings, attach_validator({thread["id"] for thread in threads}, len(fresh)))
    available = set(_numbers(stock["references"]))
    items = []
    for item in result["result"]["items"]:
        event, source = fresh[item["event"]]
        analysis = "；".join(event["points"][1:]) + (f" 风险：{event['risk']}" if event["risk"] else "")
        summary = event["points"][0] if event["points"] else ""
        items.append({**item, "node": {"date": event["date"], "major": item["major"], "title": event["title"],
                                       "summary": summary, "analysis": analysis, **source,
                                       "unverified": unverified(summary + " " + analysis, available)}})
    from src import commitments
    touched = store.attach_nodes(code, items)
    # 新节点可能让承诺兑现、延期或变更，重新审阅涉及的事项。
    for thread in store.threads(code):
        if thread["id"] in touched:
            commitments.review(code, thread, settings, store)
    return items


def link_validator(payload, references):
    if payload.get("verdict") not in VERDICTS:
        raise agent.OutputError("verdict 只能是：" + "、".join(VERDICTS) + "。")
    refs = payload.get("refs", [])
    if not isinstance(refs, list) or any(ref not in references for ref in refs):
        raise agent.OutputError("refs 只能使用本次运行的 R 编号。")
    return {"verdict": payload["verdict"], "explanation": _text(payload.get("explanation"), "explanation"), "refs": refs}


def analyze_link(username, node_a, node_b, settings, store, *, on_step=None):
    nodes = [store.node(node_a, username), store.node(node_b, username)]
    context = []
    for node in nodes:
        thread = next(t for t in store.threads(node["code"], username) if t["id"] == node["thread_id"])
        context.append({"code": node["code"], "node": {key: node[key] for key in ("date", "kind", "title", "summary", "analysis", "art_code")},
                        "thread": {"title": thread["title"], "status": thread["status"], "summary": thread["summary"],
                                   "nodes": [f"{n['date']} {n['kind']} {n['title']}" for n in thread["nodes"]]}})
    steps = []
    def record(step):
        steps.append(step)
        if on_step is not None:
            on_step(list(steps))
    run = agent.run(prompts.TIMELINE_LINK, [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                    _tools(settings, LINK_TOOLS), 4, settings=settings, workpaper=agent.Workpaper(record), validate=link_validator)
    result = {**run["result"], "references": {ref: run["references"][ref] for ref in run["result"]["refs"]}, "steps": run["steps"]}
    link_id = store.save_link(username, node_a, node_b, result)
    return {"id": link_id, "node_a": node_a, "node_b": node_b, **result, "usage": run["usage"]}


def reanalyze(thread_id, settings, store):
    """重写一个事项的总结和节点分析，节点本身不变。公告多的事项工具调用次数放宽到够读。"""
    code = store.thread_code(thread_id)
    thread = next(t for t in store.threads(code) if t["id"] == thread_id)
    nodes = {f"A{node['id']}": node for node in official(thread) if not node["added_by"]}
    brief = [{"id": key, "date": node["date"], "title": node["title"], "art_code": node["art_code"], "major": node["major"]}
             for key, node in nodes.items()]
    paper = agent.Workpaper(lambda step: None)
    run = agent.run(prompts.TIMELINE_THREAD, [{"role": "user", "content": f"股票 {code}，事项：{thread['title']}（{thread['theme']}）\n"
                                               + json.dumps(brief, ensure_ascii=False)}],
                    _tools(settings, THREAD_TOOLS), min(16, len(nodes) + 2), settings=settings, workpaper=paper,
                    validate=thread_validator(set(nodes)))["result"]
    available = set(_numbers([paper.references, brief]))
    written = {nodes[key]["id"]: {**text, "unverified": unverified(text["summary"] + " " + text["analysis"], available)}
               for key, text in run["nodes"].items()}
    store.update_thread_analysis(thread_id, run["status"], run["summary"], unverified(run["summary"], available), paper.steps, written)
    return len(written)


def main(argv=None):
    from src.accounts import default_store
    from src.storage import Storage
    parser = argparse.ArgumentParser(description="为股票初始化过去 12 个月的事件脉络")
    parser.add_argument("codes", nargs="*", help="带交易所的股票代码，如 sz002079；--branches 时不填表示全部")
    parser.add_argument("--branches", action="store_true", help="为已有脉络补充新闻旁枝")
    parser.add_argument("--reanalyze", type=int, nargs="+", default=[], metavar="ID", help="重写这些事项的总结和节点分析")
    args = parser.parse_args(argv)
    load_env()
    store, settings = Storage(default_store().path), Settings.from_env()
    for thread_id in args.reanalyze:
        print(f"事项 {thread_id}：重写 {reanalyze(thread_id, settings, store)} 个节点")
    if args.reanalyze:
        return
    if args.branches:
        for code in args.codes or store.timeline_codes():
            counts = branch_stock(code, settings, store)
            print(f"{code}：{len(counts)} 个事项，新增旁枝 {sum(counts)} 条")
        return
    for code in args.codes:
        result = initialize(code, settings, store)
        print(f"{code}：" + ("已有脉络，跳过" if result["threads"] is None else f"{result['threads']} 个事项，{len(result['steps'])} 步底稿"))


if __name__ == "__main__":
    main()

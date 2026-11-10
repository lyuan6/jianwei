"""产业联动图：把持仓的上下游关系聚合到二级行业。

命令行：python -m src.industry  为所有已整理上下游的股票和其中的公司补齐所属板块。
"""

import argparse
import json
from collections import defaultdict
from datetime import timedelta

from src import agent, events, market, news, prompts
from src.brief import _numbers
from src.config import Settings, load_env
from src.timeline import _text, unverified

# 产业图只放重点：每个行业最多两条、近两周的。
MAX_ITEMS, ITEM_DAYS = 2, 14


def resolve(name):
    """新闻里只有名字的客户：证券搜索按全名匹配 A 股代码，匹配不上就不进图。"""
    try:
        found = market.search_symbols(name)
    except ValueError:
        return ""
    return next((symbol[2:] for symbol, label in found if label == name and symbol[:2] in ("sh", "sz", "bj")), "")


def refresh(store, codes=None):
    """补齐还没有板块记录的公司；单家失败只跳过。返回新增数。"""
    known = store.company_boards()
    names = {b["name"] for b in known.values()}
    targets = {}
    for code in codes or store.chain_codes():
        chain = store.chain(code)
        if chain is None:
            continue
        targets[code[2:]] = chain["name"]
        for link in chain["links"]:
            if link["sec_code"]:
                targets[link["sec_code"]] = link["name"]
            elif link["name"] not in names:
                sec = resolve(link["name"])
                if sec:
                    targets[sec] = link["name"]
    added = 0
    for sec, name in targets.items():
        if sec in known:
            continue
        try:
            boards = events.fetch_boards(sec)
        except ValueError:
            continue
        store.save_company_board(sec, name, boards)
        added += 1
    return added


def layers(names, edges):
    """上游在左：按最长路径分层；环里的回边不参与分层。"""
    out = defaultdict(set)
    for a, b in edges:
        out[a].add(b)
    state, order, keep = {}, [], defaultdict(set)

    def visit(n):
        state[n] = 1
        for m in sorted(out[n]):
            if state.get(m) == 1:
                continue
            keep[n].add(m)
            if m not in state:
                visit(m)
        state[n] = 2
        order.append(n)

    for n in sorted(names):
        if n not in state:
            visit(n)
    level = dict.fromkeys(names, 0)
    for n in reversed(order):
        for m in keep[n]:
            level[m] = max(level[m], level[n] + 1)
    return level


def graph(store, codes):
    boards = store.company_boards()
    by_name = {b["name"]: sec for sec, b in boards.items()}
    nodes, edges = {}, defaultdict(list)

    def node(industry):
        return nodes.setdefault(industry["name"], {"name": industry["name"], "code": industry["code"], "holdings": [],
                                                   "companies": set(), "inner": []})


    for code in codes:
        own = boards.get(code[2:])
        if not own or not own["industry"]:
            continue
        chain = store.chain(code)
        holding = {"code": code, "name": chain["name"] if chain else own["name"]}
        home = node(own["industry"])
        home["holdings"].append(holding)
        for link in chain["links"] if chain else []:
            sec = link["sec_code"] or by_name.get(link["name"], "")
            other = boards.get(sec, {}).get("industry")
            if not other:
                continue
            node(other)["companies"].add(link["name"])
            evidence = {"company": link["name"], "holding": holding["name"], "code": code, "side": link["side"],
                        "kind": link["category"] or link["segment"], "date": link["date"], "quote": link["quote"], "url": link["url"]}
            # 同一行业里的往来不画边，记在节点上。
            if other["name"] == home["name"]:
                home["inner"].append(evidence)
                continue
            pair = (other["name"], home["name"]) if link["side"] == "上游" else (home["name"], other["name"])
            edges[pair].append(evidence)
    level = layers(list(nodes), list(edges))
    highlights(store, codes, nodes, edges)
    return {"nodes": [{**n, "companies": sorted(n["companies"]), "layer": level[n["name"]]} for n in nodes.values()],
            "edges": [{"from": a, "to": b, "evidence": ev} for (a, b), ev in edges.items()]}


def highlights(store, codes, nodes, edges):
    """给每个行业挑近两周的重点，并算出它直接或隔一层能连到哪些持仓（暴露）。"""
    since = (market.now().date() - timedelta(days=ITEM_DAYS)).isoformat()
    boards = store.company_boards()
    by_name = {b["name"]: sec for sec, b in boards.items()}
    home = {h["code"]: n["name"] for n in nodes.values() for h in n["holdings"]}
    near = defaultdict(set)
    for a, b in edges:
        near[a].add(b)
        near[b].add(a)
    for n in nodes.values():
        n["reach"] = sorted({h["code"] for m in {n["name"], *near[n["name"]]} for h in nodes[m]["holdings"]})
        n["items"] = []
    for code in codes:
        chain = store.chain(code)
        for link in chain["links"] if chain else []:
            sec = link["sec_code"] or by_name.get(link["name"], "")
            industry = (boards.get(sec, {}).get("industry") or {}).get("name")
            if industry in nodes and link["date"] >= since:
                nodes[industry]["items"].append({"kind": "站点", "date": link["date"], "title": f"{link['name']}：{link['note']}",
                                                 "signal": link["signal"], "url": link["url"], "code": code, "chain": link["id"]})
        if code in home:
            for thread in store.threads(code):
                for node in thread["nodes"]:
                    if node["major"] and node["kind"] == "公告" and node["date"] >= since:
                        nodes[home[code]]["items"].append({"kind": "事项", "date": node["date"], "title": f"{thread['title']}：{node['title']}",
                                                           "signal": "", "url": node["url"], "code": code, "node": node["id"]})
    for item in store.industry_news(since):
        if item["industry"] in nodes:
            nodes[item["industry"]]["items"].append({"kind": "行业新闻", "date": item["date"], "title": item["summary"] or item["title"],
                                                      "signal": "", "url": item["url"], "source": item["domain"], "unverified": item["unverified"]})
    # 有价格或毛利信号的站点最先，其次行业新闻、持仓的阶段节点、其他站点；同级按日期新旧。
    rank = lambda i: (3 if i["signal"] else 2 if i["kind"] == "行业新闻" else 1 if i["kind"] == "事项" else 0, i["date"])
    for n in nodes.values():
        n["items"] = sorted(n["items"], key=rank, reverse=True)[:MAX_ITEMS]


def news_validator(ids):
    def validate(payload, references):
        items = payload.get("news")
        if not isinstance(items, list) or len(items) > 1:
            raise agent.OutputError("news 须为最多 1 条的数组。")
        if any(not isinstance(i, dict) or i.get("id") not in ids for i in items):
            raise agent.OutputError("id 必须是输入里的新闻编号。")
        return {"news": [{"id": i["id"], "summary": _text(i.get("summary", ""), "summary")} for i in items]}
    return validate


def holding_industries(store):
    industries = set()
    for username, disabled, *_ in store.list():
        if not disabled:
            industries |= {n["name"] for n in graph(store, [h["code"] for h in store.holdings(username)])["nodes"]}
    return industries


def collect_news(store, settings):
    """定时任务：持仓涉及的每个行业检索一次近两天的新闻，模型只挑有实质变化的一条。返回新增条数。"""
    industries = holding_industries(store)
    keep = (market.now().date() - timedelta(days=ITEM_DAYS)).isoformat()
    picked = []
    for name in sorted(industries):
        # 单个行业检索或挑选失败只跳过，第二天还会再看。
        try:
            found = news.search_news(f"{name} 行业 价格 政策 产能 需求", settings.exa_api_key, days=2, num=6, max_chars=400)["results"]
            rows = {f"N{i + 1}": row for i, row in enumerate(found)}
            if not rows:
                continue
            payload = {"industry": name, "news": [{"id": k, "title": r["title"], "date": r["date"], "domain": r["domain"], "text": r["text"]}
                                                  for k, r in rows.items()]}
            chosen = agent.complete([{"role": "user", "content": prompts.INDUSTRY_NEWS + "\n" + json.dumps(payload, ensure_ascii=False)}],
                                    settings, news_validator(set(rows)))["result"]["news"]
        except ValueError as exc:
            print(f"{name} 行业新闻未整理：{exc}")
            continue
        for item in chosen:
            row = rows[item["id"]]
            picked.append({"industry": name, "date": row["date"] or market.now().date().isoformat(), "title": row["title"] or row["domain"],
                           "summary": item["summary"], "url": row["url"], "domain": row["domain"],
                           "unverified": unverified(item["summary"], set(_numbers([row])))})
    store.add_industry_news(picked, keep)
    return len(picked)


def main(argv=None):
    from src.accounts import default_store
    from src.storage import Storage
    parser = argparse.ArgumentParser(description="为已整理上下游的股票和其中的公司补齐所属板块")
    parser.add_argument("--news", action="store_true", help="同时为持仓涉及的行业收集今天的重点新闻")
    args = parser.parse_args(argv)
    load_env()
    store = Storage(default_store().path)
    print(f"新增 {refresh(store)} 家公司的板块。")
    if args.news:
        print(f"新增 {collect_news(store, Settings.from_env())} 条行业新闻。")


if __name__ == "__main__":
    main()

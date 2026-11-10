"""上下游：其他上市公司公告里提到本公司的段落（供应商）和新闻里的签约、订单（客户）。

命令行：python -m src.chain sz300750 …  不填代码时为所有已有脉络、还没有上下游的股票整理；--force 重新整理。
"""

import argparse
import json
from datetime import date, timedelta

from src import agent, events, industry, market, news, prompts
from src.brief import _numbers
from src.config import Settings, load_env
from src.market import short_name
from src.timeline import _text, unverified

KEYWORDS = ("供货", "客户", "供应商", "采购", "定点", "订单")
NEWS_QUERIES = ("{name} 签约 合作 订单 客户", "{name} 战略合作协议 供货")
PAGES = 2
MAX_MENTIONS = 40
MAX_SUPPLIERS, MAX_CUSTOMERS = 15, 10
# 全文检索按相关度排序，会混入多年前的招股书，只看近两年。
RECENT_DAYS = 730
# 增量更新往前多看两天，接住检索延迟收录的公告。
UPDATE_OVERLAP = 2


def mentions(name, code):
    """多个关键词检索，单次检索失败只跳过；同一家公司保留最近两段。"""
    found, searched = {}, []
    since = (market.now().date() - timedelta(days=RECENT_DAYS)).isoformat()
    for keyword in KEYWORDS:
        for page in range(1, PAGES + 1):
            try:
                rows = events.search_mentions(name, keyword, code, page)
            except ValueError:
                searched.append({"keyword": keyword, "page": page, "ok": False})
                continue
            searched.append({"keyword": keyword, "page": page, "ok": True})
            for row in rows:
                if row["date"] >= since:
                    found.setdefault(row["url"], row)
    by_company = {}
    for row in sorted(found.values(), key=lambda r: r["date"], reverse=True):
        rows = by_company.setdefault(row["sec_code"], [])
        if len(rows) < 2 and all(row["text"] != other["text"] for other in rows):
            rows.append(row)
    rows = [row for rows in by_company.values() for row in rows]
    return rows[:MAX_MENTIONS], searched


def coverage(name, settings):
    found = {}
    for query in NEWS_QUERIES:
        for row in news.search_news(query.format(name=name), settings.exa_api_key, days=365, num=10, max_chars=400)["results"]:
            found.setdefault(row["url"], row)
    return list(found.values())


def validator(candidates, segments, company):
    def entry(item):
        if not isinstance(item, dict) or item.get("id") not in candidates:
            raise agent.OutputError("id 必须是输入里的编号。")
        if item.get("status") not in prompts.CHAIN_STATUSES:
            raise agent.OutputError("status 只能是：" + "、".join(prompts.CHAIN_STATUSES) + "。")
        seller, buyer = _text(item.get("seller"), "seller"), _text(item.get("buyer"), "buyer")
        # 方向由程序按买卖双方的名字判断：模型写“对方”“本公司”时常把视角弄反。
        if (company in seller) == (company in buyer):
            return None
        side, name = ("下游", buyer) if company in seller else ("上游", seller)
        source = candidates[item["id"]]
        # 对方名字对不上原文的一条无法给出来源，单独去掉，不让整只股票失败。
        if name != source.get("company") and name not in source["title"] + source["text"]:
            return None
        segment = (item.get("segment") or "") if side == "下游" else ""
        if segment and segment not in segments:
            raise agent.OutputError("segment 只能从 segments 里选或留空。")
        category = _text(item.get("category") or "", "category")[:12] if side == "上游" else ""
        stage = item.get("stage") if side == "上游" else ""
        if side == "上游" and stage not in prompts.CHAIN_STAGES:
            raise agent.OutputError("stage 只能是：" + "、".join(prompts.CHAIN_STAGES) + "。")
        return {"id": item["id"], "side": side, "name": name[:40], "category": category, "segment": segment,
                "stage": stage, "dependent": side == "上游" and item.get("dependent") is True,
                "status": item["status"], "note": _text(item.get("note", ""), "note"), "signal": _text(item.get("signal", ""), "signal")}

    def validate(payload, references):
        items = payload.get("links")
        if not isinstance(items, list):
            raise agent.OutputError("缺少 links 数组。")
        result, seen, used = [], set(), set()
        caps = {"上游": MAX_SUPPLIERS, "下游": MAX_CUSTOMERS}
        for item in items:
            checked = entry(item)
            # 一段资料只对应一家公司：汇总稿会把一篇新闻拆成很多条关系。
            if checked and caps[checked["side"]] and (checked["side"], checked["name"]) not in seen and checked["id"] not in used:
                seen.add((checked["side"], checked["name"]))
                used.add(checked["id"])
                caps[checked["side"]] -= 1
                result.append(checked)
        return {"links": result}
    return validate


def classify(name, segments, rows, reports, settings):
    """一次模型请求判断每段资料的买卖方向；原文段落、日期和链接由程序从检索结果取。"""
    candidates = {f"C{i + 1}": row for i, row in enumerate(rows)} | {f"N{i + 1}": row for i, row in enumerate(reports)}
    if not candidates:
        return []
    payload = {"company": name, "segments": segments,
               "announcements": [{"id": key, "company": row["company"], "date": row["date"], "text": row["text"]}
                                 for key, row in candidates.items() if key.startswith("C")],
               "news": [{"id": key, "title": row["title"], "date": row["date"], "domain": row["domain"], "text": row["text"]}
                        for key, row in candidates.items() if key.startswith("N")]}
    result = agent.complete([{"role": "user", "content": prompts.CHAIN + "\n" + json.dumps(payload, ensure_ascii=False)}],
                            settings, validator(candidates, segments, name), max_tokens=4000)
    links = []
    for item in result["result"]["links"]:
        row = candidates[item.pop("id")]
        official = "sec_code" in row
        links.append({**item, "sec_code": row["sec_code"] if official and item["name"] == row["company"] else "", "date": row["date"] or "",
                      "quote": row["text"] if official else row["title"] or row["text"][:120],
                      "source": row["title"] if official else row["domain"], "url": row["url"],
                      "unverified": unverified(item["note"] + " " + item["signal"], set(_numbers([row])))})
    return links


def build(code, settings, store):
    code = market.resolve_symbol(code)
    name = short_name(market.fetch_quotes([code])[code].name)
    business = events.fetch_business(code)
    rows, searched = mentions(name, code[2:])
    # 新闻只是客户的补充来源，检索不可用时用公告里的段落照常整理。
    try:
        reports = coverage(name, settings)
    except ValueError as exc:
        reports, searched = [], searched + [{"keyword": "新闻", "ok": False, "error": str(exc)}]
    links = classify(name, [s["name"] for s in business["segments"]], rows, reports, settings)
    store.save_chain(code, name, business, searched, links)
    industry.refresh(store, [code])
    return {"code": code, "suppliers": sum(l["side"] == "上游" for l in links), "customers": sum(l["side"] == "下游" for l in links),
            "searched": searched}


def update(code, settings, store):
    """晨报之后的增量更新：只看上次整理以来的新段落和新闻，已知的公司和链接不再交给模型，新关系追加在后。"""
    current = store.chain(code)
    if current is None:
        return None
    since = (date.fromtimestamp(current["updated_at"]) - timedelta(days=UPDATE_OVERLAP)).isoformat()
    known_urls = {l["url"] for l in current["links"]}
    known = {l["name"] for l in current["links"]} | {l["sec_code"] for l in current["links"] if l["sec_code"]}
    rows = []
    for keyword in KEYWORDS:
        # 单个关键词检索失败只跳过，明天的更新还会覆盖这几天。
        try:
            found = events.search_mentions(current["name"], keyword, code[2:])
        except ValueError:
            continue
        rows += [r for r in found if r["date"] >= since and r["url"] not in known_urls and r["company"] not in known
                 and r["sec_code"] not in known and r["url"] not in {x["url"] for x in rows}]
    try:
        found = news.search_news(NEWS_QUERIES[0].format(name=current["name"]), settings.exa_api_key, num=8, max_chars=400, since=since)
        reports = [r for r in found["results"] if r["url"] not in known_urls]
    except ValueError:
        reports = []
    links = classify(current["name"], [s["name"] for s in current["segments"]], rows[:MAX_MENTIONS], reports, settings)
    links = [l for l in links if l["name"] not in known]
    store.add_chain_links(code, links, {"上游": MAX_SUPPLIERS, "下游": MAX_CUSTOMERS})
    if links:
        industry.refresh(store, [code])
    return {"code": code, "candidates": len(rows) + len(reports), "added": len(links)}


def main(argv=None):
    from src.accounts import default_store
    from src.storage import Storage
    parser = argparse.ArgumentParser(description="整理股票的上下游")
    parser.add_argument("codes", nargs="*", help="带交易所的股票代码；不填为所有已有脉络的股票")
    parser.add_argument("--force", action="store_true", help="已有上下游时重新整理")
    args = parser.parse_args(argv)
    load_env()
    store, settings = Storage(default_store().path), Settings.from_env()
    done = set(store.chain_codes())
    for code in args.codes or store.timeline_codes():
        if code in done and not args.force:
            print(f"{code}：已有上下游，跳过")
            continue
        # 单只股票失败只打印，批量补齐继续往下做。
        try:
            result = build(code, settings, store)
        except ValueError as exc:
            print(f"{code}：未完成，{exc}")
            continue
        failed = sum(not s["ok"] for s in result["searched"])
        print(f"{code}：上游 {result['suppliers']}，下游 {result['customers']}，检索失败 {failed}/{len(result['searched'])}")


if __name__ == "__main__":
    main()

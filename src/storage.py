"""持仓、晨报、任务与每日用量，共用账号数据库。"""

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from src.accounts import Store
from src.market import is_index, resolve_symbol

DEFAULT_HOLDINGS = [
    ("sz300750", "300", "238.50"), ("sz002079", "5000", "9.86"),
    ("sh601212", "20000", "5.32"), ("sh688836", "100", "512.00"),
    ("sh600519", "100", "1580.00"), ("sh601318", "1000", "52.40"),
    ("sz000333", "800", "71.20"), ("sh600036", "1500", "38.90"),
]
SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (username TEXT NOT NULL, code TEXT NOT NULL,
 qty TEXT, cost TEXT, position INTEGER NOT NULL, added_at INTEGER NOT NULL, PRIMARY KEY(username, code));
CREATE TABLE IF NOT EXISTS briefs (portfolio_key TEXT NOT NULL, date TEXT NOT NULL,
 status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(portfolio_key, date));
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, username TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, input_json TEXT NOT NULL, steps_json TEXT NOT NULL, result_json TEXT,
 error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(username, created_at);
CREATE TABLE IF NOT EXISTS usage (username TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL,
 count INTEGER NOT NULL, PRIMARY KEY(username, date, kind));
CREATE TABLE IF NOT EXISTS timeline_stocks (code TEXT PRIMARY KEY, since TEXT NOT NULL, until TEXT NOT NULL,
 announcements_json TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY, code TEXT NOT NULL, title TEXT NOT NULL, theme TEXT NOT NULL,
 status TEXT NOT NULL, summary TEXT NOT NULL, error TEXT NOT NULL, unverified_json TEXT NOT NULL, steps_json TEXT NOT NULL,
 added_by TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS threads_code ON threads(code);
CREATE TABLE IF NOT EXISTS thread_nodes (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, date TEXT NOT NULL,
 kind TEXT NOT NULL, major INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, analysis TEXT NOT NULL,
 unverified_json TEXT NOT NULL, url TEXT NOT NULL, art_code TEXT, added_by TEXT, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS thread_nodes_thread ON thread_nodes(thread_id);
CREATE TABLE IF NOT EXISTS thread_links (id INTEGER PRIMARY KEY, username TEXT NOT NULL, node_a INTEGER NOT NULL,
 node_b INTEGER NOT NULL, verdict TEXT NOT NULL, explanation TEXT NOT NULL, refs_json TEXT NOT NULL,
 steps_json TEXT NOT NULL, created_at INTEGER NOT NULL);
"""
COMMITMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, code TEXT NOT NULL, kind TEXT NOT NULL,
 content TEXT NOT NULL, amount TEXT NOT NULL, deadline_text TEXT NOT NULL, deadline TEXT NOT NULL, node_id INTEGER NOT NULL,
 status TEXT NOT NULL, outcome TEXT NOT NULL, evidence_json TEXT NOT NULL, unverified_json TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS commitments_code ON commitments(code);
CREATE TABLE IF NOT EXISTS commitment_reviews (thread_id INTEGER PRIMARY KEY, steps_json TEXT NOT NULL, error TEXT NOT NULL,
 reviewed_at INTEGER NOT NULL);
"""
CHAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_stocks (code TEXT PRIMARY KEY, name TEXT NOT NULL, business_json TEXT NOT NULL,
 searched_json TEXT NOT NULL, error TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chain_links (id INTEGER PRIMARY KEY, code TEXT NOT NULL, side TEXT NOT NULL, name TEXT NOT NULL,
 sec_code TEXT NOT NULL, category TEXT NOT NULL, segment TEXT NOT NULL, status TEXT NOT NULL, date TEXT NOT NULL,
 quote TEXT NOT NULL, note TEXT NOT NULL, signal TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
 unverified_json TEXT NOT NULL, created_at INTEGER NOT NULL, stage TEXT NOT NULL DEFAULT '', dependent INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS chain_links_code ON chain_links(code);
"""
MACRO_SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_events (id INTEGER PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
 summary TEXT NOT NULL, reason TEXT NOT NULL, industries_json TEXT NOT NULL, url TEXT NOT NULL UNIQUE, domain TEXT NOT NULL,
 unverified_json TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS macro_events_date ON macro_events(date);
"""
BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_boards (sec_code TEXT PRIMARY KEY, name TEXT NOT NULL, sector TEXT NOT NULL,
 industry_code TEXT NOT NULL, industry TEXT NOT NULL, concepts_json TEXT NOT NULL, fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS industry_news (id INTEGER PRIMARY KEY, industry TEXT NOT NULL, date TEXT NOT NULL, title TEXT NOT NULL,
 summary TEXT NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL, unverified_json TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS industry_news_industry ON industry_news(industry, date);
"""
CHAIN_FIELDS = ("id", "code", "side", "name", "sec_code", "category", "segment", "status", "date", "quote", "note", "signal",
                "source", "url", "unverified", "created_at", "stage", "dependent")
COMMITMENT_FIELDS = ("id", "thread_id", "code", "kind", "content", "amount", "deadline_text", "deadline", "node_id", "status",
                     "outcome", "evidence", "unverified", "created_at")
THREAD_FIELDS = ("id", "code", "title", "theme", "status", "summary", "error", "unverified", "steps", "added_by", "created_at", "updated_at")
NODE_FIELDS = ("id", "thread_id", "date", "kind", "major", "title", "summary", "analysis", "unverified", "url", "art_code",
               "added_by", "created_at")


QUOTA_NAMES = {"brief": "生成晨报", "research": "研究", "timeline": "生成脉络", "link": "关联分析"}


class QuotaExceeded(ValueError):
    pass


def portfolio_key(codes):
    return hashlib.sha256(",".join(sorted(set(codes))).encode()).hexdigest()[:16]


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Storage(Store):
    def __init__(self, path):
        super().__init__(path)
        with self.db() as db:
            db.executescript(SCHEMA + COMMITMENT_SCHEMA + CHAIN_SCHEMA + BOARD_SCHEMA + MACRO_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            # 区分首次读取与用户主动清空，空持仓不能在下一次读取时重新灌入示例。
            if "holdings_initialized" not in [row[1] for row in db.execute("PRAGMA table_info(accounts)")]:
                db.execute("ALTER TABLE accounts ADD COLUMN holdings_initialized INTEGER NOT NULL DEFAULT 0")
            # 生产阶段和依赖程度是 11-03 后加的列，已有库补上。
            if "stage" not in [row[1] for row in db.execute("PRAGMA table_info(chain_links)")]:
                db.execute("ALTER TABLE chain_links ADD COLUMN stage TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE chain_links ADD COLUMN dependent INTEGER NOT NULL DEFAULT 0")

    def holdings(self, username):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            initialized = db.execute("SELECT holdings_initialized FROM accounts WHERE username=?", (username,)).fetchone()
            if initialized is None:
                raise ValueError("账号不存在。")
            if not initialized[0]:
                db.executemany("INSERT OR IGNORE INTO holdings VALUES (?, ?, ?, ?, ?, ?)",
                               [(username, code, qty, cost, i, self.now()) for i, (code, qty, cost) in enumerate(DEFAULT_HOLDINGS)])
                db.execute("UPDATE accounts SET holdings_initialized=1 WHERE username=?", (username,))
            rows = db.execute("SELECT code, qty, cost, position, added_at FROM holdings WHERE username=? ORDER BY position", (username,))
            return [dict(zip(("code", "qty", "cost", "position", "added_at"), row)) for row in rows]

    def set_holdings(self, username, holdings):
        if len(holdings) > 30:
            raise ValueError("持仓最多 30 只。")
        rows, seen = [], set()
        for position, holding in enumerate(holdings):
            code = resolve_symbol(holding["code"])
            if is_index(code) or code in seen:
                raise ValueError("持仓须为不重复的个股。")
            seen.add(code)
            amounts = []
            for key in ("qty", "cost"):
                value = holding.get(key)
                if value is None or value == "":
                    amounts.append(None)
                    continue
                try:
                    number = Decimal(str(value))
                except InvalidOperation:
                    raise ValueError("数量和成本须为非负数字。") from None
                if not number.is_finite() or number < 0:
                    raise ValueError("数量和成本须为非负数字。")
                if number.adjusted() > 20 or number.as_tuple().exponent < -20:
                    raise ValueError("数量或成本超出支持的精度范围。")
                amounts.append(format(number, "f"))
            rows.append((username, code, *amounts, position, self.now()))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            added = dict(db.execute("SELECT code, added_at FROM holdings WHERE username=?", (username,)))
            rows = [(*row[:-1], added.get(row[1], row[-1])) for row in rows]
            db.execute("DELETE FROM holdings WHERE username=?", (username,))
            db.executemany("INSERT INTO holdings VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.execute("UPDATE accounts SET holdings_initialized=1 WHERE username=?", (username,))

    def brief(self, codes, day):
        with self.db() as db:
            row = db.execute("SELECT payload_json FROM briefs WHERE portfolio_key=? AND date=? AND status='done'",
                             (portfolio_key(codes), day)).fetchone()
            return json.loads(row[0]) if row else None

    def briefs(self, codes):
        with self.db() as db:
            rows = db.execute("SELECT date, json_extract(payload_json, '$.title') FROM briefs "
                              "WHERE portfolio_key=? AND status='done' ORDER BY date DESC", (portfolio_key(codes),))
            return [{"date": day, "title": title} for day, title in rows]

    def save_brief(self, codes, day, payload):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO briefs VALUES (?, ?, 'done', ?, ?)",
                       (portfolio_key(codes), day, encode(payload), self.now()))

    def create_job(self, username, kind, inputs, day, limit):
        raw = encode(inputs)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM jobs WHERE username=? AND kind=? AND input_json=? AND status IN ('queued','running')",
                             (username, kind, raw)).fetchone()
            if row:
                return row[0], False
            used = db.execute("SELECT count FROM usage WHERE username=? AND date=? AND kind=?", (username, day, kind)).fetchone()
            if limit is not None and used and used[0] >= limit:
                raise QuotaExceeded(f"今日{QUOTA_NAMES[kind]}次数已用完。")
            job_id = uuid4().hex
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, 'queued', ?, '[]', NULL, NULL, ?, ?)",
                       (job_id, username, kind, raw, self.now(), self.now()))
            db.execute("INSERT INTO usage VALUES (?, ?, ?, 1) ON CONFLICT(username,date,kind) DO UPDATE SET count=count+1",
                       (username, day, kind))
            return job_id, True

    def job(self, job_id, username):
        with self.db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM jobs WHERE id=? AND username=?", (job_id, username)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("input", "steps", "result"):
            raw = result.pop(key + "_json")
            result[key] = json.loads(raw) if raw is not None else None
        return result

    def jobs(self, username, kind="research", limit=30):
        with self.db() as db:
            rows = db.execute("SELECT id, input_json, result_json, status, error, created_at FROM jobs "
                              "WHERE username=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (username, kind, limit)).fetchall()
        result = []
        for job_id, inputs, output, status, error, created_at in rows:
            result.append({"id": job_id, "input": json.loads(inputs), "result": json.loads(output) if output else None,
                           "status": status, "error": error, "created_at": created_at})
        return result

    def source_observations(self, username):
        groups = {"get_price_history": "腾讯行情", "compare": "腾讯行情", "search_news": "Exa",
                  **dict.fromkeys(("list_announcements", "read_announcement", "get_unlocks", "get_holder_changes",
                                   "get_forecasts", "get_financials"), "东方财富")}
        with self.db() as db:
            rows = db.execute("SELECT steps_json, result_json, updated_at FROM jobs WHERE username=? "
                              "ORDER BY updated_at DESC, rowid DESC LIMIT 30", (username,)).fetchall()
        observations = {}
        for steps, result, at in rows:
            for step in reversed(json.loads(steps)):
                name = groups.get(step["tool"])
                if name and name not in observations:
                    observations[name] = {"state": "error" if step["error"] else "ok", "checked_at": at,
                                          "elapsed_seconds": step["elapsed_seconds"]}
            output = json.loads(result) if result else {}
            if output.get("usage") or any(stock.get("usage") for stock in output.get("stocks", [])):
                observations.setdefault("DeepSeek", {"state": "ok", "checked_at": at})
        return observations

    def usage_count(self, username, day, kind):
        with self.db() as db:
            row = db.execute("SELECT count FROM usage WHERE username=? AND date=? AND kind=?", (username, day, kind)).fetchone()
        return row[0] if row else 0

    def update_job(self, job_id, *, status=None, steps=None, result=None, error=None):
        fields, values = ["updated_at=?"], [self.now()]
        for key, value in (("status", status), ("steps_json", encode(steps) if steps is not None else None),
                           ("result_json", encode(result) if result is not None else None), ("error", error)):
            if value is not None:
                fields.append(key + "=?")
                values.append(value)
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ", ".join(fields) + " WHERE id=?", [*values, job_id])

    def timeline_stock(self, code):
        with self.db() as db:
            row = db.execute("SELECT since, until, announcements_json FROM timeline_stocks WHERE code=?", (code,)).fetchone()
        return {"since": row[0], "until": row[1], "announcements": json.loads(row[2])} if row else None

    def save_timeline(self, code, since, until, announcements, threads):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM timeline_stocks WHERE code=?", (code,)).fetchone():
                return
            db.execute("INSERT INTO timeline_stocks VALUES (?, ?, ?, ?, ?)", (code, since, until, encode(announcements), self.now()))
            for thread in threads:
                thread_id = self._insert_thread(db, code, thread)
                for node in thread["nodes"]:
                    self._insert_node(db, thread_id, node)

    def _insert_thread(self, db, code, thread, added_by=None):
        return db.execute("INSERT INTO threads (code, title, theme, status, summary, error, unverified_json, steps_json, added_by, "
                          "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (code, thread["title"], thread["theme"], thread["status"], thread.get("summary", ""), thread.get("error", ""),
                           encode(thread.get("unverified", [])), encode(thread.get("steps", [])), added_by, self.now(), self.now())).lastrowid

    def _insert_node(self, db, thread_id, node, added_by=None):
        return db.execute("INSERT INTO thread_nodes (thread_id, date, kind, major, title, summary, analysis, unverified_json, url, "
                          "art_code, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (thread_id, node["date"], node["kind"], int(node["major"]), node["title"], node.get("summary", ""),
                           node.get("analysis", ""), encode(node.get("unverified", [])), node["url"], node.get("art_code"),
                           added_by, self.now())).lastrowid

    def threads(self, code, username=None):
        with self.db() as db:
            threads = db.execute("SELECT * FROM threads WHERE code=? AND (added_by IS NULL OR added_by=?) ORDER BY id",
                                 (code, username)).fetchall()
            nodes = db.execute("SELECT n.* FROM thread_nodes n JOIN threads t ON t.id=n.thread_id WHERE t.code=? "
                               "AND (n.added_by IS NULL OR n.added_by=?) ORDER BY n.date, n.id", (code, username)).fetchall()
        result = {row[0]: {**dict(zip(THREAD_FIELDS, row)), "nodes": []} for row in threads}
        for thread in result.values():
            thread["steps"], thread["unverified"] = json.loads(thread["steps"]), json.loads(thread["unverified"])
        for row in nodes:
            node = dict(zip(NODE_FIELDS, row))
            node["major"], node["unverified"] = bool(node["major"]), json.loads(node["unverified"])
            if node["thread_id"] in result:
                result[node["thread_id"]]["nodes"].append(node)
        return list(result.values())

    def attach_nodes(self, code, items):
        """items: [{"thread": id 或 None, "title", "theme", "status", "node": {...}}]，晨报每日归类用；返回涉及的事项 id。"""
        touched = set()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            known = {row[0] for row in db.execute("SELECT id FROM threads WHERE code=? AND added_by IS NULL", (code,))}
            for item in items:
                thread_id = item["thread"]
                if thread_id is None:
                    thread_id = self._insert_thread(db, code, {"title": item["title"], "theme": item["theme"], "status": item["status"]})
                    known.add(thread_id)
                elif thread_id in known:
                    db.execute("UPDATE threads SET status=?, updated_at=? WHERE id=?", (item["status"], self.now(), thread_id))
                else:
                    continue
                self._insert_node(db, thread_id, item["node"])
                touched.add(thread_id)
        return touched

    def add_branches(self, thread_id, nodes):
        """新闻旁枝；同一事项里已有的链接不重复收录。"""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            known = {row[0] for row in db.execute("SELECT url FROM thread_nodes WHERE thread_id=?", (thread_id,))}
            for node in nodes:
                if node["url"] not in known:
                    self._insert_node(db, thread_id, node)
                    known.add(node["url"])

    def add_user_node(self, code, username, node, *, thread_id=None, title=None, theme="其他"):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if thread_id is None:
                thread_id = self._insert_thread(db, code, {"title": title, "theme": theme, "status": "进行中"}, username)
            elif not db.execute("SELECT 1 FROM threads WHERE id=? AND code=? AND (added_by IS NULL OR added_by=?)",
                                (thread_id, code, username)).fetchone():
                raise ValueError("事项不存在。")
            return self._insert_node(db, thread_id, node, username)

    def node(self, node_id, username):
        with self.db() as db:
            row = db.execute("SELECT n.*, t.code FROM thread_nodes n JOIN threads t ON t.id=n.thread_id WHERE n.id=? "
                             "AND (n.added_by IS NULL OR n.added_by=?)", (node_id, username)).fetchone()
        return {**dict(zip(NODE_FIELDS, row)), "code": row[-1]} if row else None

    def save_link(self, username, node_a, node_b, result):
        with self.db() as db:
            return db.execute("INSERT INTO thread_links (username, node_a, node_b, verdict, explanation, refs_json, steps_json, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (username, node_a, node_b, result["verdict"], result["explanation"],
                               encode(result["references"]), encode(result["steps"]), self.now())).lastrowid

    def links(self, code, username):
        with self.db() as db:
            rows = db.execute("SELECT l.id, l.node_a, l.node_b, l.verdict, l.explanation, l.refs_json, l.steps_json, l.created_at "
                              "FROM thread_links l JOIN thread_nodes n ON n.id=l.node_a JOIN threads t ON t.id=n.thread_id "
                              "WHERE l.username=? AND t.code=? ORDER BY l.id", (username, code)).fetchall()
        return [{"id": row[0], "node_a": row[1], "node_b": row[2], "verdict": row[3], "explanation": row[4],
                 "references": json.loads(row[5]), "steps": json.loads(row[6]), "created_at": row[7]} for row in rows]

    def save_commitments(self, thread_id, code, commitments, steps, error=""):
        """重新审阅一个事项后整体替换它的承诺；承诺只由智能体生成。"""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM commitments WHERE thread_id=?", (thread_id,))
            db.executemany("INSERT INTO commitments (thread_id, code, kind, content, amount, deadline_text, deadline, node_id, status, "
                           "outcome, evidence_json, unverified_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           [(thread_id, code, c["kind"], c["content"], c["amount"], c["deadline_text"], c["deadline"], c["node_id"],
                             c["status"], c["outcome"], encode(c["evidence"]), encode(c["unverified"]), self.now()) for c in commitments])
            db.execute("INSERT OR REPLACE INTO commitment_reviews VALUES (?, ?, ?, ?)", (thread_id, encode(steps), error, self.now()))

    def save_chain(self, code, name, business, searched, links, error=""):
        """重新整理一只股票的上下游时整体替换。"""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chain_links WHERE code=?", (code,))
            db.execute("INSERT OR REPLACE INTO chain_stocks VALUES (?, ?, ?, ?, ?, ?)",
                       (code, name, encode(business), encode(searched), error, self.now()))
            db.executemany("INSERT INTO chain_links (code, side, name, sec_code, category, segment, status, date, quote, note, signal, "
                           "source, url, unverified_json, created_at, stage, dependent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           [(code, l["side"], l["name"], l["sec_code"], l["category"], l["segment"], l["status"], l["date"], l["quote"],
                             l["note"], l["signal"], l["source"], l["url"], encode(l["unverified"]), self.now(), l["stage"],
                             int(l["dependent"])) for l in links])

    def add_chain_links(self, code, links, caps):
        """每日增量：追加新关系，每一侧超过上限时删掉日期最早的；created_at 记最近一次整理时间。"""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO chain_links (code, side, name, sec_code, category, segment, status, date, quote, note, signal, "
                           "source, url, unverified_json, created_at, stage, dependent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           [(code, l["side"], l["name"], l["sec_code"], l["category"], l["segment"], l["status"], l["date"], l["quote"],
                             l["note"], l["signal"], l["source"], l["url"], encode(l["unverified"]), self.now(), l["stage"],
                             int(l["dependent"])) for l in links])
            for side, cap in caps.items():
                db.execute("DELETE FROM chain_links WHERE id IN (SELECT id FROM chain_links WHERE code=? AND side=? "
                           "ORDER BY date DESC, id DESC LIMIT -1 OFFSET ?)", (code, side, cap))
            db.execute("UPDATE chain_stocks SET created_at=? WHERE code=?", (self.now(), code))

    def chain(self, code):
        with self.db() as db:
            row = db.execute("SELECT name, business_json, searched_json, error, created_at FROM chain_stocks WHERE code=?", (code,)).fetchone()
            links = db.execute("SELECT * FROM chain_links WHERE code=? ORDER BY side, date DESC, id", (code,)).fetchall()
        if row is None:
            return None
        items = [dict(zip(CHAIN_FIELDS, link)) for link in links]
        for item in items:
            item["unverified"], item["dependent"] = json.loads(item["unverified"]), bool(item["dependent"])
        return {"name": row[0], **json.loads(row[1]), "searched": json.loads(row[2]), "error": row[3], "updated_at": row[4], "links": items}

    def save_company_board(self, sec_code, name, boards):
        industry = boards["industry"] or {"code": "", "name": ""}
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO company_boards VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (sec_code, name, boards["sector"], industry["code"], industry["name"], encode(boards["concepts"]), self.now()))

    def company_boards(self):
        with self.db() as db:
            rows = db.execute("SELECT sec_code, name, sector, industry_code, industry, concepts_json FROM company_boards").fetchall()
        return {row[0]: {"name": row[1], "sector": row[2], "industry": {"code": row[3], "name": row[4]} if row[4] else None,
                         "concepts": json.loads(row[5])} for row in rows}

    def add_industry_news(self, items, keep_since):
        """每日行业重点新闻；同一链接不重复收录，早于 keep_since 的删掉。"""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            known = {row[0] for row in db.execute("SELECT url FROM industry_news")}
            db.executemany("INSERT INTO industry_news (industry, date, title, summary, url, domain, unverified_json, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(i["industry"], i["date"], i["title"], i["summary"], i["url"], i["domain"], encode(i["unverified"]), self.now())
                            for i in items if i["url"] not in known])
            db.execute("DELETE FROM industry_news WHERE date < ?", (keep_since,))

    def industry_news(self, since):
        with self.db() as db:
            rows = db.execute("SELECT industry, date, title, summary, url, domain, unverified_json FROM industry_news "
                              "WHERE date >= ? ORDER BY date DESC, id DESC", (since,)).fetchall()
        return [{"industry": r[0], "date": r[1], "title": r[2], "summary": r[3], "url": r[4], "domain": r[5],
                 "unverified": json.loads(r[6])} for r in rows]

    def add_macro_events(self, items):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO macro_events (date, kind, title, summary, reason, industries_json, url, domain, unverified_json, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           [(i["date"], i["kind"], i["title"], i["summary"], i["reason"], encode(i["industries"]), i["url"], i["domain"],
                             encode(i["unverified"]), self.now()) for i in items])
            return db.total_changes - before

    def macro_events(self, since):
        with self.db() as db:
            rows = db.execute("SELECT id, date, kind, title, summary, reason, industries_json, url, domain, unverified_json FROM macro_events "
                              "WHERE date >= ? ORDER BY date DESC, id DESC", (since,)).fetchall()
        return [{"id": r[0], "date": r[1], "kind": r[2], "title": r[3], "summary": r[4], "reason": r[5], "industries": json.loads(r[6]),
                 "url": r[7], "domain": r[8], "unverified": json.loads(r[9])} for r in rows]

    def thread_code(self, thread_id):
        with self.db() as db:
            return db.execute("SELECT code FROM threads WHERE id=?", (thread_id,)).fetchone()[0]

    def update_thread_analysis(self, thread_id, status, summary, unverified_items, steps, nodes):
        """重新分析一个事项：节点编号不变，承诺等引用不受影响；没写到的节点保留原文。"""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE threads SET status=?, summary=?, error='', unverified_json=?, steps_json=?, updated_at=? WHERE id=?",
                       (status, summary, encode(unverified_items), encode(steps), self.now(), thread_id))
            db.executemany("UPDATE thread_nodes SET summary=?, analysis=?, unverified_json=? WHERE id=? AND thread_id=?",
                           [(n["summary"], n["analysis"], encode(n["unverified"]), node_id, thread_id) for node_id, n in nodes.items()])

    def chain_codes(self):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT code FROM chain_stocks ORDER BY code")]

    def timeline_codes(self):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT code FROM timeline_stocks ORDER BY code")]

    def reviewed_threads(self, code):
        with self.db() as db:
            return {row[0] for row in db.execute("SELECT r.thread_id FROM commitment_reviews r JOIN threads t ON t.id=r.thread_id "
                                                 "WHERE t.code=?", (code,))}

    def commitments(self, codes):
        marks = ",".join("?" * len(codes))
        with self.db() as db:
            rows = db.execute(f"SELECT c.*, t.title, n.date, n.title, n.url, r.steps_json FROM commitments c "
                              f"JOIN threads t ON t.id=c.thread_id JOIN thread_nodes n ON n.id=c.node_id "
                              f"JOIN commitment_reviews r ON r.thread_id=c.thread_id "
                              f"WHERE c.code IN ({marks}) AND t.added_by IS NULL ORDER BY n.date DESC, c.id", list(codes)).fetchall()
        result = []
        for row in rows:
            item = dict(zip(COMMITMENT_FIELDS, row))
            item["evidence"], item["unverified"] = json.loads(item["evidence"]), json.loads(item["unverified"])
            extra = row[len(COMMITMENT_FIELDS):]
            item.update({"thread_title": extra[0], "announced": extra[1], "source_title": extra[2], "source_url": extra[3],
                         "steps": json.loads(extra[4])})
            result.append(item)
        ids = {node for item in result for node in item["evidence"]}
        with self.db() as db:
            found = {row[0]: {"id": row[0], "date": row[1], "title": row[2], "url": row[3]} for row in db.execute(
                f"SELECT id, date, title, url FROM thread_nodes WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
        for item in result:
            item["evidence"] = sorted((found[node] for node in item["evidence"] if node in found), key=lambda node: node["date"])
        return result

    def recover_jobs(self):
        with self.db() as db:
            # 排队任务也随进程消失，不能让页面永久轮询。
            db.execute("UPDATE jobs SET status='failed', error='服务重启，任务已中断。', updated_at=? WHERE status IN ('running','queued')",
                       (self.now(),))

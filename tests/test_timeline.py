import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import agent, commitments, market, timeline
from src.config import Settings
from src.storage import Storage
from src.web import create_app

SETTINGS = Settings(mode="api", base_url="https://example.test", model="test", api_key="secret")
NOW = datetime(2026, 11, 2, 8, 30, tzinfo=market.TZ)


def announcement(art_code, day, title, categories):
    return {"type": "公告", "code": "002079", "date": day, "title": "苏州固锝:" + title, "categories": categories,
            "art_code": art_code, "source": "东方财富公开数据", "url": f"https://data.eastmoney.com/notices/detail/002079/{art_code}.html"}


ANNOUNCEMENTS = [
    announcement("AN1", "2026-04-02", "关于控股股东增持公司股份计划的公告", ["股东/实际控制人股份增持"]),
    announcement("AN2", "2026-04-02", "律师事务所关于增持的法律意见书", ["法律意见书"]),
    announcement("AN3", "2026-08-01", "关于控股股东增持公司股份计划实施完成的公告", ["股东/实际控制人股份增持"]),
    announcement("AN4", "2026-07-25", "关于回购公司股份方案的公告", ["回购预案"]),
    announcement("AN5", "2026-06-30", "第八届董事会决议公告", ["董事会决议公告"]),
]


def reply(payload):
    return {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": [], "usage": {}}


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "test.db")

    def test_candidates_drop_attachments_and_flag_required_types(self):
        items = timeline.candidates(ANNOUNCEMENTS)
        self.assertNotIn("AN2", [item["art_code"] for item in items])
        flags = {item["art_code"]: item["must"] for item in items}
        self.assertEqual(flags, {"AN1": True, "AN5": False, "AN4": True, "AN3": True})
        self.assertEqual([item["id"] for item in items], ["A1", "A2", "A3", "A4"])

    @patch("src.chain.build")
    @patch("src.timeline.branch_stock", return_value=[])
    @patch("src.timeline.market.now", return_value=NOW)
    @patch("src.timeline.events.fetch_announcement_history", return_value=ANNOUNCEMENTS)
    @patch("src.agent.model_client.chat")
    def test_initialize_saves_threads_and_keeps_failed_analysis(self, chat, fetch, clock, branches, chain):
        # A1 增持计划 2026-04-02, A2 董事会 06-30, A3 回购 07-25, A4 增持完成 08-01
        chat.side_effect = [
            reply({"threads": [{"title": "控股股东增持", "theme": "股东变动", "nodes": ["A1", "A4"], "major": ["A1", "A4"]},
                               {"title": "回购", "theme": "资本运作", "nodes": ["A3"], "major": ["A3"]}]}),
            reply({"summary": "计划增持 5,000 万元，已完成 8,010.13 万元。", "status": "已完成",
                   "nodes": [{"id": "A4", "summary": "增持完成", "analysis": "兑现承诺"}], "news": []}),
            reply({"status": "完成了"}), reply({"status": "完成了"}),
            reply({"commitments": [{"kind": "增持", "content": "控股股东增持", "amount": "5,000万元至10,000万元",
                                    "deadline_text": "6个月内", "deadline": "2026-10-02", "node": "N1", "status": "已兑现",
                                    "outcome": "累计增持 8,010.13 万元", "evidence": ["N2"]}]}),
            reply({"commitments": []}),
        ]
        result = timeline.initialize("sz002079", SETTINGS, self.store)
        self.assertEqual(result["threads"], 2)
        threads = self.store.threads("sz002079")
        first, second = threads
        self.assertEqual((first["status"], [n["art_code"] for n in first["nodes"]]), ("已完成", ["AN1", "AN3"]))
        self.assertEqual(first["nodes"][1]["analysis"], "兑现承诺")
        self.assertEqual(first["unverified"], ["5,000", "8,010.13"])
        self.assertEqual((second["status"], second["summary"]), ("进行中", ""))
        self.assertTrue(second["error"])
        self.assertEqual(self.store.timeline_stock("sz002079")["until"], "2026-11-02")
        self.assertIsNone(timeline.initialize("sz002079", SETTINGS, self.store)["threads"])
        self.assertEqual(chat.call_count, 6)
        [item] = self.store.commitments(["sz002079"])
        self.assertEqual((item["status"], item["thread_title"], [n["date"] for n in item["evidence"]]), ("已兑现", "控股股东增持", ["2026-08-01"]))
        self.assertEqual(item["unverified"], ["5,000", "10,000", "8,010.13"])

    @patch("src.agent.model_client.chat")
    @patch("src.timeline.news.search_news")
    def test_branches_pick_reports_from_search_and_skip_known_links(self, search, chat):
        self.store.save_timeline("sz002079", "2025-11-02", "2026-11-02", ANNOUNCEMENTS, [
            {"title": "控股股东增持", "theme": "股东变动", "status": "已完成", "nodes": [
                {"date": "2026-04-02", "kind": "公告", "major": True, "title": "增持计划", "url": ANNOUNCEMENTS[0]["url"], "art_code": "AN1"},
                {"date": "2026-05-01", "kind": "新闻", "major": False, "title": "旧报道", "url": "https://news.example/old"}]}])
        thread = self.store.threads("sz002079")[0]
        search.return_value = {"results": [
            {"title": "旧报道", "url": "https://news.example/old", "domain": "news.example", "date": "2026-05-01", "text": ""},
            {"title": "控股股东持续增持", "url": "https://news.example/a", "domain": "news.example", "date": "2026-07-16",
             "text": "累计增持201.69万股，持股比例升至21.0645%。"}]}
        chat.return_value = reply({"news": [{"id": "N1", "summary": "增持201.69万股，持股21.0645%。", "analysis": "补充了约 3,000 万元的资金来源"}]})
        found = timeline.branch("sz002079", "苏州固锝", thread, SETTINGS)
        self.assertEqual(search.call_args.kwargs["since"], "2026-03-26")
        self.assertEqual([(n["kind"], n["url"], n["unverified"]) for n in found], [("新闻", "https://news.example/a", ["3,000"])])
        self.store.add_branches(thread["id"], found + found)
        self.assertEqual([n["url"] for n in self.store.threads("sz002079")[0]["nodes"] if n["kind"] == "新闻"],
                         ["https://news.example/old", "https://news.example/a"])
        chat.return_value = reply({"news": [{"id": "N9", "summary": "", "analysis": ""}]})
        with self.assertRaises(agent.OutputError):
            timeline.branch("sz002079", "苏州固锝", thread, SETTINGS)

    def test_group_output_must_use_input_ids(self):
        validate = timeline.group_validator({"A1": {}, "A2": {}})
        with self.assertRaises(agent.OutputError):
            validate({"threads": [{"title": "x", "theme": "其他", "nodes": ["A9"], "major": []}]}, {})
        with self.assertRaises(agent.OutputError):
            validate({"threads": [{"title": "x", "theme": "未知", "nodes": ["A1"], "major": []}]}, {})
        checked = validate({"threads": [{"title": "x", "theme": "其他", "nodes": ["A1", "A2"], "major": ["A1"]},
                                        {"title": "y", "theme": "其他", "nodes": ["A2"], "major": []}]}, {})
        self.assertEqual([t["nodes"] for t in checked["threads"]], [["A1", "A2"]])

    def test_news_must_come_from_search_results(self):
        validate = timeline.thread_validator({"A1"})
        refs = {"R1": {"results": [{"title": "报道", "url": "https://news.example/a", "domain": "news.example", "date": "2026-04-03", "text": ""}]}}
        base = {"summary": "s", "status": "进行中", "nodes": []}
        checked = validate({**base, "news": [{"ref": "R1", "url": "https://news.example/a", "analysis": "相关"}]}, refs)
        self.assertEqual(checked["news"][0]["title"], "报道")
        with self.assertRaises(agent.OutputError):
            validate({**base, "news": [{"ref": "R1", "url": "https://made.up/b", "analysis": ""}]}, refs)

    @patch("src.agent.model_client.chat")
    def test_attach_files_new_events_once_and_skips_old_dates(self, chat):
        self.store.save_timeline("sz002079", "2025-11-02", "2026-10-30", ANNOUNCEMENTS, [
            {"title": "回购", "theme": "资本运作", "status": "进行中",
             "nodes": [{"date": "2026-07-25", "kind": "公告", "major": True, "title": "回购方案", "url": ANNOUNCEMENTS[3]["url"], "art_code": "AN4"}]}])
        thread_id = self.store.threads("sz002079")[0]["id"]
        rows = [announcement("AN9", "2026-11-01", "关于回购实施完成的公告", ["回购实施公告"]),
                announcement("AN8", "2026-10-29", "三季度报告", ["三季度报告全文"])]
        stock = {"code": "sz002079", "references": {"R1": rows}, "events": [
            {"date": "2026-11-01", "tag": "回购", "title": "回购完成", "points": ["回购完成", "按计划兑现"], "risk": "", "refs": ["R1"]},
            {"date": "2026-10-29", "tag": "财报", "title": "三季报", "points": ["营收增长"], "risk": "", "refs": ["R1"]}]}
        chat.side_effect = [reply({"items": [{"event": 0, "thread": thread_id, "major": True, "status": "已完成"}]}),
                            reply({"commitments": [{"kind": "回购", "content": "公司回购", "node": f"N{thread_id}", "status": "已兑现",
                                                    "evidence": [f"N{thread_id + 1}"]}]})]
        items = timeline.attach("sz002079", stock, SETTINGS, self.store)
        self.assertEqual(len(items), 1)
        sent = json.loads(chat.call_args_list[0].args[0][-1]["content"].rsplit("\n", 1)[-1])
        self.assertEqual([e["title"] for e in sent["events"]], ["回购完成"])
        thread = self.store.threads("sz002079")[0]
        self.assertEqual((thread["status"], thread["nodes"][-1]["art_code"]), ("已完成", "AN9"))
        self.assertEqual(self.store.commitments(["sz002079"])[0]["status"], "已兑现")
        self.assertEqual(timeline.attach("sz002079", stock, SETTINGS, self.store), [])
        self.assertEqual(chat.call_count, 2)


class CommitmentTests(unittest.TestCase):
    def item(self, status, deadline="", evidence=()):
        return {"status": status, "deadline": deadline, "announced": "2026-04-02", "evidence": [{"date": day} for day in evidence]}

    def test_ledger_marks_overdue_upcoming_and_recent_changes(self):
        rows = commitments.ledger([self.item("进行中", "2026-10-02"), self.item("进行中", "2026-11-20"),
                                   self.item("进行中", "2027-07-23"), self.item("延期", "2028-08-31", ["2026-10-25"]),
                                   self.item("变更", "", ["2026-08-26"]), self.item("已兑现", "2026-10-02")], "2026-11-02")
        self.assertEqual([row["state"] for row in rows], ["到期未披露", "进行中", "进行中", "延期", "变更", "已兑现"])
        self.assertEqual([bool(row["alert"]) for row in rows], [True, True, False, True, False, False])
        self.assertEqual(rows[1]["alert"], "18 天后到期")
        self.assertEqual(commitments.stats(rows), {"到期未披露": 1, "进行中": 2, "延期": 1, "变更": 1, "已兑现": 1})

    def test_validator_rejects_unknown_nodes_and_bad_dates(self):
        validate = commitments.validator({"N1", "N2"})
        good = {"kind": "回购", "content": "回购 5,000 万元", "node": "N1", "status": "进行中", "deadline": "2027-07-23", "evidence": ["N2"]}
        self.assertEqual(validate({"commitments": [good]}, {})["commitments"][0]["deadline"], "2027-07-23")
        for bad in ({"node": "N9"}, {"evidence": ["N9"]}, {"deadline": "明年"}, {"status": "差不多"}, {"content": " "}):
            with self.assertRaises(agent.OutputError):
                validate({"commitments": [{**good, **bad}]}, {})


class TimelineApiTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("测试不得发起网络请求")))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Storage(Path(temp.name) / "test.db")
        with patch("src.accounts.SCRYPT", {"n": 16, "r": 8, "p": 1}):
            self.store.add("alice", "password")
            self.store.add("bob", "password")
        self.linked = []
        def initializer(code, settings, store, on_step):
            store.save_timeline(code, "2025-11-02", "2026-11-02", ANNOUNCEMENTS, [
                {"title": "增持", "theme": "股东变动", "status": "已完成", "nodes": [
                    {"date": "2026-04-02", "kind": "公告", "major": True, "title": "增持计划", "url": ANNOUNCEMENTS[0]["url"], "art_code": "AN1"}]}])
            return {"code": code, "threads": 1, "steps": [], "usage": []}
        def linker(username, node_a, node_b, settings, store, on_step):
            self.linked.append((username, node_a, node_b))
            return {"steps": []}
        with patch("src.web.load_env"):
            app = create_app(self.store, lambda codes: {}, settings=SETTINGS, initializer=initializer, linker=linker)
        self.client = TestClient(app, base_url="https://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        stack.enter_context(patch("src.web.fetch_history", return_value={"bars": [{"date": "2026-04-02", "close": "9.10", "open": "9"}]}))

    def login(self, name):
        self.client.post("/api/login", json={"username": name, "password": "password"})

    def wait(self, job_id):
        for _ in range(100):
            job = self.client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.02)
        self.fail("任务未结束")

    def test_timeline_flow_and_per_user_additions(self):
        self.assertEqual(self.client.get("/api/timeline/sz002079").status_code, 401)
        self.login("alice")
        self.assertFalse(self.client.get("/api/timeline/sz002079").json()["initialized"])
        self.assertEqual(self.client.post("/api/timeline/sz002079/nodes", json={"art_code": "AN3"}).status_code, 400)
        job = self.wait(self.client.post("/api/timeline/sz002079/init").json()["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.client.post("/api/timeline/sz002079/init").status_code, 400)
        view = self.client.get("/api/timeline/sz002079").json()
        self.assertEqual((view["history"], view["chain"]), ([{"date": "2026-04-02", "close": "9.10"}], None))
        self.assertEqual([c["art_code"] for c in view["candidates"]], ["AN3", "AN4", "AN5"])
        thread_id = view["threads"][0]["id"]
        self.assertEqual(self.client.post("/api/timeline/sz002079/nodes", json={"art_code": "AN9"}).status_code, 400)
        added = self.client.post("/api/timeline/sz002079/nodes", json={"art_code": "AN3", "thread_id": thread_id}).json()["id"]
        self.client.post("/api/timeline/sz002079/nodes", json={"art_code": "AN4"})
        view = self.client.get("/api/timeline/sz002079").json()
        self.assertEqual([len(t["nodes"]) for t in view["threads"]], [2, 1])
        first = view["threads"][0]["nodes"][0]["id"]
        job = self.wait(self.client.post("/api/timeline/links", json={"node_a": first, "node_b": added}).json()["job_id"])
        self.assertEqual((job["status"], self.linked), ("done", [("alice", first, added)]))
        self.store.save_commitments(view["threads"][0]["id"], "sz002079", [
            {"kind": "增持", "content": "增持", "amount": "", "deadline_text": "", "deadline": "2026-10-02", "node_id": first,
             "status": "进行中", "outcome": "", "evidence": [], "unverified": []}], [], "")
        self.store.set_holdings("alice", [{"code": "sz002079"}, {"code": "sh600519"}])
        with patch("src.web.now", return_value=NOW):
            ledger = self.client.get("/api/commitments").json()
        self.assertEqual((ledger["counts"], ledger["missing"]), ({"到期未披露": 1}, ["sh600519"]))
        self.assertEqual(ledger["items"][0]["source_url"], ANNOUNCEMENTS[0]["url"])

        self.client.post("/api/logout")
        self.login("bob")
        view = self.client.get("/api/timeline/sz002079").json()
        self.assertEqual([len(t["nodes"]) for t in view["threads"]], [1])
        self.assertEqual(self.client.post("/api/timeline/links", json={"node_a": first, "node_b": added}).status_code, 404)
        self.assertEqual(self.client.post("/api/timeline/sz002079/nodes", json={"art_code": "AN5", "thread_id": thread_id + 1}).status_code, 404)


if __name__ == "__main__":
    unittest.main()

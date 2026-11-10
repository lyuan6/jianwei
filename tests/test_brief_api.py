import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.config import Settings
from src.storage import Storage
from src.web import create_app

SETTINGS = Settings(mode="api", model="test", base_url="https://example.test", api_key="test")


class BriefApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "t.db")
        with patch("src.accounts.SCRYPT", {"n": 16, "r": 8, "p": 1}):
            for name in ("alice", "bob", "team"):
                self.store.add(name, "password")
                self.store.set_holdings(name, [{"code": "sz002079"}])
        self.entered, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        def generator(codes, settings, store, day, *, on_step):
            on_step([{"code": codes[0], "ref": "R1"}])
            self.entered.set()
            self.release.wait(3)
            result = {"events": [], "steps": [{"ref": "R1"}], "date": day}
            store.save_brief(codes, day, result)
            return result
        with patch("src.web.load_env"):
            self.app = create_app(self.store, lambda codes: {}, settings=SETTINGS, generator=generator)
        self.client = TestClient(self.app, base_url="https://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, name="alice"):
        self.assertEqual(self.client.post("/api/login", json={"username": name, "password": "password"}).status_code, 200)

    def test_endpoints_require_login_and_invalid_dates_are_short_errors(self):
        for path in ("/api/holdings", "/api/brief", "/api/jobs/nope"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/api/brief/generate").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/brief").json(), None)
        self.assertEqual(self.client.get("/api/brief?date=nope").status_code, 400)
        self.assertEqual(self.client.get("/api/holdings").json()["holdings"][0]["code"], "sz002079")

    def test_generation_progress_result_and_cross_account_isolation(self):
        self.login()
        job_id = self.client.post("/api/brief/generate").json()["job_id"]
        self.assertTrue(self.entered.wait(3))
        self.assertEqual(self.client.post("/api/brief/generate").json()["job_id"], job_id)
        self.assertEqual(self.client.get("/api/jobs/" + job_id).json()["steps"][0]["ref"], "R1")
        self.login("bob")
        self.assertEqual(self.client.get("/api/jobs/" + job_id).status_code, 404)
        self.release.set()
        self.app.state.jobs.close()
        self.login()
        self.assertEqual(self.client.get("/api/jobs/" + job_id).json()["status"], "done")
        report = self.client.get("/api/brief").json()
        self.login("bob")
        self.assertEqual(self.client.get("/api/brief").json(), report)

    def test_past_briefs_are_listed_newest_first_for_current_holdings(self):
        self.store.save_brief(["sz002079"], "2026-10-27", {"title": "今日 1 项重点、0 项留意"})
        self.store.save_brief(["sz002079"], "2026-10-28", {"title": "今日 0 项重点、2 项留意"})
        self.store.save_brief(["sh600000"], "2026-10-28", {"title": "其他组合"})
        self.assertEqual(self.client.get("/api/briefs").status_code, 401)
        self.login()
        result = self.client.get("/api/briefs").json()
        self.assertEqual(result["briefs"], [{"date": "2026-10-28", "title": "今日 0 项重点、2 项留意"},
                                            {"date": "2026-10-27", "title": "今日 1 项重点、0 项留意"}])
        self.assertEqual(self.client.get("/api/brief?date=2026-10-27").json()["title"], "今日 1 项重点、0 项留意")

    def test_quota_and_team_exception_and_empty_holdings(self):
        from src.market import now
        day = now().date().isoformat()
        with self.store.db() as db:
            db.executemany("INSERT INTO usage VALUES (?, ?, 'brief', 5)", [("alice", day), ("team", day)])
        self.login()
        self.assertEqual(self.client.post("/api/brief/generate").status_code, 429)
        self.login("team")
        self.assertEqual(self.client.post("/api/brief/generate").status_code, 200)
        self.release.set()
        self.login("bob")
        self.store.set_holdings("bob", [])
        self.assertEqual(self.client.post("/api/brief/generate").status_code, 400)


if __name__ == "__main__":
    unittest.main()

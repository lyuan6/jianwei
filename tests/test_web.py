import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import src.accounts as accounts
from src.accounts import ALPHABET, AccountError, Store, Throttled, generate_password, hash_password, verify_password
from src.market import MarketError, Quote
from src.web import create_app

FAST = {"n": 2 ** 4, "r": 8, "p": 1}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        return self.t


def quote(code):
    return Quote(code, "测试" + code, "10.5", "10", "5", "2026-10-26T15:00:00+08:00", "2026-10-26T15:05:00+08:00")


@patch.object(accounts, "SCRYPT", FAST)
class AccountTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.store = Store(Path(self.dir.name) / "t.db", clock=self.clock)

    def test_generated_password_is_readable_and_mixed(self):
        for _ in range(50):
            pw = generate_password()
            self.assertRegex(pw, r"^[^-]{4}-[^-]{4}-[^-]{4}$")
            raw = pw.replace("-", "")
            self.assertTrue(set(raw) <= set(ALPHABET))
            self.assertFalse(set(raw) & set("0O1lI"))
            self.assertTrue(re.search(r"[a-z]", raw) and re.search(r"[A-Z]", raw) and re.search(r"\d", raw))

    def test_hash_is_salted_and_verifies(self):
        a, b = hash_password("secret"), hash_password("secret")
        self.assertNotEqual(a, b)
        self.assertNotIn("secret", a)
        self.assertTrue(verify_password("secret", a))
        self.assertFalse(verify_password("Secret", a))
        self.assertFalse(verify_password("secret", "not-a-hash"))

    def test_add_reset_disable(self):
        pw = self.store.add("reviewer1")
        with self.assertRaises(AccountError):
            self.store.add("reviewer1")
        for bad in ("Reviewer", "1abc", "a", "a b", "x" * 40):
            with self.assertRaises(AccountError):
                self.store.add(bad)
        token = self.store.authenticate("reviewer1", pw, "1.1.1.1")
        self.assertEqual(self.store.session_user(token), "reviewer1")
        new = self.store.reset("reviewer1")
        self.assertIsNone(self.store.session_user(token))  # reset ends existing sessions
        self.assertIsNone(self.store.authenticate("reviewer1", pw, "1.1.1.1"))
        token = self.store.authenticate("reviewer1", new, "1.1.1.1")
        self.store.set_disabled("reviewer1", True)
        self.assertIsNone(self.store.session_user(token))
        self.assertIsNone(self.store.authenticate("reviewer1", new, "1.1.1.1"))
        self.store.set_disabled("reviewer1", False)
        self.assertTrue(self.store.authenticate("reviewer1", new, "1.1.1.1"))
        with self.assertRaises(AccountError):
            self.store.reset("nobody")
        self.assertEqual([row[0] for row in self.store.list()], ["reviewer1"])

    def test_lockout_per_user_then_expires(self):
        pw = self.store.add("reviewer1")
        for _ in range(accounts.MAX_USER_FAILURES):
            self.assertIsNone(self.store.authenticate("reviewer1", "wrong", "1.1.1.1"))
        with self.assertRaises(Throttled):
            self.store.authenticate("reviewer1", pw, "2.2.2.2")  # locked even with the right password
        self.clock.t += accounts.WINDOW_SECONDS + 1
        self.assertTrue(self.store.authenticate("reviewer1", pw, "1.1.1.1"))

    def test_lockout_per_ip_across_usernames(self):
        pw = self.store.add("reviewer1")
        for i in range(accounts.MAX_IP_FAILURES):
            self.store.authenticate(f"guess{i}", "wrong", "9.9.9.9")
        with self.assertRaises(Throttled):
            self.store.authenticate("reviewer1", pw, "9.9.9.9")
        self.assertTrue(self.store.authenticate("reviewer1", pw, "1.1.1.1"))

    def test_session_expires(self):
        pw = self.store.add("reviewer1")
        token = self.store.authenticate("reviewer1", pw, "1.1.1.1")
        self.clock.t += accounts.SESSION_SECONDS + 1
        self.assertIsNone(self.store.session_user(token))
        self.assertIsNone(self.store.session_user(""))


@patch.object(accounts, "SCRYPT", FAST)
class WebTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("src.web.load_env")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(Path(self.dir.name) / "t.db")
        self.password = self.store.add("reviewer1")
        self.calls = []

        def loader(codes):
            self.calls.append(codes)
            if "sz000001" in codes:
                raise MarketError("行情源暂时不可用，请稍后刷新。")
            return {c: quote(c) for c in codes}

        self.client = TestClient(create_app(self.store, loader), base_url="https://testserver")

    def login(self, password=None):
        return self.client.post("/api/login", json={"username": "reviewer1", "password": password or self.password})

    def test_index_served_with_security_headers(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("见微", res.text)
        self.assertNotRegex(res.text, r"<script>(?!</script>)")  # no inline script; CSP would block it
        self.assertIn("default-src 'self'", res.headers["content-security-policy"])
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_api_requires_login(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/quotes?symbols=sh000001").status_code, 401)
        self.assertEqual(self.calls, [])

    def test_login_cookie_me_logout(self):
        self.assertEqual(self.login("wrong").status_code, 401)
        res = self.client.post("/api/login", json={"username": " Reviewer1 ", "password": self.password})
        self.assertEqual(res.status_code, 200)
        cookie = res.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=lax"):
            self.assertIn(flag, cookie)
        self.assertEqual(self.client.get("/api/me").json()["user"], "reviewer1")
        self.assertEqual(self.client.get("/api/me").headers["cache-control"], "no-store")
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_lockout_returns_429(self):
        for _ in range(accounts.MAX_USER_FAILURES):
            self.login("wrong")
        res = self.login()
        self.assertEqual(res.status_code, 429)
        self.assertIn("15 分钟", res.json()["detail"])

    def test_quotes_cached_validated_and_errors_mapped(self):
        self.login()
        res = self.client.get("/api/quotes?symbols=sz300750,sh000001")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["quotes"]["sz300750"]["price"], "10.5")
        self.client.get("/api/quotes?symbols=sh000001,sz300750")  # same set in another order hits the cache
        self.assertEqual(self.calls, [("sh000001", "sz300750")])
        self.assertEqual(self.client.get("/api/quotes?symbols=300750").status_code, 400)  # bare code is ambiguous
        many = ",".join(f"sh6000{i:02d}" for i in range(13))
        self.assertEqual(self.client.get(f"/api/quotes?symbols={many}").status_code, 400)
        self.assertEqual(self.client.get("/api/quotes?symbols=sz000001").status_code, 502)

    def test_cookie_secure_can_be_turned_off_for_local_http(self):
        with patch.dict("os.environ", {"COOKIE_SECURE": "0"}):
            client = TestClient(create_app(self.store, lambda codes: {}))
        res = client.post("/api/login", json={"username": "reviewer1", "password": self.password})
        self.assertNotIn("secure", res.headers["set-cookie"].lower())

    def test_oversized_password_rejected_before_hashing(self):
        res = self.client.post("/api/login", json={"username": "reviewer1", "password": "x" * 500})
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main()

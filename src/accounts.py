"""Reviewer accounts, sessions and login throttling in SQLite. Only salted scrypt hashes are stored."""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# Readable alphabet: no 0/O, 1/l/I, so reviewers can type passwords from a document.
ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
USERNAME = re.compile(r"[a-z][a-z0-9_-]{1,31}")
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
SESSION_SECONDS = 7 * 24 * 3600
WINDOW_SECONDS = 15 * 60
MAX_USER_FAILURES = 5
MAX_IP_FAILURES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_failures (username TEXT NOT NULL, ip TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS failures_at ON login_failures (at);
"""


def default_store():
    from src.config import data_dir
    path = data_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Store(path / "jianwei.db")


class AccountError(ValueError):
    pass


class Throttled(Exception):
    pass


def generate_password():
    while True:
        raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
        if any(c.islower() for c in raw) and any(c.isupper() for c in raw) and any(c.isdigit() for c in raw):
            return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def hash_password(password, params=None):
    params = params or SCRYPT
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **params)
    # Parameters are stored with the hash so they can be raised later without breaking old accounts.
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **params, salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=len(expected),
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dummy = None
        with self.db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def now(self):
        return int(self.clock())

    # Accounts

    def add(self, username, password=None):
        if not USERNAME.fullmatch(username):
            raise AccountError("用户名只能用小写字母、数字、下划线和短横线，以字母开头，2 到 32 位。")
        password = password or generate_password()
        try:
            with self.db() as db:
                db.execute("INSERT INTO accounts (username, password_hash, disabled, created_at) VALUES (?, ?, 0, ?)", (username, hash_password(password), self.now()))
        except sqlite3.IntegrityError:
            raise AccountError(f"账号 {username} 已存在。") from None
        return password

    def reset(self, username, password=None):
        password = password or generate_password()
        with self.db() as db:
            if not db.execute("UPDATE accounts SET password_hash = ? WHERE username = ?",
                              (hash_password(password), username)).rowcount:
                raise AccountError(f"账号 {username} 不存在。")
            db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        return password

    def set_disabled(self, username, disabled):
        with self.db() as db:
            if not db.execute("UPDATE accounts SET disabled = ? WHERE username = ?",
                              (int(disabled), username)).rowcount:
                raise AccountError(f"账号 {username} 不存在。")
            if disabled:
                db.execute("DELETE FROM sessions WHERE username = ?", (username,))

    def list(self):
        with self.db() as db:
            return db.execute("""SELECT a.username, a.disabled, a.created_at,
                (SELECT COUNT(*) FROM sessions s WHERE s.username = a.username AND s.expires_at > ?)
                FROM accounts a ORDER BY a.username""", (self.now(),)).fetchall()

    # Login

    def authenticate(self, username, password, ip):
        now = self.now()
        since = now - WINDOW_SECONDS
        with self.db() as db:
            db.execute("DELETE FROM login_failures WHERE at < ?", (since,))
            by_user = db.execute("SELECT COUNT(*) FROM login_failures WHERE username = ? AND at >= ?",
                                 (username, since)).fetchone()[0]
            by_ip = db.execute("SELECT COUNT(*) FROM login_failures WHERE ip = ? AND at >= ?",
                               (ip, since)).fetchone()[0]
            if by_user >= MAX_USER_FAILURES or by_ip >= MAX_IP_FAILURES:
                raise Throttled
            row = db.execute("SELECT password_hash, disabled FROM accounts WHERE username = ?", (username,)).fetchone()
        if row is None or row[1]:
            # Same work for unknown or disabled accounts, so response time does not reveal which names exist.
            self._dummy = self._dummy or hash_password("unused")
            verify_password(password, self._dummy)
            ok = False
        else:
            ok = verify_password(password, row[0])
        if not ok:
            with self.db() as db:
                db.execute("INSERT INTO login_failures VALUES (?, ?, ?)", (username, ip, now))
            return None
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token_hash(token), username, now + SESSION_SECONDS))
        return token

    def session_user(self, token):
        if not token:
            return None
        with self.db() as db:
            row = db.execute("""SELECT s.username FROM sessions s JOIN accounts a ON a.username = s.username
                WHERE s.token_hash = ? AND s.expires_at > ? AND a.disabled = 0""",
                             (token_hash(token), self.now())).fetchone()
        return row[0] if row else None

    def logout(self, token):
        if token:
            with self.db() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

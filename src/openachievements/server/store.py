"""Server storage: accounts, device tokens, and each profile's event stream.

SQLite, one file (ADR 0002): a self-hosted server should be one container and
one volume, with nothing else to run. The same schema is plain enough to move
to PostgreSQL for a large hosted instance without changing the sync protocol.

The server is a sync peer, never the only copy. It stores events exactly as
the client wrote them (the hash is checked on the way in), numbers them in the
order they arrived, and hands them out by that number.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path

from .. import events as ev

_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  privacy TEXT NOT NULL DEFAULT '{"public": false}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  device_id TEXT,
  device_name TEXT,
  kind TEXT NOT NULL DEFAULT 'device',
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS profiles (
  profile_id TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
  bound_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  body TEXT NOT NULL,
  received_at TEXT NOT NULL,
  UNIQUE (profile_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_profile_seq ON events(profile_id, seq);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
"""

SCHEMA_VERSION = 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Conflict(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        db = self._db()
        db.executescript(_SCHEMA)
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", (SCHEMA_VERSION, ev.now()))
        db.commit()

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 15000")
            self._local.db = db
        return db

    def ready(self) -> bool:
        return self._db().execute("SELECT 1").fetchone()[0] == 1

    # ---- accounts and tokens ----------------------------------------------------

    def create_account(self, username: str, password: str) -> int:
        try:
            cur = self._db().execute(
                "INSERT INTO accounts (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, hash_password(password), ev.now()))
        except sqlite3.IntegrityError:
            raise Conflict("username_taken", "that username is taken") from None
        return cur.lastrowid

    def account_count(self) -> int:
        return self._db().execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    def verify_login(self, username: str, password: str) -> int | None:
        row = self._db().execute("SELECT id, password_hash FROM accounts WHERE username = ?", (username,)).fetchone()
        if row is None:
            check_password(password, hash_password("timing-equaliser"))
            return None
        return row["id"] if check_password(password, row["password_hash"]) else None

    def issue_token(self, account_id: int, device_id: str | None, device_name: str | None, kind: str = "device") -> str:
        token = "oa_" + secrets.token_urlsafe(32)
        self._db().execute(
            "INSERT INTO tokens (token_hash, account_id, device_id, device_name, kind, created_at) VALUES (?,?,?,?,?,?)",
            (_token_hash(token), account_id, device_id, device_name, kind, ev.now()))
        return token

    def account_for_token(self, token: str) -> sqlite3.Row | None:
        db = self._db()
        row = db.execute(
            "SELECT t.token_hash, t.device_id, t.device_name, t.kind, a.id AS account_id, a.username, a.privacy "
            "FROM tokens t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL", (_token_hash(token),)).fetchone()
        if row is not None:
            db.execute("UPDATE tokens SET last_used_at = ? WHERE token_hash = ?", (ev.now(), row["token_hash"]))
        return row

    def revoke_token(self, token_hash: str) -> None:
        self._db().execute("UPDATE tokens SET revoked_at = ? WHERE token_hash = ?", (ev.now(), token_hash))

    def devices(self, account_id: int) -> list[dict]:
        rows = self._db().execute(
            "SELECT device_id, device_name, kind, created_at, last_used_at, revoked_at FROM tokens "
            "WHERE account_id = ? ORDER BY created_at", (account_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke_device(self, account_id: int, device_id: str) -> int:
        cur = self._db().execute(
            "UPDATE tokens SET revoked_at = ? WHERE account_id = ? AND device_id = ? AND revoked_at IS NULL",
            (ev.now(), account_id, device_id))
        return cur.rowcount

    def set_privacy(self, account_id: int, privacy: dict) -> None:
        self._db().execute("UPDATE accounts SET privacy = ? WHERE id = ?", (json.dumps(privacy), account_id))

    def public_profile(self, username: str) -> tuple[int, dict] | None:
        row = self._db().execute("SELECT id, privacy FROM accounts WHERE username = ?", (username,)).fetchone()
        if row is None:
            return None
        privacy = json.loads(row["privacy"])
        return (row["id"], privacy) if privacy.get("public") else None

    def delete_account(self, account_id: int) -> None:
        """Hosted data only. The client's local profile is never touched."""
        self._db().execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    # ---- profiles and events ----------------------------------------------------------

    def profile_for(self, account_id: int) -> str | None:
        row = self._db().execute("SELECT profile_id FROM profiles WHERE account_id = ?", (account_id,)).fetchone()
        return row["profile_id"] if row else None

    def bind_profile(self, account_id: int, profile_id: str) -> None:
        """One profile per account. The first sync decides which."""
        bound = self.profile_for(account_id)
        if bound == profile_id:
            return
        if bound is not None:
            raise Conflict("profile_mismatch", "this account already syncs a different profile")
        try:
            self._db().execute("INSERT INTO profiles VALUES (?, ?, ?)", (profile_id, account_id, ev.now()))
        except sqlite3.IntegrityError:
            raise Conflict("profile_taken", "that profile is already synced by another account") from None

    _UNLOCKS = ("json_extract(e.body, '$.event_type') = 'achievement.unlocked' "
                "AND coalesce(json_extract(e.body, '$.payload.provenance'), '') != 'manual'")

    def stats(self, top: int = 10) -> dict:
        """Totals for the signed-out page, and public profiles by unlock count.
        Only accounts that chose to be public are named. Revocations are rare
        and not subtracted here; a profile's own page is the exact count."""
        db = self._db()
        accounts = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        public = db.execute("SELECT COUNT(*) FROM accounts WHERE json_extract(privacy, '$.public') = 1").fetchone()[0]
        unlocks = db.execute(f"SELECT COUNT(*) FROM events e WHERE {self._UNLOCKS}").fetchone()[0]
        games = db.execute("SELECT COUNT(DISTINCT json_extract(e.body, '$.game_id')) FROM events e "
                           "WHERE json_extract(e.body, '$.event_type') = 'game.registered'").fetchone()[0]
        leaders = [{"username": r["username"], "unlocks": r["n"]} for r in db.execute(
            f"SELECT a.username, COUNT(*) AS n FROM accounts a JOIN profiles p ON p.account_id = a.id "
            f"JOIN events e ON e.profile_id = p.profile_id WHERE json_extract(a.privacy, '$.public') = 1 "
            f"AND {self._UNLOCKS} GROUP BY a.id ORDER BY n DESC, a.username LIMIT ?", (top,))]
        return {"accounts": accounts, "public_profiles": public, "unlocks": unlocks, "games": games,
                "top": leaders}

    def event_count(self, profile_id: str) -> int:
        return self._db().execute("SELECT COUNT(*) FROM events WHERE profile_id = ?", (profile_id,)).fetchone()[0]

    def store_events(self, profile_id: str, batch: list[dict],
                     max_events: int | None = None) -> tuple[list[str], list[str], list[str]]:
        """Insert in one transaction. Returns (newly stored, already known, over
        quota). Known ids count as accepted, because re-sending is always safe;
        only genuinely new events count against `max_events`, and the count is
        taken inside the same write lock so two devices cannot both slip past."""
        db = self._db()
        stored, known, over = [], [], []
        db.execute("BEGIN IMMEDIATE")
        try:
            room = None
            if max_events is not None:
                count = db.execute("SELECT COUNT(*) FROM events WHERE profile_id = ?", (profile_id,)).fetchone()[0]
                room = max_events - count
            for event in batch:
                exists = db.execute("SELECT 1 FROM events WHERE profile_id = ? AND event_id = ?",
                                    (profile_id, event["event_id"])).fetchone()
                if exists:
                    known.append(event["event_id"])
                    continue
                if room is not None and room <= 0:
                    over.append(event["event_id"])
                    continue
                cur = db.execute(
                    "INSERT OR IGNORE INTO events (profile_id, event_id, device_id, body, received_at) VALUES (?,?,?,?,?)",
                    (profile_id, event["event_id"], event["device_id"],
                     json.dumps(event, sort_keys=True, ensure_ascii=False), ev.now()))
                if cur.rowcount:
                    stored.append(event["event_id"])
                    if room is not None:
                        room -= 1
                else:
                    known.append(event["event_id"])
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return stored, known, over

    def events_after(self, profile_id: str, seq: int, limit: int) -> list[tuple[int, dict]]:
        rows = self._db().execute(
            "SELECT seq, body FROM events WHERE profile_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (profile_id, seq, limit)).fetchall()
        return [(r["seq"], json.loads(r["body"])) for r in rows]

    def all_events(self, profile_id: str) -> list[dict]:
        rows = self._db().execute("SELECT body FROM events WHERE profile_id = ? ORDER BY seq", (profile_id,))
        return [json.loads(r["body"]) for r in rows]


def data_dir() -> Path:
    return Path(os.environ.get("OA_DATA_DIR", "./data"))

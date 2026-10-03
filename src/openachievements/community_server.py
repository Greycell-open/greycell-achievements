"""greycell.app's community stats service: anonymous counts in, one snapshot a day out.

The apps that share stats (community.py; on by default, off in Settings,
Privacy) post small batches of items: what happened (an unlock, a Platinum, a
game added, put on Backlog or Completed, a new install), on which platform,
in which minute, and for Steam games the app id, the achievement's Steam name
and, for a Platinum, the hours played. No player, no install id, no account.

Shown publicly: one snapshot, rebuilt every 24 hours (owner, 2026-10-03:
"one refresh every 24h"), with the last 24 and 48 hours and all time. Names
come only from Steam's public data, looked up here, never from what a client
sent, so nobody can put text on greycell.app; an achievement Steam does not
list is never shown. Other platforms count in the totals, unnamed.

No addresses are stored or logged. A sender's address is used only in memory,
hashed with a key made at each start, to cap how much one sender can post in
an hour. Single items are kept 60 days for the windows; totals forever.

    uvicorn openachievements.community_server:app --host 127.0.0.1 --port 8794
    COMMUNITY_DB=/data/community.sqlite  COMMUNITY_SNAPSHOT_EVERY=86400
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import statistics
import threading
import time
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DB = os.environ.get("COMMUNITY_DB", "community.sqlite")
SNAPSHOT_EVERY = float(os.environ.get("COMMUNITY_SNAPSHOT_EVERY", "86400"))
KEEP_DAYS = 60
MAX_BODY = 64 * 1024
MAX_ITEMS = 500
PER_SENDER_HOUR = 1500
KINDS = ("install", "unlock", "platinum", "added", "backlog", "completed")
PLATFORMS = ("steam", "playstation", "xbox", "gog", "retroachievements", "pc", "windows", "linux")
_API = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
SCHEMA = "https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid={appid}&language=english"
DETAILS = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
_salt = secrets.token_bytes(16)
_senders: dict[str, list] = {}
_lock = threading.Lock()
_snapshot: dict = {"at": 0.0, "data": None}


def connect(path: str = DB) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items (at INTEGER NOT NULL, kind TEXT NOT NULL, platform TEXT NOT NULL,
                                          appid INTEGER, api TEXT, minutes INTEGER);
        CREATE INDEX IF NOT EXISTS items_at ON items(at);
        CREATE TABLE IF NOT EXISTS totals (name TEXT PRIMARY KEY, n INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS games (appid INTEGER PRIMARY KEY, title TEXT, checked INTEGER, ok INTEGER);
        CREATE TABLE IF NOT EXISTS achievements (appid INTEGER, api TEXT, name TEXT, pct REAL,
                                                 PRIMARY KEY (appid, api));
        CREATE TABLE IF NOT EXISTS completions (appid INTEGER, minutes INTEGER, at INTEGER);
    """)
    return db


def _epoch(text: str) -> int | None:
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def clean(item: dict, now: float) -> dict | None:
    """One item as stored, or None when it is not one this service takes."""
    if not isinstance(item, dict) or item.get("kind") not in KINDS or item.get("platform") not in PLATFORMS:
        return None
    at = _epoch(item.get("at"))
    if at is None or not now - 2 * 86400 <= at <= now + 600:          # recent things only
        return None
    appid, api, minutes = item.get("appid"), item.get("api"), item.get("minutes")
    if appid is not None and (not isinstance(appid, int) or isinstance(appid, bool) or not 0 < appid < 10**9):
        return None
    if api is not None and (appid is None or not isinstance(api, str) or not _API.match(api)):
        return None
    if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= 600000):
        return None
    return {"at": at - at % 60, "kind": item["kind"], "platform": item["platform"], "appid": appid,
            "api": api, "minutes": minutes if item["kind"] == "platinum" else None}


def allow(sender: str, count: int, now: float) -> bool:
    """At most PER_SENDER_HOUR items an hour from one sender; in memory only."""
    key = hashlib.sha256(_salt + sender.encode()).hexdigest()[:16]
    with _lock:
        recent = [(t, n) for t, n in _senders.get(key, []) if now - t < 3600]
        if sum(n for _, n in recent) + count > PER_SENDER_HOUR:
            _senders[key] = recent
            return False
        _senders[key] = recent + [(now, count)]
        if len(_senders) > 50000:                    # never grows without bound
            _senders.clear()
        return True


def store(db: sqlite3.Connection, items: list[dict]) -> int:
    with _lock:
        db.executemany("INSERT INTO items VALUES (:at, :kind, :platform, :appid, :api, :minutes)", items)
        for it in items:
            for name in (it["kind"], f"{it['kind']}:{it['platform']}"):
                db.execute("INSERT INTO totals VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET n = n + 1", (name,))
            if it["kind"] == "platinum" and it["appid"] and it["minutes"]:
                db.execute("INSERT INTO completions VALUES (?, ?, ?)", (it["appid"], it["minutes"], it["at"]))
        db.commit()
    return len(items)


# ---- Steam names, looked up here ----------------------------------------------------------

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "GreycellCommunityStats (+https://greycell.app)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read(4 * 1024 * 1024))


def resolve(db: sqlite3.Connection, appid: int, get=_get_json, now: float | None = None) -> bool:
    """Look up a Steam game's title and achievements. True when Steam knows it."""
    now = time.time() if now is None else now
    try:
        schema = get(SCHEMA.format(appid=appid)).get("response") or {}
        details = (get(DETAILS.format(appid=appid)).get(str(appid)) or {})
    except Exception:  # noqa: BLE001 - Steam unreachable: asked again later
        return False
    title = ((details.get("data") or {}).get("name") if details.get("success") else None)
    rows = [(appid, a.get("internal_name"), a.get("localized_name"), a.get("player_percent_unlocked"))
            for a in schema.get("achievements") or [] if isinstance(a, dict) and a.get("internal_name")]
    with _lock:
        db.execute("INSERT OR REPLACE INTO games VALUES (?, ?, ?, ?)", (appid, title, int(now), int(bool(title))))
        db.executemany("INSERT OR REPLACE INTO achievements VALUES (?, ?, ?, ?)",
                       [(a, i, str(n or i)[:200], float(p) if p is not None else None) for a, i, n, p in rows])
        db.commit()
    return bool(title)


def pending(db: sqlite3.Connection, now: float) -> list[int]:
    """Steam games seen but not looked up, or looked up over a week ago."""
    rows = db.execute("""SELECT DISTINCT i.appid FROM items i LEFT JOIN games g ON g.appid = i.appid
                         WHERE i.appid IS NOT NULL AND i.platform = 'steam'
                           AND (g.appid IS NULL OR g.checked < ?) LIMIT 50""", (int(now - 7 * 86400),))
    return [r[0] for r in rows]


def resolver(db: sqlite3.Connection, stop: threading.Event, pace: float = 1.5) -> None:
    while not stop.is_set():
        for appid in pending(db, time.time()):
            resolve(db, appid)
            if stop.wait(pace):
                return
        stop.wait(30)


# ---- the daily snapshot -------------------------------------------------------------------

def _named(db, sql: str, args: tuple) -> list[dict]:
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.row_factory = None


def snapshot(db: sqlite3.Connection, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    day, two = int(now - 86400), int(now - 2 * 86400)
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")   # noqa: E731

    def counts(since: int) -> dict:
        rows = db.execute("SELECT kind, COUNT(*) FROM items WHERE at >= ? GROUP BY kind", (since,))
        out = {k: 0 for k in KINDS}
        out.update(dict(rows.fetchall()))
        return out
    totals = {k: 0 for k in KINDS}
    totals.update({n: c for n, c in db.execute("SELECT name, n FROM totals WHERE name NOT LIKE '%:%'")})
    platforms = dict(db.execute("""SELECT platform, COUNT(*) FROM items WHERE kind = 'unlock' AND at >= ?
                                   GROUP BY platform ORDER BY 2 DESC""", (day,)).fetchall())
    shown = """JOIN games g ON g.appid = i.appid AND g.ok = 1"""
    latest = _named(db, f"""SELECT a.name, g.title AS game, i.appid, i.at, a.pct FROM items i {shown}
                            JOIN achievements a ON a.appid = i.appid AND a.api = i.api
                            WHERE i.kind = 'unlock' ORDER BY i.at DESC LIMIT 12""", ())
    platinums = _named(db, f"""SELECT g.title AS game, i.appid, i.at, i.minutes FROM items i {shown}
                               WHERE i.kind = 'platinum' ORDER BY i.at DESC LIMIT 10""", ())
    top_games = _named(db, f"""SELECT g.title AS game, i.appid, COUNT(*) AS unlocks FROM items i {shown}
                               WHERE i.kind = 'unlock' AND i.at >= ? GROUP BY i.appid
                               ORDER BY unlocks DESC LIMIT 10""", (two,))
    most_earned = _named(db, f"""SELECT a.name, g.title AS game, i.appid, COUNT(*) AS times FROM items i {shown}
                                 JOIN achievements a ON a.appid = i.appid AND a.api = i.api
                                 WHERE i.kind = 'unlock' AND i.at >= ? GROUP BY i.appid, i.api
                                 ORDER BY times DESC LIMIT 6""", (two,))
    rarest = _named(db, f"""SELECT a.name, g.title AS game, i.appid, a.pct, MAX(i.at) AS at FROM items i {shown}
                            JOIN achievements a ON a.appid = i.appid AND a.api = i.api
                            WHERE i.kind = 'unlock' AND i.at >= ? AND a.pct IS NOT NULL
                            GROUP BY i.appid, i.api ORDER BY a.pct ASC LIMIT 6""", (two,))
    added = _named(db, f"""SELECT g.title AS game, i.appid, COUNT(*) AS times FROM items i {shown}
                           WHERE i.kind IN ('added', 'backlog') AND i.at >= ? GROUP BY i.appid
                           ORDER BY times DESC LIMIT 8""", (int(now - 7 * 86400),))
    times = {}
    for appid, minutes in db.execute("SELECT appid, minutes FROM completions"):
        times.setdefault(appid, []).append(minutes)
    titles = dict(db.execute("SELECT appid, title FROM games WHERE ok = 1").fetchall())
    to_100 = sorted(({"game": titles[a], "appid": a, "hours": round(statistics.median(m) / 60, 1), "reports": len(m)}
                     for a, m in times.items() if len(m) >= 3 and a in titles),
                    key=lambda r: -r["reports"])[:10]
    for row in latest + platinums + rarest:
        row["at"] = iso(row["at"])
    for row in platinums:
        minutes = row.pop("minutes")
        row["hours"] = round(minutes / 60, 1) if minutes else None
    return {"updated": iso(now), "next": iso(now + SNAPSHOT_EVERY), "totals": totals,
            "last_24h": counts(day), "last_48h": counts(two), "platforms_24h": platforms,
            "latest_unlocks": latest, "latest_platinums": platinums, "top_games_48h": top_games,
            "most_earned_48h": most_earned, "rarest_48h": rarest, "most_added_7d": added, "time_to_100": to_100}


def prune(db: sqlite3.Connection, now: float) -> None:
    with _lock:
        db.execute("DELETE FROM items WHERE at < ?", (int(now - KEEP_DAYS * 86400),))
        db.commit()


# ---- the service ---------------------------------------------------------------------------

def create_app(db_path: str = DB, start_resolver: bool = True) -> FastAPI:
    db = connect(db_path)
    app = FastAPI(title="Greycell community stats", docs_url=None, redoc_url=None, openapi_url=None)
    stop = threading.Event()
    if start_resolver:
        threading.Thread(target=resolver, args=(db, stop), daemon=True, name="steam-names").start()

    @app.post("/api/achievements/events")
    async def events(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "too large")
        try:
            items = json.loads(body).get("items")
        except (ValueError, AttributeError):
            raise HTTPException(400, "not JSON") from None
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise HTTPException(400, "items")
        now = time.time()
        sender = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
        if not allow(sender, len(items), now):
            raise HTTPException(429, "later")
        kept = [c for c in (clean(i, now) for i in items) if c]
        return {"stored": store(db, kept)}

    @app.get("/api/achievements/live")
    def live():
        now = time.time()
        with _lock:
            data = _snapshot["data"]
            empty = data is not None and not any(data["totals"].values())
            fresh = data is not None and now - _snapshot["at"] < SNAPSHOT_EVERY
        if fresh and empty and db.execute("SELECT 1 FROM items LIMIT 1").fetchone():
            fresh = False                                  # the first data arrived: no empty day
        if not fresh:
            prune(db, now)
            data = snapshot(db, now)
            with _lock:
                _snapshot.update(at=now, data=data)
        return JSONResponse(_snapshot["data"], headers={"Cache-Control": "public, max-age=600"})

    @app.get("/api/achievements/healthz")
    def healthz():
        return {"ok": True}

    app.state.db, app.state.stop = db, stop
    return app


app = create_app() if os.environ.get("COMMUNITY_DB") else None

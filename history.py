"""A small SQLite record of what the sorter did, shown by the web UI."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,            -- sorted | review | quarantined | removed | rejected | allowed | discarded
    name TEXT NOT NULL,            -- torrent / book name as shown in the log
    torrent_hash TEXT DEFAULT '',
    author TEXT DEFAULT '', series TEXT DEFAULT '', sequence TEXT DEFAULT '', title TEXT DEFAULT '',
    dest TEXT DEFAULT '',          -- library, review or quarantine folder
    reason TEXT DEFAULT '',        -- why (review/quarantine) or the Audible match (sorted)
    tags TEXT DEFAULT '',          -- what the tags said
    files INTEGER DEFAULT 0,
    size INTEGER DEFAULT 0,
    candidate TEXT DEFAULT ''      -- review: closest Audible match as JSON (offered by "Accept match")
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_hash ON events(torrent_hash);
CREATE TABLE IF NOT EXISTS placements (   -- hard links / copies made for a torrent that's still seeding
    torrent_hash TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS placements_hash ON placements(torrent_hash);
"""


class History:
    def __init__(self, path: Path, readonly: bool = False):
        self.path = path
        self.readonly = readonly          # dry run: read what's there, record nothing
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            cols = {r["name"] for r in db.execute("PRAGMA table_info(events)")}
            if "candidate" not in cols:  # database from an earlier version
                db.execute("ALTER TABLE events ADD COLUMN candidate TEXT DEFAULT ''")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------------ writing
    def record(self, kind: str, name: str, **fields) -> None:
        if self.readonly:
            return
        cols = ["ts", "kind", "name", *fields]
        vals = [time.time(), kind, name, *("" if v is None else v for v in fields.values())]
        with self.lock, self._db() as db:
            db.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)

    def add_placements(self, torrent_hash: str, pairs) -> None:
        if self.readonly or not torrent_hash:
            return
        with self.lock, self._db() as db:
            db.executemany("INSERT INTO placements VALUES (?, ?, ?)",
                           [(torrent_hash, str(s), str(d)) for s, d in pairs])

    def move_placements(self, torrent_hash: str, moves) -> None:
        """Files placed for a torrent were moved (review -> library): follow them."""
        if self.readonly or not torrent_hash:
            return
        with self.lock, self._db() as db:
            db.executemany("UPDATE placements SET dst = ? WHERE torrent_hash = ? AND dst = ?",
                           [(str(new), torrent_hash, str(old)) for old, new in moves])

    # ------------------------------------------------------------------ reading
    def event(self, event_id: int) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(r) if r else None

    def rejected(self, torrent_hash: str) -> bool:
        """You rejected this torrent's review copy: it may be deleted without checking a library copy."""
        if not torrent_hash:
            return False
        with self._db() as db:
            return db.execute("SELECT 1 FROM events WHERE kind = 'rejected' AND torrent_hash = ? LIMIT 1",
                              (torrent_hash,)).fetchone() is not None

    def placements(self, torrent_hash: str) -> list[tuple[str, str]]:
        with self._db() as db:
            rows = db.execute("SELECT src, dst FROM placements WHERE torrent_hash = ?", (torrent_hash,))
            return [(r["src"], r["dst"]) for r in rows]

    def pending_reviews(self) -> list[dict]:
        """Review items whose folder is still in the review folder and that weren't sorted since."""
        with self._db() as db:
            rows = db.execute("""
                SELECT * FROM events e WHERE kind = 'review'
                  AND id = (SELECT MAX(id) FROM events WHERE kind = 'review' AND dest = e.dest)
                  AND NOT EXISTS (SELECT 1 FROM events s WHERE s.kind = 'sorted'
                                  AND s.name = e.name AND s.id > e.id)
                ORDER BY ts DESC""").fetchall()
        return [dict(r) for r in rows if r["dest"] and Path(r["dest"]).exists()]

    def history(self, query: str = "", page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
        where, args = "e.kind = 'sorted'", []
        if query:
            like = f"%{query}%"
            where += " AND (e.author LIKE ? OR e.series LIKE ? OR e.title LIKE ? OR e.name LIKE ?)"
            args = [like] * 4
        with self._db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM events e WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"""
                SELECT e.*, (SELECT MAX(r.ts) FROM events r WHERE r.kind = 'removed'
                             AND e.torrent_hash != '' AND r.torrent_hash = e.torrent_hash) AS removed_ts
                FROM events e WHERE {where} ORDER BY e.ts DESC LIMIT ? OFFSET ?""",
                              [*args, per_page, (max(page, 1) - 1) * per_page]).fetchall()
        return [dict(r) for r in rows], total

    def sorted_books(self) -> list[dict]:
        """The latest 'sorted' event for each library folder."""
        with self._db() as db:
            rows = db.execute("""SELECT * FROM events e WHERE kind = 'sorted'
                                 AND id = (SELECT MAX(id) FROM events WHERE kind = 'sorted' AND dest = e.dest)
                                 ORDER BY ts""").fetchall()
        return [dict(r) for r in rows]

    OUTCOME = """(SELECT o.kind FROM events o WHERE o.kind IN ('allowed', 'discarded')
                  AND o.dest = e.dest AND o.id > e.id ORDER BY o.id DESC LIMIT 1)"""

    def quarantined(self, limit: int = 200) -> list[dict]:
        """Quarantine events, newest first, each with 'outcome': 'allowed', 'discarded' or None (waiting)."""
        with self._db() as db:
            rows = db.execute(f"SELECT e.*, {self.OUTCOME} AS outcome FROM events e "
                              "WHERE e.kind = 'quarantined' ORDER BY e.ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def quarantine_outcome(self, event: dict) -> str | None:
        with self._db() as db:
            r = db.execute(f"SELECT {self.OUTCOME} FROM events e WHERE e.id = ?", (event["id"],)).fetchone()
        return r[0] if r else None

    def counts(self) -> dict[str, int]:
        with self._db() as db:
            rows = db.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind").fetchall()
        return {r["kind"]: r["n"] for r in rows}

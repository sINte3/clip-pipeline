"""Учёт в SQLite: исходники → клипы → публикации → замеры просмотров.

Это источник правды для ворот G1–G3: доля роликов выше порога, средний охват,
начисления по кампаниям. Экспорт в CSV — для таблицы в Google Drive.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT,
    duration_s REAL,
    width INTEGER,
    height INTEGER,
    transcript_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, origin)
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    campaign_id TEXT NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    hook TEXT,
    score REAL,
    llm_score REAL,
    path TEXT,
    status TEXT NOT NULL DEFAULT 'rendered',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    account TEXT NOT NULL,
    platform TEXT NOT NULL,
    url TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'live',
    notes TEXT,
    UNIQUE(url)
);
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    ts TEXT NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    received_at TEXT NOT NULL,
    method TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_stats_post ON stats(post_id, ts);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class PostStat:
    post_id: int
    clip_id: int
    campaign_id: str
    account: str
    platform: str
    url: str
    posted_at: str
    status: str
    views: int
    hook: str


class Tracker:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- источники -------------------------------------------------------
    def upsert_source(self, campaign_id: str, origin: str, path: str, title: str, duration_s: float,
                      width: int, height: int, transcript_path: Optional[str]) -> int:
        with self._conn() as c:
            row = c.execute("SELECT id FROM sources WHERE campaign_id=? AND origin=?", (campaign_id, origin)).fetchone()
            if row:
                c.execute("UPDATE sources SET path=?, title=?, duration_s=?, width=?, height=?, transcript_path=COALESCE(?, transcript_path) WHERE id=?",
                          (path, title, duration_s, width, height, transcript_path, row["id"]))
                return int(row["id"])
            cur = c.execute(
                "INSERT INTO sources(campaign_id, origin, path, title, duration_s, width, height, transcript_path, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (campaign_id, origin, path, title, duration_s, width, height, transcript_path, now_iso()))
            return int(cur.lastrowid)

    def get_source(self, source_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def list_sources(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM sources ORDER BY id"))

    # --- клипы -----------------------------------------------------------
    def add_clip(self, source_id: int, campaign_id: str, start_s: float, end_s: float, hook: str, score: float,
                 llm_score: Optional[float], path: Optional[str]) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO clips(source_id, campaign_id, start_s, end_s, hook, score, llm_score, path, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source_id, campaign_id, start_s, end_s, hook, score, llm_score, path, "rendered", now_iso()))
            return int(cur.lastrowid)

    def set_clip_path(self, clip_id: int, path: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE clips SET path=? WHERE id=?", (path, clip_id))

    def set_clip_status(self, clip_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE clips SET status=? WHERE id=?", (status, clip_id))

    # --- публикации и замеры -------------------------------------------
    def add_post(self, clip_id: int, account: str, platform: str, url: str, notes: str = "") -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO posts(clip_id, account, platform, url, posted_at, status, notes) VALUES (?,?,?,?,?,?,?)",
                            (clip_id, account, platform, url, now_iso(), "live", notes))
            c.execute("UPDATE clips SET status='posted' WHERE id=?", (clip_id,))
            return int(cur.lastrowid)

    def set_post_status(self, post_id: int, status: str, notes: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE posts SET status=?, notes=COALESCE(NULLIF(?, ''), notes) WHERE id=?", (status, notes, post_id))

    def add_stat(self, post_id: int, views: int, likes: int = 0, comments: int = 0, shares: int = 0,
                 ts: Optional[str] = None) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO stats(post_id, ts, views, likes, comments, shares) VALUES (?,?,?,?,?,?)",
                            (post_id, ts or now_iso(), views, likes, comments, shares))
            return int(cur.lastrowid)

    def import_stats_csv(self, path: Path) -> int:
        """CSV с колонками post_id,views[,likes,comments,shares,ts]."""
        n = 0
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                self.add_stat(int(row["post_id"]), int(row["views"]), int(row.get("likes") or 0),
                              int(row.get("comments") or 0), int(row.get("shares") or 0), row.get("ts") or None)
                n += 1
        return n

    def add_payout(self, campaign_id: str, amount_usd: float, method: str = "", notes: str = "") -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO payouts(campaign_id, amount_usd, received_at, method, notes) VALUES (?,?,?,?,?)",
                            (campaign_id, amount_usd, now_iso(), method, notes))
            return int(cur.lastrowid)

    # --- выборки для отчёта --------------------------------------------
    def latest_post_stats(self, since: Optional[str] = None) -> list[PostStat]:
        sql = """
        SELECT p.id AS post_id, p.clip_id, c.campaign_id, p.account, p.platform, p.url, p.posted_at, p.status,
               COALESCE((SELECT s.views FROM stats s WHERE s.post_id=p.id ORDER BY s.ts DESC LIMIT 1), 0) AS views,
               COALESCE(c.hook, '') AS hook
        FROM posts p JOIN clips c ON c.id = p.clip_id
        """
        params: tuple = ()
        if since:
            sql += " WHERE p.posted_at >= ?"
            params = (since,)
        sql += " ORDER BY p.posted_at"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [PostStat(**{k: row[k] for k in row.keys()}) for row in rows]

    def payouts(self, since: Optional[str] = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if since:
                return list(c.execute("SELECT * FROM payouts WHERE received_at >= ? ORDER BY received_at", (since,)))
            return list(c.execute("SELECT * FROM payouts ORDER BY received_at"))

    def counts(self) -> dict:
        with self._conn() as c:
            return {
                "sources": c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "clips": c.execute("SELECT COUNT(*) FROM clips").fetchone()[0],
                "posts": c.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                "stats": c.execute("SELECT COUNT(*) FROM stats").fetchone()[0],
            }

    def export_csv(self, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        with self._conn() as c:
            for table in ("sources", "clips", "posts", "stats", "payouts"):
                rows = c.execute(f"SELECT * FROM {table}").fetchall()
                p = out_dir / f"{table}.csv"
                with open(p, "w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f)
                    if rows:
                        w.writerow(rows[0].keys())
                        for r in rows:
                            w.writerow(list(r))
                    else:
                        w.writerow([])
                written.append(p)
        return written

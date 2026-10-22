"""Local-first storage. Every sample is written immediately, so long runs survive crashes."""
import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path


def data_dir() -> Path:
    d = Path(os.environ.get("PERFSCOPE_HOME", Path.home() / ".perfscope"))
    d.mkdir(parents=True, exist_ok=True)
    return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, name TEXT, project TEXT, tags TEXT, notes TEXT,
  device TEXT, app_package TEXT, app_version TEXT, config TEXT,
  started_at INTEGER, ended_at INTEGER, status TEXT,
  remote_id TEXT, uploaded_at INTEGER
);
CREATE TABLE IF NOT EXISTS points (
  session_id TEXT, ts INTEGER, metric TEXT, value REAL
);
CREATE INDEX IF NOT EXISTS idx_points ON points(session_id, ts);
CREATE TABLE IF NOT EXISTS events (
  session_id TEXT, ts INTEGER, type TEXT, label TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS attachments (
  session_id TEXT, ts INTEGER, kind TEXT, title TEXT, text TEXT
);
"""

JSON_COLS = ("tags", "device", "config")


class Store:
    def __init__(self, path=None):
        self.path = str(path or data_dir() / "local.db")
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            # Mark sessions interrupted by a crash
            self.conn.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
            self.conn.commit()

    def _row(self, r):
        d = dict(r)
        for c in JSON_COLS:
            if d.get(c):
                d[c] = json.loads(d[c])
        return d

    def create_session(self, s: dict):
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions (id,name,project,tags,notes,device,app_package,app_version,"
                "config,started_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], s["name"], s["project"], json.dumps(s.get("tags", []), ensure_ascii=False),
                 s.get("notes", ""), json.dumps(s["device"], ensure_ascii=False), s["app_package"],
                 s.get("app_version", ""), json.dumps(s.get("config", {}), ensure_ascii=False),
                 s["started_at"], "running"))
            self.conn.commit()

    def finish_session(self, sid, ended_at, status="finished"):
        with self.lock:
            self.conn.execute("UPDATE sessions SET ended_at=?, status=? WHERE id=?",
                              (ended_at, status, sid))
            self.conn.commit()

    def add_points(self, sid, ts, values: dict):
        if not values:
            return
        with self.lock:
            self.conn.executemany("INSERT INTO points VALUES (?,?,?,?)",
                                  [(sid, ts, k, v) for k, v in values.items()])
            self.conn.commit()

    def add_event(self, sid, ts, etype, label):
        with self.lock:
            self.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (sid, ts, etype, label))
            self.conn.commit()

    def rename_session(self, sid, name):
        with self.lock:
            n = self.conn.execute("UPDATE sessions SET name=? WHERE id=?", (name, sid)).rowcount
            self.conn.commit()
        return n

    def rename_event(self, sid, ts, etype, old, new):
        with self.lock:
            n = self.conn.execute("UPDATE events SET label=? WHERE session_id=? AND ts=? AND type=? AND label=?",
                                  (new, sid, ts, etype, old)).rowcount
            self.conn.commit()
        return n

    def list_sessions(self, limit=10000):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get_session(self, sid):
        with self.lock:
            r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    def points(self, sid, since=0):
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, metric, value FROM points WHERE session_id=? AND ts>? ORDER BY ts",
                (sid, since)).fetchall()
        return [[r[0], r[1], r[2]] for r in rows]

    def events(self, sid, since=0):
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, type, label FROM events WHERE session_id=? AND ts>? ORDER BY ts",
                (sid, since)).fetchall()
        return [[r[0], r[1], r[2]] for r in rows]

    # ------------------------------------------------ attachments: crash logs and other text kept with a run
    def add_attachment(self, sid, ts, kind, title, text):
        with self.lock:
            self.conn.execute("INSERT INTO attachments VALUES (?,?,?,?,?)", (sid, ts, kind, title, text))
            self.conn.commit()

    def attachments(self, sid):
        with self.lock:
            rows = self.conn.execute("SELECT ts, kind, title, text FROM attachments WHERE session_id=? ORDER BY ts",
                                     (sid,)).fetchall()
        return [{"ts": r[0], "kind": r[1], "title": r[2], "text": r[3]} for r in rows]

    # ------------------------------------------------ screenshots: one JPEG file per frame
    def shot_dir(self, sid) -> Path:
        return Path(self.path).parent / "shots" / sid

    def add_shot(self, sid, ts, jpeg: bytes):
        d = self.shot_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{ts}.tmp"
        tmp.write_bytes(jpeg)
        tmp.replace(d / f"{ts}.jpg")   # readers never see a half-written file

    def shots(self, sid, since=0):
        d = self.shot_dir(sid)
        if not d.is_dir():
            return []
        return sorted(t for t in (int(p.stem) for p in d.glob("*.jpg") if p.stem.isdigit()) if t > since)

    def shot_path(self, sid, ts) -> Path:
        return self.shot_dir(sid) / f"{int(ts)}.jpg"

    def mark_uploaded(self, sid, remote_id, ts):
        with self.lock:
            self.conn.execute("UPDATE sessions SET remote_id=?, uploaded_at=? WHERE id=?",
                              (remote_id, ts, sid))
            self.conn.commit()

    def delete_session(self, sid):
        with self.lock:
            for t in ("points", "events", "attachments"):
                self.conn.execute(f"DELETE FROM {t} WHERE session_id=?", (sid,))
            self.conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.conn.commit()
        shutil.rmtree(self.shot_dir(sid), ignore_errors=True)

    def get_settings(self):
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        return {r[0]: r[1] for r in rows}

    def set_settings(self, d: dict):
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                  [(k, str(v)) for k, v in d.items()])
            self.conn.commit()

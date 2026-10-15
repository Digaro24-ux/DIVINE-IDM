"""SQLite persistence for download tasks (both engines)."""
import sqlite3
import threading

DB_FILE = "downloads.db"
_lock = threading.Lock()
COLS = ["id", "url", "filename", "save_dir", "save_path", "total_size",
        "downloaded", "status", "speed", "eta", "kind", "meta"]


def _conn():
    return sqlite3.connect(DB_FILE, timeout=10)


def init_db():
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY, url TEXT, filename TEXT, save_dir TEXT,
            save_path TEXT, total_size INTEGER, downloaded INTEGER,
            status TEXT, speed REAL, eta TEXT, kind TEXT, meta TEXT)""")
        # migrate databases created by older versions
        have = {r[1] for r in c.execute("PRAGMA table_info(downloads)")}
        for col in ("kind", "meta"):
            if col not in have:
                c.execute(f"ALTER TABLE downloads ADD COLUMN {col} TEXT")


def upsert(row):
    with _lock, _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO downloads ({','.join(COLS)}) "
                  f"VALUES ({','.join('?' * len(COLS))})", [row[k] for k in COLS])


def load_all():
    with _lock, _conn() as c:
        rows = c.execute(f"SELECT {','.join(COLS)} FROM downloads ORDER BY id").fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def delete(task_id):
    with _lock, _conn() as c:
        c.execute("DELETE FROM downloads WHERE id = ?", (task_id,))

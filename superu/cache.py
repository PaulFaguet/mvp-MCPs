"""Cache SQLite pour éviter de spammer le site Super U (Cloudflare)."""

import json
import sqlite3
import time


class Cache:
    def __init__(self, db_path: str = "cache.db", ttl_minutes: int = 30):
        self.db_path = db_path
        self.ttl_seconds = ttl_minutes * 60
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

    def get(self, key: str) -> dict | list | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value, created_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, created_at = row
            if time.time() - created_at > self.ttl_seconds:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return None
            return json.loads(value)

    def set(self, key: str, value: dict | list) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache")

    def cleanup_expired(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM cache WHERE created_at < ?",
                (time.time() - self.ttl_seconds,),
            )
            return cursor.rowcount

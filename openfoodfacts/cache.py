"""Cache SQLite pour éviter de spammer l'API Open Food Facts (15 req/min en lecture)."""

import json
import os
import sqlite3
import time

# Le cache vit à la racine du projet, quel que soit le cwd de l'appelant.
_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "cache.db")


class Cache:
    def __init__(self, db_path: str = _DEFAULT_DB, ttl_minutes: int = 1440):
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

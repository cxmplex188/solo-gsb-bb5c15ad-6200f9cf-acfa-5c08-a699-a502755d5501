"""SQLite 持久化层（标准库 sqlite3，WAL 模式，线程安全）。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stroke_references (
    char        TEXT PRIMARY KEY,
    strokes     TEXT NOT NULL,          -- JSON: [[[x, y], ...], ...]
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    payload_hash  TEXT NOT NULL,        -- 请求体规范化后的 SHA-256
    char          TEXT NOT NULL,
    result        TEXT NOT NULL,        -- JSON: ValidateResponse（不含 replayed）
    created_at    TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 参考字库 ----------

    def upsert_reference(self, char: str, strokes: list) -> dict:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO stroke_references (char, strokes, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(char) DO UPDATE SET strokes = excluded.strokes,
                                                updated_at = excluded.updated_at
                """,
                (char, json.dumps(strokes), now, now),
            )
            self._conn.commit()
        return self.get_reference(char)  # type: ignore[return-value]

    def get_reference(self, char: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stroke_references WHERE char = ?", (char,)
            ).fetchone()
        if row is None:
            return None
        return {
            "char": row["char"],
            "strokes": json.loads(row["strokes"]),
            "updated_at": row["updated_at"],
        }

    def list_references(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT char, strokes, updated_at FROM stroke_references ORDER BY char"
            ).fetchall()
        return [
            {
                "char": r["char"],
                "stroke_count": len(json.loads(r["strokes"])),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def delete_reference(self, char: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM stroke_references WHERE char = ?", (char,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- 提交（幂等） ----------

    def get_submission(self, submission_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        return self._row_to_submission(row) if row else None

    def create_submission(self, submission_id: str, payload_hash: str, char: str, result: dict) -> dict:
        """插入新提交；若并发下主键冲突则返回已存在的记录。"""
        now = utcnow()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO submissions (submission_id, payload_hash, char, result, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (submission_id, payload_hash, char, json.dumps(result), now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                pass
        return self.get_submission(submission_id)  # type: ignore[return-value]

    @staticmethod
    def _row_to_submission(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "submission_id": row["submission_id"],
            "payload_hash": row["payload_hash"],
            "char": row["char"],
            "result": json.loads(row["result"]),
            "created_at": row["created_at"],
        }

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lean_loop.jsonutil import utc_now


DEFAULT_CACHE_LIMIT_BYTES = 64 * 1024 * 1024
DEFAULT_CACHE_ENTRY_LIMIT = 5000


def retrieval_cache_path(project: Path) -> Path:
    return project / ".lean-agent" / "cache" / "retrieval.sqlite3"


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS retrieval_cache (
                    cache_key TEXT PRIMARY KEY,
                    index_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )"""
            )
            yield connection
    finally:
        connection.close()


class RetrievalCache:
    def __init__(
        self,
        project: Path,
        *,
        max_bytes: int = DEFAULT_CACHE_LIMIT_BYTES,
        max_entries: int = DEFAULT_CACHE_ENTRY_LIMIT,
    ) -> None:
        self.path = retrieval_cache_path(project)
        self.max_bytes = max_bytes
        self.max_entries = max_entries

    def get(self, cache_key: str, fingerprint: str) -> dict[str, Any] | None:
        with _connection(self.path) as connection:
            row = connection.execute(
                "SELECT result_json FROM retrieval_cache WHERE cache_key = ? AND index_fingerprint = ?",
                (cache_key, fingerprint),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE retrieval_cache SET accessed_at = ? WHERE cache_key = ?",
                (utc_now(), cache_key),
            )
        value = json.loads(row["result_json"])
        return value if isinstance(value, dict) else None

    def put(self, cache_key: str, fingerprint: str, result: dict[str, Any]) -> None:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        now = utc_now()
        with _connection(self.path) as connection:
            connection.execute(
                """INSERT INTO retrieval_cache(
                    cache_key, index_fingerprint, created_at, accessed_at, result_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  index_fingerprint = excluded.index_fingerprint,
                  accessed_at = excluded.accessed_at,
                  result_json = excluded.result_json""",
                (cache_key, fingerprint, now, now, serialized),
            )
            while True:
                row = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(length(result_json)), 0) FROM retrieval_cache"
                ).fetchone()
                if int(row[0]) <= self.max_entries and int(row[1]) <= self.max_bytes:
                    break
                connection.execute(
                    "DELETE FROM retrieval_cache WHERE cache_key IN "
                    "(SELECT cache_key FROM retrieval_cache ORDER BY accessed_at LIMIT 100)"
                )

    def status(self) -> dict[str, object]:
        if not self.path.is_file():
            return {"exists": False, "path": str(self.path), "entries": 0, "size_bytes": 0}
        with _connection(self.path) as connection:
            entries = connection.execute("SELECT COUNT(*) FROM retrieval_cache").fetchone()[0]
        return {
            "exists": True,
            "path": str(self.path),
            "entries": int(entries),
            "size_bytes": self.path.stat().st_size,
            "max_result_bytes": self.max_bytes,
            "max_entries": self.max_entries,
        }


def clear_retrieval_cache(project: Path) -> None:
    path = retrieval_cache_path(project)
    path.unlink(missing_ok=True)
    path.with_name(path.name + "-wal").unlink(missing_ok=True)
    path.with_name(path.name + "-shm").unlink(missing_ok=True)

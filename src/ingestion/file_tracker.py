"""
src/ingestion/file_tracker.py
SHA-256 file hash + processing_status table.

Provides:
  - Incremental processing: skip already-successful files
  - Idempotency: re-running never double-processes
  - Audit trail: every file's outcome is recorded

Table: processing_status (in the same SQLite DB as invoice data)
  file_hash       TEXT PRIMARY KEY
  source_file     TEXT
  status          TEXT  — "success" | "failed" | "quarantined" | "skipped"
  processed_at    TEXT  — ISO timestamp
  run_timestamp   TEXT  — run identifier (same across all files in one run)
  error           TEXT  — error message if failed/quarantined
  invoice_id      TEXT  — extracted InvoiceId if successful
"""

import hashlib
import sqlite3
import os
from datetime import datetime, timezone
from src.observability.logger import get_logger

log = get_logger(__name__)


def compute_file_hash(path: str) -> str:
    """SHA-256 of file contents. Detects duplicates even if filename changes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class FileTracker:

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._init_table()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_table(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_status (
                    file_hash     TEXT PRIMARY KEY,
                    source_file   TEXT,
                    status        TEXT,
                    processed_at  TEXT,
                    run_timestamp TEXT,
                    error         TEXT,
                    invoice_id    TEXT
                )
            """)

    def already_processed(self, file_hash: str) -> bool:
        """True if this exact file was previously processed successfully."""
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT status FROM processing_status WHERE file_hash=?",
                (file_hash,)
            )
            row = cur.fetchone()
            return bool(row and row[0] == "success")

    def mark(
        self,
        file_hash: str,
        source_file: str,
        status: str,
        run_timestamp: str,
        error: str = "",
        invoice_id: str = "",
    ):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO processing_status
                    (file_hash, source_file, status, processed_at, run_timestamp, error, invoice_id)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(file_hash) DO UPDATE SET
                    status=excluded.status,
                    processed_at=excluded.processed_at,
                    run_timestamp=excluded.run_timestamp,
                    error=excluded.error,
                    invoice_id=excluded.invoice_id
            """, (
                file_hash, source_file, status,
                datetime.now(timezone.utc).isoformat(),
                run_timestamp, error, invoice_id,
            ))

    def status_summary(self) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT status, COUNT(*) FROM processing_status GROUP BY status"
            )
            return {row[0]: row[1] for row in cur.fetchall()}

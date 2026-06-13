"""
src/storage/sqlite_writer.py  (corrected)
Writes normalised invoice records to:
  1. SQLite database  (output/extractions.db)
  2. SQL dump file    (output/processed/file.sql)

FIX vs original: added _confidence_score, _confidence_tier, _action,
_conflicts, _is_duplicate columns for observability.
"""
"""
src/storage/sqlite_writer.py
FIX: Added _migrate_table() so schema changes never cause OperationalError.
"""

import sqlite3
import logging
import os
from src.models.schema import COLUMNS

log = logging.getLogger(__name__)

_EXTRA_COLS = [
    "_is_valid", "_validation_errors",
    "_confidence_score", "_confidence_tier", "_action",
    "_conflicts", "_is_duplicate",
]
_TABLE_COLS = COLUMNS + _EXTRA_COLS
_TABLE_NAME = "invoices"


def _create_table(conn: sqlite3.Connection) -> None:
    col_defs = ", ".join(f'"{c}" TEXT' for c in _TABLE_COLS)
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_TABLE_NAME} ({col_defs})')
    conn.commit()


def _migrate_table(conn: sqlite3.Connection) -> None:
    cur = conn.execute(f'PRAGMA table_info("{_TABLE_NAME}")')
    existing = {row[1] for row in cur.fetchall()}
    for col in _TABLE_COLS:
        if col not in existing:
            conn.execute(f'ALTER TABLE {_TABLE_NAME} ADD COLUMN "{col}" TEXT')
            log.info("[SQLite] Added missing column: %s", col)
    conn.commit()


def _record_to_row(record: dict) -> tuple:
    return tuple(str(record.get(col, "") or "") for col in _TABLE_COLS)


def write_sqlite(records: list[dict], db_path: str, sql_dump_path: str) -> None:
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    os.makedirs(os.path.dirname(sql_dump_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    _create_table(conn)
    _migrate_table(conn)

    placeholders = ", ".join("?" * len(_TABLE_COLS))
    col_names    = ", ".join(f'"{c}"' for c in _TABLE_COLS)
    insert_sql   = f'INSERT INTO {_TABLE_NAME} ({col_names}) VALUES ({placeholders})'

    rows = [_record_to_row(r) for r in records]
    conn.executemany(insert_sql, rows)
    conn.commit()
    log.info("[SQLite Writer] Inserted %d record(s) → %s", len(rows), db_path)

    with open(sql_dump_path, "w", encoding="utf-8") as f:
        for line in conn.iterdump():
            f.write(line + "\n")
    log.info("[SQLite Writer] SQL dump → %s", sql_dump_path)
    conn.close()
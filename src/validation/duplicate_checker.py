"""
src/validation/duplicate_checker.py
Detects duplicate invoices in the SQLite output DB.

Duplicate = same (InvoiceId + SellerVatId + InvGrandTotal + IssueDate).
Flags without blocking — humans decide whether it is a genuine resubmission.
"""

import sqlite3
import logging

log = logging.getLogger(__name__)


def check_duplicate(record: dict, db_path: str) -> bool:
    """
    Returns True if a record with the same key already exists in the DB.
    Returns False if DB doesn't exist yet or key is not found.
    """
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM invoices
            WHERE InvoiceId=? AND SellerVatId=? AND InvGrandTotal=? AND IssueDate=?
            """,
            (
                record.get("InvoiceId", ""),
                record.get("SellerVatId", ""),
                str(record.get("InvGrandTotal", "")),
                record.get("IssueDate", ""),
            ),
        )
        count = cur.fetchone()[0]
        conn.close()
        if count > 0:
            log.warning(
                "[DuplicateChecker] Duplicate detected: InvoiceId=%s",
                record.get("InvoiceId")
            )
        return count > 0
    except sqlite3.OperationalError:
        return False   # Table doesn't exist yet — first run
    except Exception as e:
        log.warning("[DuplicateChecker] Error: %s", e)
        return False

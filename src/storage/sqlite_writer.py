"""
src/storage/sqlite_writer.py
FIX: Two normalised tables + schema migration.

Tables:
  invoices           — one row per invoice
  invoice_line_items — one row per line item (FK: SourceFile + InvoiceId)
  processing_status  — created by file_tracker.py (left alone here)

write_sqlite() signature changed:
  OLD: write_sqlite(records, db_path, sql_dump_path)
  NEW: write_sqlite(invoice_records, line_item_records, db_path, sql_dump_path)
"""

import sqlite3
import logging
import os
import json

log = logging.getLogger(__name__)

_INV_TABLE  = "invoices"
_LINE_TABLE = "invoice_line_items"

# All fields that can appear in the invoices table
_INV_COLS = [
    "SourceFile", "FileHash", "RunTimestamp", "ExtractionMethod",
    "InvoiceId", "InvoiceTypeCode", "IssueDate", "ReferencedInvoiceId",
    "Currency", "BuyerReference",
    "SellerName", "SellerContactName", "SellerPhone", "SellerEmail",
    "SellerStreet", "SellerPostcode", "SellerCity", "SellerCountry", "SellerVatId",
    "BuyerId", "BuyerName", "BuyerStreet", "BuyerPostcode",
    "BuyerCity", "BuyerCountry", "BuyerEmail",
    "ShipToName", "ShipToStreet", "ShipToPostcode", "ShipToCity", "ShipToCountry",
    "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
    "InvTaxBasisTotal", "InvTaxTotal", "InvTaxTotalCurrencyId",
    "InvRounding", "InvGrandTotal", "InvPrepaidTotal", "InvDuePayable",
    "PaymentMeansCode", "Iban", "BankAccountName", "Bic", "PaymentTerms", "DueDate",
    "_is_valid", "_validation_errors", "_confidence_score",
    "_confidence_tier", "_action", "_conflicts", "_is_duplicate",
]

_LINE_COLS = [
    "SourceFile", "InvoiceId",
    "LineId", "LineNote", "SellerProductId",
    "ProductName", "ProductDescription",
    "BuyerOrderLineId", "BilledQuantity", "BilledUnit",
    "NetPrice", "DiscountAmount", "LineTotalAmount",
    "TaxType", "TaxCategory", "TaxRatePercent",
    "BillingStartDate", "DiscountIndicator", "DiscountReason",
]


def _create_tables(conn: sqlite3.Connection) -> None:
    inv_defs  = ", ".join(f'"{c}" TEXT' for c in _INV_COLS)
    line_defs = ", ".join(f'"{c}" TEXT' for c in _LINE_COLS)
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_INV_TABLE}  ({inv_defs})')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_LINE_TABLE} ({line_defs})')
    conn.commit()


def _migrate(conn: sqlite3.Connection, table: str, cols: list[str]) -> None:
    """Add any missing columns — safe to call on every run."""
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    existing = {row[1] for row in cur.fetchall()}
    for col in cols:
        if col not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" TEXT')
            log.info("[SQLite] Added column %s.%s", table, col)
    conn.commit()


def _to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, default=str)
    return str(v)


def write_sqlite(
    invoice_records: list[dict],
    line_item_records: list[dict],
    db_path: str,
    sql_dump_path: str,
) -> None:
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    os.makedirs(os.path.dirname(sql_dump_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    _create_tables(conn)
    _migrate(conn, _INV_TABLE,  _INV_COLS)
    _migrate(conn, _LINE_TABLE, _LINE_COLS)

    if invoice_records:
        ph  = ", ".join("?" * len(_INV_COLS))
        cols = ", ".join(f'"{c}"' for c in _INV_COLS)
        sql  = f'INSERT OR REPLACE INTO {_INV_TABLE} ({cols}) VALUES ({ph})'
        rows = [tuple(_to_str(r.get(c)) for c in _INV_COLS) for r in invoice_records]
        conn.executemany(sql, rows)
        log.info("[SQLite] %d invoice(s) → %s", len(rows), db_path)

    if line_item_records:
        ph   = ", ".join("?" * len(_LINE_COLS))
        cols = ", ".join(f'"{c}"' for c in _LINE_COLS)
        sql  = f'INSERT INTO {_LINE_TABLE} ({cols}) VALUES ({ph})'
        rows = [tuple(_to_str(r.get(c)) for c in _LINE_COLS) for r in line_item_records]
        conn.executemany(sql, rows)
        log.info("[SQLite] %d line item(s) → %s", len(rows), db_path)

    conn.commit()

    with open(sql_dump_path, "w", encoding="utf-8") as f:
        for line in conn.iterdump():
            f.write(line + "\n")
    log.info("[SQLite] SQL dump → %s", sql_dump_path)
    conn.close()

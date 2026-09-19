"""Load a parsed file into Postgres. Re-uploading the same bytes is a no-op; overlapping periods upsert."""
from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from . import config as cfgmod
from .parsers import IGNORED, LABELS, ParseError, detect, parse

# table -> (conflict columns, columns to refresh on conflict; empty = leave the existing row alone)
TABLES = {
    "bank_txn": (["row_hash"], []),
    "pos_bill": (["invoice_no", "order_ts"], ["payment_type", "status", "order_type", "area", "total"]),
    "pos_online_order": (["order_from", "client_order_no"], ["invoice_no", "order_ts", "status", "total"]),
    "plat_order": (["platform", "product", "order_id", "txn_type"],
                   ["order_ts", "bill_amount", "commissionable", "commission_pct", "commission_amt", "other_fees",
                    "gst_on_fees", "net_payable", "utr", "settlement_date", "extras"]),
    "plat_adjustment": (["platform", "product", "kind", "ref", "adj_date"], ["period_to", "amount", "utr"]),
}


def _upsert(conn: psycopg.Connection, table: str, rows: list[dict], upload_id: int) -> int:
    if not rows:
        return 0
    conflict, refresh = TABLES[table]
    cols = ["upload_id"] + list(rows[0].keys())
    action = "do nothing" if not refresh else "do update set " + ", ".join(f"{c}=excluded.{c}" for c in refresh) + ", upload_id=excluded.upload_id"
    sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
           f"on conflict ({', '.join(conflict)}) {action} returning (xmax = 0) as inserted")
    new = 0
    with conn.cursor() as cur:
        for r in rows:
            vals = [upload_id] + [Jsonb(v) if isinstance(v, dict) else v for v in r.values()]
            cur.execute(sql, vals)
            got = cur.fetchone()
            new += 1 if got and got["inserted"] else 0
    return new


def ingest(conn: psycopg.Connection, path: Path, filename: str | None = None, cfg=None) -> dict:
    cfg = cfg or cfgmod.load()
    filename = filename or path.name
    result = {"filename": filename, "kind": None, "label": None, "status": None, "rows_read": 0, "rows_new": 0, "note": None}
    kind = detect(path)
    result.update(kind=kind, label=LABELS.get(kind))
    if kind in IGNORED:
        return {**result, "status": "ignored", "note": IGNORED[kind]}
    if kind == "unknown":
        return {**result, "status": "rejected", "note": "Layout not recognised. Supported: ICICI statement, Petpooja order summary and "
                "online-platform report, Zomato delivery settlement and dining payout, Swiggy annexure orders and adjustments."}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    seen = conn.execute("select filename, uploaded_at from upload where sha256=%s", (digest,)).fetchone()
    if seen:
        return {**result, "status": "duplicate", "note": f"Identical file already loaded as '{seen['filename']}' on {seen['uploaded_at']:%d %b %Y}."}
    try:
        parsed = parse(kind, path, cfg)
    except ParseError as e:
        return {**result, "status": "rejected", "note": str(e)}
    with conn.transaction():
        uid = conn.execute(
            "insert into upload (kind, filename, sha256, period_from, period_to) values (%s,%s,%s,%s,%s) returning id",
            (kind, filename, digest, parsed["period_from"], parsed["period_to"])).fetchone()["id"]
        read = new = 0
        for table, rows in parsed["tables"].items():
            read += len(rows)
            new += _upsert(conn, table, rows, uid)
        conn.execute("update upload set rows_read=%s, rows_new=%s where id=%s", (read, new, uid))
    return {**result, "status": "loaded", "rows_read": read, "rows_new": new,
            "note": f"{parsed['period_from']:%d %b %Y} to {parsed['period_to']:%d %b %Y}"
                    + (f" · {read - new} rows already known (updated)" if read != new else "")}

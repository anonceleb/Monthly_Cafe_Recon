"""Petpooja exports: Order Summary (csv, item level) and Sales Report: Online Platforms (xlsx)."""
from __future__ import annotations

import csv
from pathlib import Path

import re

from .common import ParseError, find_row, header_map, num, sheet_rows, snap_month, stated_period, text, to_dt

BILL_REQUIRED = ["invoice_no", "date", "payment_type", "status", "total"]


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    return rows[0] if rows else [], rows[1:]


def parse_bills(path: Path) -> dict:
    hdr, body = read_csv(path)
    idx = {c: i for i, c in enumerate(hdr)}
    missing = [c for c in BILL_REQUIRED if c not in idx]
    if missing:
        raise ParseError(f"Petpooja order summary: missing columns {missing}")
    seen, out = set(), []
    for r in body:
        if len(r) <= idx["total"] or not r[idx["invoice_no"]].strip():
            continue
        inv = r[idx["invoice_no"]].strip()
        if inv in seen:                 # item-level report repeats bill fields on every item row
            continue
        seen.add(inv)
        out.append({"invoice_no": inv, "order_ts": to_dt(r[idx["date"]]), "payment_type": r[idx["payment_type"]].strip(),
                    "status": r[idx["status"]].strip(), "order_type": r[idx.get("order_type", 0)].strip() if "order_type" in idx else None,
                    "area": r[idx["area"]].strip() if "area" in idx else None, "total": num(r[idx["total"]])})
    if not out:
        raise ParseError("Petpooja order summary: no bills found")
    ts = [o["order_ts"].date() for o in out]
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", path.name)          # Order_Summary_Item_Report_…_2026-04-01_2026-04-30.csv
    lo, hi = (to_dt(m.group(1)).date(), to_dt(m.group(2)).date()) if m else snap_month(min(ts), max(ts))
    return {"tables": {"pos_bill": out}, "period_from": min(lo, min(ts)), "period_to": max(hi, max(ts))}


def is_online(rows) -> bool:
    return any("Sales Report: Online Platforms" in " ".join(str(c) for c in r if c) for r in rows[:6])


def parse_online(path: Path) -> dict:
    rows = sheet_rows(path)
    h = find_row(rows, lambda r: "Client Order No." in [str(c).strip() for c in r if c])
    if h is None:
        raise ParseError("Petpooja online report: header row with 'Client Order No.' not found")
    cols = header_map(rows[h], {"ts": "Date", "order_no": "Client Order No.", "from": "Order From", "status": "Status",
                                 "total": "Total", "invoice": "Invoice No."}, "Petpooja online report")
    out = []
    for r in rows[h + 1:]:
        if not text(r[cols["from"]]) or not text(r[cols["order_no"]]):
            continue                       # blank or the 'Total' summary row
        out.append({"order_from": text(r[cols["from"]]).lower(), "client_order_no": text(r[cols["order_no"]]),
                    "invoice_no": text(r[cols["invoice"]]), "order_ts": to_dt(r[cols["ts"]]),
                    "status": text(r[cols["status"]]) or "", "total": num(r[cols["total"]])})
    if not out:
        raise ParseError("Petpooja online report: no orders found")
    ts = [o["order_ts"].date() for o in out]
    lo, hi = stated_period(rows, "Date:") or snap_month(min(ts), max(ts))
    return {"tables": {"pos_online_order": out}, "period_from": min(lo, min(ts)), "period_to": max(hi, max(ts))}

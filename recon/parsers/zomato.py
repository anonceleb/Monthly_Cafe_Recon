"""Zomato: Delivery 'Settlement Report' (Order Level) and Dining 'Consolidated payout report' (Transactions summary)."""
from __future__ import annotations

from pathlib import Path

from .common import ParseError, find_row, header_map, num, sheet_names, sheet_rows, snap_month, stated_period, text, to_date, to_dt


def is_delivery(names: list[str]) -> bool:
    return "Order Level" in names and "Payout Breakup" in names and "Addition Deductions Details" in names


def is_dining(names: list[str]) -> bool:
    return "Transactions summary" in names


DELIVERY_REQUIRED = {
    "order_id": "Order ID", "order_ts": "Order Date", "status": "Order status",
    "subtotal": "(1) Subtotal", "gst_collected": "(8) Total GST collected", "A": "(A) Net order value",
    "commissionable": "(9) Commissionable value", "pct": "(10) Service fees %", "service_fee": "(11) Service fee",
    "pay_fee": "(12) Payment mechanism fee", "B": "(B) Service fee & payment", "tax": "(13) Taxes on service",
    "C": "(C) Government charges", "D": "(D) Other order-level deductions", "E": "(E) Net Deductions",
    "F": "(F) Net Additions", "G": "(G) Order level Payout", "settle_date": "(28) Settlement date", "utr": "(29) Bank UTR",
}


def _delivery_header(rows) -> tuple[int, dict]:
    h = find_row(rows, lambda r: any(str(c).strip() == "Order ID" for c in r if c))
    if h is None:
        raise ParseError("Zomato delivery: header row with 'Order ID' not found")
    # Column labels are '(1) Subtotal…': combine the numbering row above with the name row
    above = rows[h - 1] if h > 0 else []
    labelled = [f"{str(above[i]).strip() if i < len(above) and above[i] else ''} {c or ''}".strip() for i, c in enumerate(rows[h])]
    return h, header_map(labelled, DELIVERY_REQUIRED, "Zomato delivery")


def parse_delivery(path: Path) -> dict:
    rows = sheet_rows(path, "Order Level")
    h, c = _delivery_header(rows)
    out = []
    for r in rows[h + 1:]:
        if not r or not str(r[0]).strip().isdigit():
            continue                      # '#REF!' template rows and blanks
        status = (text(r[c["status"]]) or "").upper()
        out.append({
            "platform": "zomato", "product": "delivery", "order_id": text(r[c["order_id"]]), "order_ts": to_dt(r[c["order_ts"]]),
            "txn_type": "sale" if status == "DELIVERED" else "cancelled", "bill_amount": num(r[c["A"]]),
            "commissionable": num(r[c["commissionable"]]), "commission_pct": num(r[c["pct"]]),
            "commission_amt": num(r[c["service_fee"]]), "other_fees": num(r[c["pay_fee"]]), "gst_on_fees": num(r[c["tax"]]),
            "net_payable": num(r[c["G"]]), "utr": text(r[c["utr"]]), "settlement_date": to_date(r[c["settle_date"]]),
            "extras": {k: str(num(r[c[k]])) for k in ("A", "B", "C", "D", "E", "F", "G")},
        })
    if not out:
        raise ParseError("Zomato delivery: no order rows found")
    ts = [o["order_ts"].date() for o in out]
    lo, hi = stated_period(sheet_rows(path, "Summary"), "Report Period") or snap_month(min(ts), max(ts))
    return {"tables": {"plat_order": out}, "period_from": min(lo, min(ts)), "period_to": max(hi, max(ts))}


DINING_REQUIRED = {
    "txn_id": "Transaction ID", "ts": "Date and time", "ttype": "Transaction Type", "type": "Type", "bill": "Bill Amount",
    "commissionable": "Commissionable amount", "pct": "Commission %", "comm": "Commission Amount",
    "tax": "Tax on commission", "tips": "Tips", "adj": "Adjustment", "net": "Net receivable",
    "settle_date": "Settlement date", "utr": "UTR Number",
}


def parse_dining(path: Path) -> dict:
    rows = sheet_rows(path, "Transactions summary")
    h = find_row(rows, lambda r: any(str(c).strip() == "Transaction ID" for c in r if c))
    if h is None:
        raise ParseError("Zomato dining: header row with 'Transaction ID' not found")
    c = header_map(rows[h], DINING_REQUIRED, "Zomato dining")
    out = []
    for r in rows[h + 1:]:
        if not r or not str(r[0]).strip().isdigit():
            continue                      # formula legend row
        typ = (text(r[c["type"]]) or "sale").lower()
        out.append({
            "platform": "zomato", "product": "dining", "order_id": text(r[c["txn_id"]]), "order_ts": to_dt(r[c["ts"]]),
            "txn_type": typ, "bill_amount": num(r[c["bill"]]), "commissionable": num(r[c["commissionable"]]),
            "commission_pct": num(r[c["pct"]]), "commission_amt": num(r[c["comm"]]), "other_fees": num(0),
            "gst_on_fees": num(r[c["tax"]]), "net_payable": num(r[c["net"]]), "utr": text(r[c["utr"]]),
            "settlement_date": to_date(r[c["settle_date"]]),
            "extras": {"tips": str(num(r[c["tips"]])), "adjustment": str(num(r[c["adj"]]))},
        })
    if not out:
        raise ParseError("Zomato dining: no transaction rows found")
    adj = _dining_adjustments(path)
    ts = [o["order_ts"].date() for o in out]
    lo, hi = stated_period(sheet_rows(path, "Summary"), "Report period") or snap_month(min(ts), max(ts))
    return {"tables": {"plat_order": out, "plat_adjustment": adj}, "period_from": min(lo, min(ts)), "period_to": max(hi, max(ts))}


def _dining_adjustments(path: Path) -> list[dict]:
    if "Additions & deductions" not in sheet_names(path):
        return []
    rows = sheet_rows(path, "Additions & deductions")
    h = find_row(rows, lambda r: any(str(c).strip() == "Campaign ID" for c in r if c))
    if h is None:
        return []
    c = header_map(rows[h], {"type": "Type", "campaign": "Campaign ID", "order": "Order ID", "date": "Date", "utr": "UTR", "amount": "Amount"},
                   "Zomato additions & deductions")
    out = []
    for r in rows[h + 1:]:
        if not r or not str(r[0]).strip().isdigit():
            continue
        out.append({"platform": "zomato", "product": "dining", "kind": (text(r[c["type"]]) or "adjustment").lower() + ":ads",
                    "ref": f"{text(r[c['campaign']])}/{text(r[c['order']])}", "adj_date": to_date(r[c["date"]]), "period_to": None,
                    "amount": num(r[c["amount"]]), "utr": text(r[c["utr"]])})
    return out

"""Swiggy consolidated annexure CSVs: orders (delivery + Dineout) and adjustments."""
from __future__ import annotations

from pathlib import Path

from .common import ParseError, num, snap_month, text, to_date, to_dt
from .petpooja import read_csv

ORDER_COLS = {
    "order_id": "Order No", "order_ts": "Order Date", "status": "Order Status", "category": "Order Category",
    "D": "Net Bill Value (without taxes) D", "E": "GST liability of", "F": "Customer payable",
    "chargeable": "Swiggy Platform Service Fee Chargeable On", "pct": "Swiggy Platform Service Fee %",
    "G": "Swiggy Platform Service Fee G", "Q": "Total Swiggy Service fee (without taxes) Q", "R": "Taxes on Swiggy fee",
    "S": "Total Swiggy fee (including taxes) S", "V": "Total of Order Level Adjustments V", "W": "Net Payable Amount (before TCS",
    "X1": "TCS X1", "X2": "TDS X2", "Y": "Net Payable Amount (after TCS and TDS", "nodal": "Nodal UTR", "current": "Current UTR",
}


def is_orders(header: list[str]) -> bool:
    return "Order No" in header and any(h.startswith("Swiggy Platform Service Fee") for h in header)


def is_adjustments(header: list[str]) -> bool:
    return "Adjustment Item" in header and "Total Amount" in header


def _idx(header: list[str], spec: dict, where: str) -> dict:
    out, missing = {}, []
    for k, label in spec.items():
        i = next((n for n, h in enumerate(header) if h.strip().startswith(label)), None)
        (out.__setitem__(k, i) if i is not None else missing.append(label))
    if missing:
        raise ParseError(f"{where}: expected column(s) not found: {missing}. The report layout may have changed.")
    return out


def parse_orders(path: Path) -> dict:
    header, body = read_csv(path)
    c = _idx(header, ORDER_COLS, "Swiggy orders")
    out = []
    for r in body:
        if len(r) < len(header) or not r[c["order_id"]].strip():
            continue
        status = r[c["status"]].strip().lower()
        product = "dining" if r[c["category"]].strip().lower() == "dineout" else "delivery"
        pct = num(r[c["pct"]], None)
        Q, G = num(r[c["Q"]]), num(r[c["G"]])
        out.append({
            "platform": "swiggy", "product": product, "order_id": r[c["order_id"]].strip(), "order_ts": to_dt(r[c["order_ts"]]),
            "txn_type": "cancelled" if status == "cancelled" else "sale", "bill_amount": num(r[c["F"]]),
            "commissionable": num(r[c["chargeable"]]), "commission_pct": pct, "commission_amt": G, "other_fees": Q - G,
            "gst_on_fees": num(r[c["R"]]), "net_payable": num(r[c["Y"]]),
            "utr": text(r[c["current"]]) or text(r[c["nodal"]]), "settlement_date": None,
            "extras": {k: str(num(r[c[k]])) for k in ("D", "E", "F", "Q", "S", "V", "W", "X1", "X2")},
        })
    if not out:
        raise ParseError("Swiggy orders: no order rows found")
    ts = [o["order_ts"].date() for o in out]
    lo, hi = snap_month(min(ts), max(ts))
    return {"tables": {"plat_order": out}, "period_from": lo, "period_to": hi}


def parse_adjustments(path: Path) -> dict:
    header, body = read_csv(path)
    c = _idx(header, {"item": "Adjustment Item", "from": "Period From", "to": "Period To", "amount": "Total Amount"}, "Swiggy adjustments")
    out = []
    for r in body:
        if len(r) < len(header) or not r[c["item"]].strip():
            continue
        out.append({"platform": "swiggy", "product": "delivery", "kind": r[c["item"]].strip().lower(),
                    "ref": f"{r[c['from']]}..{r[c['to']]}", "adj_date": to_date(r[c["from"]]), "period_to": to_date(r[c["to"]]),
                    "amount": num(r[c["amount"]]), "utr": None})
    if not out:
        raise ParseError("Swiggy adjustments: no rows found")
    d = [o["adj_date"] for o in out] + [o["period_to"] for o in out]
    return {"tables": {"plat_adjustment": out}, "period_from": min(d), "period_to": max(d)}

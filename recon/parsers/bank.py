"""ICICI current-account 'Detailed Statement' (xlsx)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..config import Config
from .common import ParseError, find_row, header_map, num, sheet_rows, text, to_date, to_dt

REQUIRED = {"sn": "S.N.", "tran_id": "Tran. Id", "txn_date": "Transaction Date", "posted": "Transaction Posted Date",
            "remarks": "Transaction Remarks", "wd": "Withdrawal Amt", "dep": "Deposit Amt", "bal": "Balance"}
UTR_RE = re.compile(r"(?:NEFT|RTGS)-([A-Z0-9]+)-")


def is_bank(rows) -> bool:
    return find_row(rows, lambda r: "Transaction Remarks" in [str(c).strip() for c in r if c]) is not None


def payer_of(remarks: str, cfg: Config):
    up = remarks.upper()
    for p in cfg.payers:
        if p["pattern"].upper() in up:
            return p["platform"], p.get("product")
    return None, None


def parse(path: Path, cfg: Config) -> dict:
    rows = sheet_rows(path)
    h = find_row(rows, lambda r: "Transaction Remarks" in [str(c).strip() for c in r if c])
    if h is None:
        raise ParseError("bank statement: header row with 'Transaction Remarks' not found")
    cols = header_map(rows[h], REQUIRED, "bank statement")
    out, period_from, period_to = [], None, None
    for r in rows[h + 1:]:
        sn = r[cols["sn"]] if len(r) > cols["sn"] else None
        if not str(sn).strip().isdigit():
            if out:      # legend/footer text after the last transaction
                break
            continue
        remarks = text(r[cols["remarks"]]) or ""
        wd, dep = num(r[cols["wd"]]), num(r[cols["dep"]])
        tran_id, posted = text(r[cols["tran_id"]]), text(r[cols["posted"]])
        m = UTR_RE.search(remarks)
        platform, product = payer_of(remarks, cfg)
        key = "|".join([tran_id or "", posted or "", remarks, str(wd), str(dep), str(num(r[cols["bal"]]))])
        out.append({
            "row_hash": hashlib.sha1(key.encode()).hexdigest(), "tran_id": tran_id,
            "txn_date": to_date(r[cols["txn_date"]]), "posted_at": to_dt(posted) if posted else None,
            "remarks": remarks, "withdrawal": wd, "deposit": dep, "balance": num(r[cols["bal"]], None),
            "utr": m.group(1) if m else None,
            "payer_platform": platform if dep > 0 else None, "payer_product": product if dep > 0 else None,
        })
    if not out:
        raise ParseError("bank statement: no transaction rows found")
    # Statement period from the header block ("From 16/03/2026 To 15/05/2026"), else from the rows.
    for r in rows[:h]:
        line = " ".join(str(c) for c in r if c)
        m = re.search(r"From\s+(\d{2}/\d{2}/\d{4})\s+To\s+(\d{2}/\d{2}/\d{4})", line)
        if m:
            period_from, period_to = to_date(m.group(1)), to_date(m.group(2))
    dates = [o["txn_date"] for o in out]
    return {"tables": {"bank_txn": out}, "period_from": period_from or min(dates), "period_to": period_to or max(dates)}

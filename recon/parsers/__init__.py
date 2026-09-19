"""Content-based file detection: what a file *is* comes from its layout, never its name."""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from . import bank, petpooja, swiggy, zomato
from .common import ParseError, sheet_names, sheet_rows  # noqa: F401 (ParseError re-exported)

LABELS = {
    "bank_icici": "ICICI bank statement", "pos_bills": "Petpooja order summary", "pos_online": "Petpooja online-platform sales",
    "zomato_delivery": "Zomato delivery settlement", "zomato_dining": "Zomato dining payout", "swiggy_orders": "Swiggy orders annexure",
    "swiggy_adjustments": "Swiggy adjustments",
}
# Recognised but deliberately not needed. Saying why beats a silent skip.
IGNORED = {
    "swiggy_invoices": "Swiggy invoice summary: fee GST is already re-derived per order, so this file is not needed.",
    "swiggy_annexure_xlsx": "Swiggy monthly annexure (xlsx): duplicates the consolidated annexure CSVs, which also carry Dineout. Upload the CSVs.",
    "pos_other": "Petpooja item/purchase report: not part of payout reconciliation.",
}


def detect(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        header, _ = petpooja.read_csv(path)
        first = ",".join(header)
        if "invoice_no" in header and "payment_type" in header:
            return "pos_bills"
        if swiggy.is_orders(header):
            return "swiggy_orders"
        if swiggy.is_adjustments(header):
            return "swiggy_adjustments"
        if "Invoice Number" in header and "Base Amount" in header:
            return "swiggy_invoices"
        if first.startswith("Start Date"):
            return "pos_other"
        return "unknown"
    if suffix in (".xlsx", ".xlsm"):
        names = sheet_names(path)
        if zomato.is_delivery(names):
            return "zomato_delivery"
        if zomato.is_dining(names):
            return "zomato_dining"
        if "Other charges and deductions" in names and "Payout Breakup" in names:
            return "swiggy_annexure_xlsx"
        rows = sheet_rows(path)[:60]
        if bank.is_bank(rows):
            return "bank_icici"
        if petpooja.is_online(rows):
            return "pos_online"
        if any(str(c).strip().startswith("Name:") for r in rows[:6] for c in r if c):
            return "pos_other"
    return "unknown"


def parse(kind: str, path: Path, cfg: Config) -> dict:
    fn = {
        "bank_icici": lambda: bank.parse(path, cfg), "pos_bills": lambda: petpooja.parse_bills(path),
        "pos_online": lambda: petpooja.parse_online(path), "zomato_delivery": lambda: zomato.parse_delivery(path),
        "zomato_dining": lambda: zomato.parse_dining(path), "swiggy_orders": lambda: swiggy.parse_orders(path),
        "swiggy_adjustments": lambda: swiggy.parse_adjustments(path),
    }.get(kind)
    if fn is None:
        raise ParseError(f"no parser for {kind}")
    return fn()

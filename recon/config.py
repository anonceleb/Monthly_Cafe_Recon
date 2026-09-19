"""Loads the contracted terms (rates.toml) and bank/POS label mappings (payers.toml)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass
class Config:
    gst_pct: float
    tol_rupee: float
    tol_fee: float
    tol_settlement: float
    match_minutes: int
    lag_days: int
    terms: dict          # {(platform, product): {"commission_pct": [..], "payment_pct": x|None}}
    payers: list         # [{"pattern","platform","product"}]
    pos_payment_type: dict
    pos_order_from: dict
    complimentary_types: list = field(default_factory=list)

    def commission_pcts(self, platform: str, product: str) -> list[float]:
        return self.terms.get((platform, product), {}).get("commission_pct", [])

    def payment_pct(self, platform: str, product: str):
        return self.terms.get((platform, product), {}).get("payment_pct")


def _as_list(v):
    if v in (None, ""):
        return []
    return [float(x) for x in v] if isinstance(v, list) else [float(v)]


def load(config_dir: Path = CONFIG_DIR) -> Config:
    rates = tomllib.loads((config_dir / "rates.toml").read_text())
    payers = tomllib.loads((config_dir / "payers.toml").read_text())
    terms = {}
    for platform, products in rates.get("platform", {}).items():
        if "commission_pct" in products:            # flat platform (eazydiner, ownly)
            terms[(platform, "dining" if platform == "eazydiner" else "delivery")] = {
                "commission_pct": _as_list(products["commission_pct"]), "payment_pct": None}
            continue
        for product, t in products.items():
            terms[(platform, product)] = {
                "commission_pct": _as_list(t.get("commission_pct")),
                "payment_pct": float(t["payment_pct"]) if t.get("payment_pct") not in (None, "") else None,
            }
    tol = rates["tolerance"]
    return Config(
        gst_pct=float(rates["gst"]["rate_on_fees"]),
        tol_rupee=float(tol["rupee"]), tol_fee=float(tol["fee_arithmetic"]),
        tol_settlement=float(tol["settlement"]), match_minutes=int(tol["match_minutes"]),
        lag_days=int(tol["settlement_lag_days"]),
        terms=terms, payers=payers.get("payer", []),
        pos_payment_type=payers.get("pos_payment_type", {}),
        pos_order_from=payers.get("pos_order_from", {}),
        complimentary_types=payers.get("pos_complimentary", {}).get("payment_types", []),
    )

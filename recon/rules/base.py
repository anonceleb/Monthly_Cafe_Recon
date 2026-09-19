"""Rule plumbing: Finding, the shared Ctx every rule receives, and the @rule registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from ..config import Config

RULES: list["Rule"] = []


@dataclass
class Finding:
    rule_id: str
    severity: str                     # gap = needs action · warn = worth a look · info = explained / declared
    message: str
    platform: str | None = None
    product: str | None = None
    ref: str | None = None
    on_date: date | None = None
    expected: float | None = None
    actual: float | None = None
    diff: float | None = None
    details: dict | list | None = None


@dataclass
class Rule:
    id: str
    title: str
    description: str
    fn: Callable[["Ctx"], list[Finding]]


def rule(id: str, title: str):
    """Register a rule. The function's docstring is shown to the user as the rule's description."""
    def deco(fn):
        RULES.append(Rule(id, title, " ".join((fn.__doc__ or "").split()), fn))
        return fn
    return deco


@dataclass
class Ctx:
    cfg: Config
    bank: pd.DataFrame
    pos_bill: pd.DataFrame
    pos_online: pd.DataFrame
    plat: pd.DataFrame
    adj: pd.DataFrame
    coverage: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)   # rules drop numbers here for the dashboard

    # -- coverage helpers -------------------------------------------------------------------
    def window(self, *keys: str):
        """Intersection of coverage windows; None if any source is missing or they do not overlap."""
        spans = [self.coverage.get(k) for k in keys]
        if any(s is None for s in spans):
            return None
        lo, hi = max(s[0] for s in spans), min(s[1] for s in spans)
        return (lo, hi) if lo <= hi else None

    def plat_rows(self, platform: str, product: str | None = None) -> pd.DataFrame:
        d = self.plat[self.plat.platform == platform]
        return d if product is None else d[d["product"] == product]

    def lag(self) -> timedelta:
        return timedelta(days=self.cfg.lag_days)


def money(x) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), 2)


def inr(x) -> str:
    x = float(x)
    s = f"{abs(x):,.2f}"
    return f"-₹{s}" if x < 0 else f"₹{s}"

"""Does each payout report obey the contract? Re-derives fees, GST and net from the report's own components."""
from __future__ import annotations

import pandas as pd

from .base import Ctx, Finding, inr, money, rule


def _x(df: pd.DataFrame, key: str) -> pd.Series:
    return df["extras"].map(lambda e: float(e.get(key, 0) or 0))


@rule("PI-01", "Commission rate differs from contract")
def commission_rate(ctx: Ctx) -> list[Finding]:
    """Platform charged a commission % that is not one of the contracted rates (config/rates.toml)."""
    out = []
    sales = ctx.plat[ctx.plat.txn_type == "sale"]
    for (platform, product), g in sales.groupby(["platform", "product"]):
        allowed = ctx.cfg.commission_pcts(platform, product)
        if not allowed:
            continue
        for pct, rows in g.groupby("commission_pct"):
            if any(abs(pct - a) < 0.005 for a in allowed):
                continue
            nearest = min(allowed, key=lambda a: abs(a - pct))
            excess = (rows.commissionable * (pct - nearest) / 100).sum()
            out.append(Finding("PI-01", "gap", f"{platform.title()} {product}: {len(rows)} orders charged {pct:g}% commission; contract is "
                               f"{'/'.join(f'{a:g}' for a in allowed)}%. Over-charge ≈ {inr(excess)}.",
                               platform, product, f"{pct:g}%", expected=nearest, actual=float(pct), diff=money(excess),
                               details=rows.order_id.tolist()[:200]))
    return out


@rule("PI-02", "Fee arithmetic does not add up")
def fee_arithmetic(ctx: Ctx) -> list[Finding]:
    """Commission ≠ rate × commissionable base, or (Zomato delivery) payment-mechanism fee ≠ contracted % of the base."""
    out, tol = [], ctx.cfg.tol_fee
    sales = ctx.plat[ctx.plat.txn_type == "sale"].copy()
    sales["calc"] = (sales.commission_pct * sales.commissionable / 100).round(2)
    bad = sales[(sales.commission_amt - sales.calc).abs() > tol]
    for r in bad.itertuples():
        out.append(Finding("PI-02", "warn", f"{r.platform.title()} {r.product} {r.order_id}: commission {inr(r.commission_amt)} but "
                           f"{r.commission_pct:g}% of {inr(r.commissionable)} is {inr(r.calc)}.", r.platform, r.product, r.order_id,
                           r.order_ts.date(), expected=money(r.calc), actual=money(r.commission_amt), diff=money(r.commission_amt - r.calc)))
    pay = ctx.cfg.payment_pct("zomato", "delivery")
    if pay is not None:
        z = sales[(sales.platform == "zomato") & (sales["product"] == "delivery")].copy()
        z["calc"] = (z.commissionable * pay / 100).round(2)
        for r in z[(z.other_fees - z.calc).abs() > tol].itertuples():
            out.append(Finding("PI-02", "warn", f"Zomato delivery {r.order_id}: payment-mechanism fee {inr(r.other_fees)}, expected "
                               f"{pay:g}% of base = {inr(r.calc)}.", "zomato", "delivery", r.order_id, r.order_ts.date(),
                               expected=money(r.calc), actual=money(r.other_fees), diff=money(r.other_fees - r.calc)))
    return out


@rule("PI-03", "GST on fees is not the statutory rate")
def gst_on_fees(ctx: Ctx) -> list[Finding]:
    """GST charged on commission and fees differs from the configured rate applied to (commission + other fees)."""
    out, tol, rate = [], ctx.cfg.tol_fee, ctx.cfg.gst_pct
    d = ctx.plat[ctx.plat.txn_type != "refund"].copy()
    d["calc"] = ((d.commission_amt + d.other_fees) * rate / 100).round(2)
    for r in d[(d.gst_on_fees - d.calc).abs() > tol].itertuples():
        out.append(Finding("PI-03", "warn", f"{r.platform.title()} {r.product} {r.order_id}: GST on fees {inr(r.gst_on_fees)}; "
                           f"{rate:g}% of fees {inr(r.commission_amt + r.other_fees)} is {inr(r.calc)}.", r.platform, r.product,
                           r.order_id, r.order_ts.date(), expected=money(r.calc), actual=money(r.gst_on_fees),
                           diff=money(r.gst_on_fees - r.calc)))
    return out


@rule("PI-04", "Net payable is lower than the report's own components")
def net_identity(ctx: Ctx) -> list[Finding]:
    """Rebuilds each order's payout from the components printed in the same report (Zomato: A − E + F; Zomato dining: commissionable −
    commission − tax + tips + adjustment; Swiggy: F − S − V, then − TCS − TDS). A residual is money deducted without being itemised.
    Grouped by residual as % of bill so a systematic deduction shows up once, not per order."""
    out, tol = [], ctx.cfg.tol_fee
    rows = []
    z = ctx.plat[(ctx.plat.platform == "zomato") & (ctx.plat["product"] == "delivery")]
    if len(z):
        rows.append(z.assign(expected=_x(z, "A") - _x(z, "E") + _x(z, "F")))
    zd = ctx.plat[(ctx.plat.platform == "zomato") & (ctx.plat["product"] == "dining")]
    if len(zd):
        rows.append(zd.assign(expected=zd.commissionable - zd.commission_amt - zd.gst_on_fees + _x(zd, "tips") + _x(zd, "adjustment")))
    s = ctx.plat[ctx.plat.platform == "swiggy"]
    if len(s):
        rows.append(s.assign(expected=_x(s, "F") - _x(s, "S") - _x(s, "V") - _x(s, "X1") - _x(s, "X2")))
    if not rows:
        return out
    d = pd.concat(rows)
    d["residual"] = (d.expected - d.net_payable).round(2)          # +ve = payout is lower than components imply
    bad = d[(d.residual.abs() > tol) & (d.txn_type != "cancelled") & (d.bill_amount.abs() > 0)].copy()
    bad["pct"] = (bad.residual / bad.bill_amount * 100).round(0)
    for (platform, product, pct), g in bad.groupby(["platform", "product", "pct"]):
        total = g.residual.sum()
        out.append(Finding("PI-04", "gap", f"{platform.title()} {product}: {len(g)} orders paid {pct:g}% of bill ({inr(total)} in total) "
                           f"below what the report's own fee lines imply — the deduction is not itemised in the report. Check whether an offer or discount funded by the restaurant explains it.",
                           platform, product, f"{pct:g}% of bill", expected=money(g.expected.sum()), actual=money(g.net_payable.sum()),
                           diff=money(-total), details=[{"order": r.order_id, "bill": money(r.bill_amount), "residual": money(r.residual)}
                                                        for r in g.itertuples()][:300]))
    return out


@rule("PI-05", "Payout row has money owed but no settlement (UTR)")
def unsettled(ctx: Ctx) -> list[Finding]:
    """Order rows with a non-zero net payable but no UTR: the platform has not (yet) paid, or the report omits the reference."""
    out = []
    d = ctx.plat[ctx.plat.utr.isna() & (ctx.plat.net_payable.abs() > 0.005)]
    for (platform, product), g in d.groupby(["platform", "product"]):
        out.append(Finding("PI-05", "warn", f"{platform.title()} {product}: {len(g)} rows with net {inr(g.net_payable.sum())} have no settlement UTR.",
                           platform, product, expected=None, actual=money(g.net_payable.sum()), diff=money(g.net_payable.sum()),
                           details=[{"order": r.order_id, "net": money(r.net_payable), "type": r.txn_type} for r in g.itertuples()][:200]))
    return out

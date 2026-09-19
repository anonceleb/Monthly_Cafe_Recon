"""Payout reports vs the bank statement. The unit is the settlement (one UTR), not the calendar month: weekly payouts straddle month ends."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .base import Ctx, Finding, inr, money, rule


def _settlements(ctx: Ctx) -> pd.DataFrame:
    p = ctx.plat[ctx.plat.utr.notna()]
    g = p.groupby(["platform", "utr"]).agg(products=("product", lambda s: ",".join(sorted(set(s)))), orders=("order_id", "count"), net=("net_payable", "sum"),
                                           first_order=("order_ts", "min"), last_order=("order_ts", "max"), settle_date=("settlement_date", "max")).reset_index()
    a = ctx.adj[ctx.adj.utr.notna()]
    if len(a):
        ag = a.groupby("utr").agg(adj=("amount", "sum"), adj_platform=("platform", "first")).reset_index()
        g = g.merge(ag, on="utr", how="outer")
        g["platform"] = g.platform.fillna(g.adj_platform)
        g["products"] = g.products.fillna("dining")
        g["orders"] = g.orders.fillna(0)
        g["net"] = g.net.fillna(0)
    else:
        g["adj"] = 0.0
    g["adj"] = g.adj.fillna(0.0)
    b = ctx.bank[(ctx.bank.deposit > 0) & ctx.bank.utr.notna()].groupby("utr").agg(bank=("deposit", "sum"), bank_date=("txn_date", "min")).reset_index()
    g = g.merge(b, on="utr", how="left")
    g["expected"] = (g.net + g.adj).round(2)
    return g


def _allocate(ctx: Ctx, s: pd.DataFrame):
    """Swiggy reports adjustments (ads) without saying which payout they came out of. Assign each to the earliest bank credit, on or after the
    adjustment period, whose shortfall equals the adjustment. Returns (per-UTR allocated sum, unallocated adjustments)."""
    alloc: dict[str, float] = {}
    loose = ctx.adj[ctx.adj.utr.isna()].sort_values("period_to")
    left = []
    cand = s[s.bank.notna()].sort_values("bank_date")
    for r in loose.itertuples():
        hit = None
        for c in cand[cand.platform == r.platform].itertuples():
            if c.bank_date < (r.period_to or r.adj_date):
                continue
            shortfall = c.bank - (c.expected + alloc.get(c.utr, 0.0))
            if abs(shortfall - r.amount) <= ctx.cfg.tol_settlement:
                hit = c.utr
                break
        if hit:
            alloc[hit] = alloc.get(hit, 0.0) + float(r.amount)
        else:
            left.append(r)
    return alloc, left


def _prepare(ctx: Ctx):
    s = _settlements(ctx)
    alloc, left = _allocate(ctx, s)
    s["alloc"] = s.utr.map(alloc).fillna(0.0)
    s["expected_all"] = (s.expected + s.alloc).round(2)
    return s, left


def _span(ctx: Ctx, platform: str, product: str | None = None):
    """Report coverage for one product; with no product, the dates that EVERY product's report covers (a payer like Zomato settles both)."""
    keys = [f"plat:{platform}:{product}"] if product else [k for k in ctx.coverage if k.startswith(f"plat:{platform}:")]
    keys = [k for k in keys if k in ctx.coverage]
    if not keys:
        return None
    lo, hi = max(ctx.coverage[k][0] for k in keys), min(ctx.coverage[k][1] for k in keys)
    return (lo, hi) if lo <= hi else None


@rule("BK-01", "Expected settlement not in the bank")
def settlement_missing(ctx: Ctx) -> list[Finding]:
    """A payout report lists a UTR (settlement) but no matching credit appears in the bank statement, though the statement covers that date.
    Settlements dated after the statement ends (or before it starts) are counted as pending/out-of-range, not as gaps."""
    out = []
    bank = ctx.coverage.get("bank")
    if not bank:
        return out
    s, _ = _prepare(ctx)
    missing = s[s.bank.isna()].copy()
    if missing.empty:                        # every settlement found in the bank: nothing to report
        return out
    missing["due"] = missing.apply(lambda r: r.settle_date if pd.notna(r.settle_date) else (r.last_order + ctx.lag()).date(), axis=1)
    for r in missing.itertuples():
        if r.due > bank[1] or r.due < bank[0]:
            continue
        out.append(Finding("BK-01", "gap", f"{r.platform.title()} settlement {r.utr} ({r.orders:g} orders, due {r.due:%d %b}) expected {inr(r.expected_all)} but no bank credit found.",
                           r.platform, r.products, r.utr, r.due, expected=money(r.expected_all), actual=0, diff=money(-r.expected_all)))
    later = missing[missing.due.map(lambda d: d > bank[1])]
    for platform, g in later.groupby("platform"):
        out.append(Finding("BK-01", "info", f"{platform.title()}: {len(g)} settlements worth {inr(g.expected_all.sum())} fall after the bank statement ends ({bank[1]:%d %b %Y}) — upload a later statement to close them.",
                           platform, None, "after-statement", None, expected=money(g.expected_all.sum()), actual=None, diff=None, details=g.utr.tolist()))
    early = missing[missing.due.map(lambda d: d < bank[0])]
    for platform, g in early.groupby("platform"):
        out.append(Finding("BK-01", "info", f"{platform.title()}: {len(g)} settlements worth {inr(g.expected_all.sum())} predate the bank statement ({bank[0]:%d %b %Y}).",
                           platform, None, "before-statement", None, expected=money(g.expected_all.sum()), details=g.utr.tolist()))
    return out


@rule("BK-02", "Bank credit differs from the payout report")
def settlement_amount(ctx: Ctx) -> list[Finding]:
    """For each UTR present in both: bank credit vs (sum of order net payables + ad/other adjustments). Weekly Swiggy ad deductions are matched to
    the payout they were taken from. A bank credit larger than the report, on a settlement touching the first/last day of the uploaded report period,
    is classed as a period boundary (orders outside the file), not a gap."""
    out, tol = [], ctx.cfg.tol_settlement
    s, _ = _prepare(ctx)
    ctx.summary["settlement"] = {}
    both = s[s.bank.notna()].copy()
    for r in both.itertuples():
        prod = r.products.split(",")[0]
        key = f"{r.platform}:{prod}"
        agg = ctx.summary["settlement"].setdefault(key, {"utrs": 0, "matched": 0, "via_adjustment": 0, "boundary": 0, "gap": 0, "expected": 0.0, "bank": 0.0, "gap_amount": 0.0, "boundary_amount": 0.0})
        agg["utrs"] += 1
        agg["expected"] += float(r.expected_all); agg["bank"] += float(r.bank)
        diff = round(float(r.bank - r.expected_all), 2)
        if abs(diff) <= tol:
            agg["matched"] += 1
            agg["via_adjustment"] += 1 if r.alloc else 0
            continue
        span = _span(ctx, r.platform, prod)
        edge = span and (r.first_order.date() <= span[0] + timedelta(days=1) or r.last_order.date() >= span[1] - timedelta(days=1))
        if diff > 0 and edge:
            agg["boundary"] += 1; agg["boundary_amount"] += diff
            out.append(Finding("BK-02", "info", f"{r.platform.title()} {r.utr}: bank paid {inr(diff)} more than the uploaded report lists — settlement touches the report's period edge "
                               f"({r.first_order:%d %b}–{r.last_order:%d %b}); the extra orders are outside the file. Upload the adjacent period to close.",
                               r.platform, prod, r.utr, r.bank_date, expected=money(r.expected_all), actual=money(r.bank), diff=diff))
        else:
            agg["gap"] += 1; agg["gap_amount"] += abs(diff)
            out.append(Finding("BK-02", "gap", f"{r.platform.title()} {r.utr}: bank credited {inr(r.bank)} but the report says {inr(r.expected_all)} "
                               f"(orders {inr(r.net)}, adjustments {inr(r.adj + r.alloc)}). Difference {inr(diff)}.", r.platform, prod, r.utr, r.bank_date,
                               expected=money(r.expected_all), actual=money(r.bank), diff=diff,
                               details={"orders": int(r.orders), "order_net": money(r.net), "adjustments": money(r.adj + r.alloc)}))
    return out


@rule("BK-03", "Bank credit from a platform with no payout report line")
def bank_credit_unexplained(ctx: Ctx) -> list[Finding]:
    """A credit from Zomato/Swiggy in the bank whose UTR is in no payout report. If it lands where the uploaded report should already list it,
    that is a gap; if it belongs to a period we hold no report for, it is only noted."""
    out, lag = [], ctx.lag()
    s, _ = _prepare(ctx)
    known = set(s.utr)
    cr = ctx.bank[(ctx.bank.deposit > 0) & ctx.bank.payer_platform.isin(["zomato", "swiggy"]) & ~ctx.bank.utr.isin(known)]
    for platform, g in cr.groupby("payer_platform"):
        span = _span(ctx, platform)
        sp = s[s.platform == platform]
        # Did the uploaded report reach its own last day? If so, credits just after it belong to the NEXT period, not to a stale download.
        tail_ok = bool(len(sp)) and sp.last_order.max().date() >= (span[1] - timedelta(days=1) if span else date.max)
        outside = []
        for r in g.itertuples():
            d = r.txn_date
            if span and span[0] + lag <= d <= span[1]:
                out.append(Finding("BK-03", "gap", f"{platform.title()} credit {inr(r.deposit)} on {d:%d %b} (UTR {r.utr}) is in no payout report although reports cover this date.",
                                   platform, r.payer_product, r.utr, d, expected=0, actual=money(r.deposit), diff=money(r.deposit)))
            elif span and span[1] < d <= span[1] + lag and not tail_ok:
                out.append(Finding("BK-03", "warn", f"{platform.title()} credit {inr(r.deposit)} on {d:%d %b} (UTR {r.utr}) is not in the report, which stops short of {span[1]:%d %b}. "
                                   f"It probably settles orders the report was generated before — re-download the report.",
                                   platform, r.payer_product, r.utr, d, expected=0, actual=money(r.deposit), diff=money(r.deposit)))
            else:
                outside.append(r)
        if outside:
            tot = sum(float(r.deposit) for r in outside)
            where = f"({span[0]:%d %b} – {span[1]:%d %b})" if span else ""
            out.append(Finding("BK-03", "info", f"{platform.title()}: {len(outside)} credits worth {inr(tot)} settle periods outside the uploaded payout reports {where}".strip() + ".",
                               platform, None, "outside-coverage", None, actual=money(tot), details=[{"utr": r.utr, "date": str(r.txn_date), "amount": money(r.deposit)} for r in outside]))
    return out


@rule("BK-04", "Adjustment (ads/deduction) not seen in any payout")
def adjustment_unmatched(ctx: Ctx) -> list[Finding]:
    """A platform adjustment with no UTR (Swiggy) that cannot be tied to any bank credit's shortfall: either it was not deducted, or it was
    deducted in an amount/timing that does not match."""
    s, left = _prepare(ctx)
    return [Finding("BK-04", "gap", f"{r.platform.title()} {r.kind} {inr(r.amount)} for {r.ref} is not reflected in any bank settlement.", r.platform, r.product,
                    r.ref, r.adj_date, expected=money(r.amount), actual=0, diff=money(-r.amount)) for r in left]

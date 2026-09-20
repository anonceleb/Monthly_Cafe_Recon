"""Petpooja vs platform payout reports. Delivery matches on the platform's order id; dine-in has no shared id, so it matches on
amount + time. Only the overlap of what both sides cover is compared, so uploading a partial period never invents gaps."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .base import Ctx, Finding, inr, money, rule

DELIVERY = [("zomato", "delivery"), ("swiggy", "delivery")]
DINING = [("zomato", "dining"), ("swiggy", "dining")]


def _delivery_join(ctx: Ctx, platform: str):
    win = ctx.window("pos_online", f"plat:{platform}:delivery")
    if not win:
        return None, None
    lo, hi = win
    pos = ctx.pos_online[(ctx.pos_online.order_from == platform) & ctx.pos_online.order_ts.dt.date.between(lo, hi)]
    plat = ctx.plat_rows(platform, "delivery")
    plat = plat[plat.order_ts.dt.date.between(lo, hi)]
    return (pos, plat), win


@rule("PP-01", "POS delivery order has no payout row")
def pos_not_in_payout(ctx: Ctx) -> list[Finding]:
    """A Petpooja online order (status Success) whose order id is absent from the platform's payout report: the sale was rung up
    but the platform never listed it, so no money is coming."""
    out = []
    for platform, _ in DELIVERY:
        got, _win = _delivery_join(ctx, platform)
        if not got:
            continue
        pos, plat = got
        missing = pos[(pos.status.str.lower() == "success") & ~pos.client_order_no.isin(plat.order_id)]
        for r in missing.itertuples():
            out.append(Finding("PP-01", "gap", f"{platform.title()} order {r.client_order_no} ({inr(r.total)}) is in Petpooja but not in the payout report.",
                               platform, "delivery", r.client_order_no, r.order_ts.date(), expected=money(r.total), actual=0, diff=money(-r.total)))
    return out


@rule("PP-02", "Payout row has no POS order")
def payout_not_in_pos(ctx: Ctx) -> list[Finding]:
    """The platform paid (or charged) for an order that Petpooja has no record of."""
    out = []
    for platform, _ in DELIVERY:
        got, _win = _delivery_join(ctx, platform)
        if not got:
            continue
        pos, plat = got
        extra = plat[(plat.txn_type == "sale") & ~plat.order_id.isin(pos.client_order_no)]
        for r in extra.itertuples():
            out.append(Finding("PP-02", "gap", f"{platform.title()} order {r.order_id} ({inr(r.bill_amount)}) is in the payout report but not in Petpooja.",
                               platform, "delivery", r.order_id, r.order_ts.date(), expected=0, actual=money(r.bill_amount), diff=money(r.bill_amount)))
    return out


@rule("PP-03", "POS total differs from platform bill")
def amount_mismatch(ctx: Ctx) -> list[Finding]:
    """Same order id on both sides but Petpooja's total differs from the platform's customer-payable amount by more than the rounding tolerance."""
    out = []
    for platform, _ in DELIVERY:
        got, _win = _delivery_join(ctx, platform)
        if not got:
            continue
        pos, plat = got
        m = pos.merge(plat[plat.txn_type == "sale"], left_on="client_order_no", right_on="order_id", suffixes=("_pos", "_plat"))
        m["diff"] = m.total - m.bill_amount
        for r in m[m["diff"].abs() > ctx.cfg.tol_rupee].itertuples():
            out.append(Finding("PP-03", "gap", f"{platform.title()} order {r.order_id}: Petpooja {inr(r.total)} vs platform {inr(r.bill_amount)}.",
                               platform, "delivery", r.order_id, r.order_ts_pos.date(), expected=money(r.total), actual=money(r.bill_amount), diff=money(-r.diff)))
    return out


@rule("PP-05", "POS says sold, platform says cancelled")
def status_mismatch(ctx: Ctx) -> list[Finding]:
    """Petpooja booked the order as a successful sale but the platform reports it cancelled/rejected, so it will not be paid."""
    out = []
    for platform, _ in DELIVERY:
        got, _win = _delivery_join(ctx, platform)
        if not got:
            continue
        pos, plat = got
        m = pos[pos.status.str.lower() == "success"].merge(plat[plat.txn_type == "cancelled"], left_on="client_order_no", right_on="order_id", suffixes=("_pos", "_plat"))
        for r in m.itertuples():
            out.append(Finding("PP-05", "gap", f"{platform.title()} order {r.order_id} ({inr(r.total)}) is a sale in Petpooja but cancelled at the platform.",
                               platform, "delivery", r.order_id, r.order_ts_pos.date(), expected=money(r.total), actual=0, diff=money(-r.total)))
    return out


def _pair(pos: pd.DataFrame, plat: pd.DataFrame, tol_amt: float, max_min: float, same_day: bool = False):
    """1:1 pairing among equal amounts, smallest time gap first. Returns (pairs[(plat_idx, pos_idx, gap_min)], left_pos_idx, left_plat_idx).
    Vectorised: every bill x transaction comparison is one numpy operation (the row-by-row version took ~10s for a month)."""
    if pos.empty or plat.empty:
        return [], list(pos.index), list(plat.index)
    pa, ba = plat.bill_amount.to_numpy(float), pos.total.to_numpy(float)
    pt = plat.order_ts.to_numpy("datetime64[s]").astype("int64")
    bt = pos.order_ts.to_numpy("datetime64[s]").astype("int64")
    gap = np.abs(bt[None, :] - pt[:, None]) / 60.0
    ok = np.abs(ba[None, :] - pa[:, None]) <= tol_amt
    ok &= (gap <= max_min) | ((bt[None, :] // 86400 == pt[:, None] // 86400) if same_day else False)
    ii, jj = np.nonzero(ok)
    pidx, bidx = plat.index.to_numpy(), pos.index.to_numpy()
    order = np.lexsort((bidx[jj], pidx[ii], gap[ii, jj]))          # smallest gap first; ties broken by index, as before
    used_p, used_b, pairs = set(), set(), []
    for k in order:
        i, j = pidx[ii[k]], bidx[jj[k]]
        if i not in used_p and j not in used_b:
            used_p.add(i); used_b.add(j); pairs.append((i, j, float(gap[ii[k], jj[k]])))
    return pairs, [j for j in pos.index if j not in used_b], [i for i in plat.index if i not in used_p]


def _splits(pos: pd.DataFrame, plat: pd.DataFrame, tol_amt: float, max_min: float):
    """Split payments: one platform transaction = two POS bills, or one POS bill = two platform transactions (same table, minutes apart)."""
    out, used_p, used_b = [], set(), set()
    for i, p in plat.iterrows():                                   # 1 platform txn  <-  2 bills
        near = [j for j, b in pos.iterrows() if j not in used_b and abs((b.order_ts - p.order_ts).total_seconds()) / 60 <= max_min]
        for x in range(len(near)):
            for y in range(x + 1, len(near)):
                if i not in used_p and abs(pos.loc[near[x], "total"] + pos.loc[near[y], "total"] - p.bill_amount) <= tol_amt:
                    used_p.add(i); used_b.update([near[x], near[y]]); out.append(("1plat-2pos", [i], [near[x], near[y]]))
    for j, b in pos.iterrows():                                    # 1 bill  <-  2 platform txns
        if j in used_b:
            continue
        near = [i for i, p in plat.iterrows() if i not in used_p and abs((b.order_ts - p.order_ts).total_seconds()) / 60 <= max_min]
        for x in range(len(near)):
            for y in range(x + 1, len(near)):
                if j not in used_b and abs(plat.loc[near[x], "bill_amount"] + plat.loc[near[y], "bill_amount"] - b.total) <= tol_amt:
                    used_b.add(j); used_p.update([near[x], near[y]]); out.append(("2plat-1pos", [near[x], near[y]], [j]))
    return out, used_p, used_b


@rule("PP-04", "Dine-in bill and platform transaction do not pair up")
def dinein_match(ctx: Ctx) -> list[Finding]:
    """Zomato Dining / Swiggy Dineout have no shared order id, so bills are paired to platform transactions in passes: (1) same platform, same amount,
    within the configured minutes; (2) same platform, same amount and day, time further apart; (3) other platform's report — the POS payment type is
    mis-tagged; (4) a bill rung under another payment type (Cash/Card/…) that is really a platform payment; (5) split payments — one transaction
    equal to two bills or vice-versa. Whatever is left on either side is a gap."""
    out: list[Finding] = []
    tol, mins = ctx.cfg.tol_rupee, ctx.cfg.match_minutes
    ctx.summary["dine_match"] = {}
    P, T = {}, {}
    for platform, product in DINING:
        win = ctx.window("pos_bill", f"plat:{platform}:{product}")
        if not win:
            continue
        lo, hi = win
        types = [t for t, v in ctx.cfg.pos_payment_type.items() if v["platform"] == platform and v["product"] == product]
        P[platform] = ctx.pos_bill[ctx.pos_bill.payment_type.isin(types) & (ctx.pos_bill.status.str.lower() == "success") & ctx.pos_bill.order_ts.dt.date.between(lo, hi)]
        plat = ctx.plat_rows(platform, product)
        T[platform] = plat[plat.txn_type.isin(["sale", "cancelled"]) & plat.order_ts.dt.date.between(lo, hi)]
        ctx.summary["dine_match"][f"{platform}:{product}"] = {"pos": len(P[platform]), "platform": len(T[platform]), "window": [str(lo), str(hi)], "paired": 0}
    if not P:
        return out
    left_pos, left_plat = {}, {}

    def bill(b): return f"Petpooja bill {b.invoice_no} ({inr(b.total)}, {b.order_ts:%d %b %H:%M}, tagged {b.payment_type})"
    def txn(platform, t): return f"{platform.title()} transaction {t.order_id} ({inr(t.bill_amount)}, {t.order_ts:%d %b %H:%M})"

    for platform in P:                                              # pass 1 + 2
        pairs, lp, lt = _pair(P[platform], T[platform], tol, mins)
        ctx.summary["dine_match"][f"{platform}:dining"]["paired"] = len(pairs)
        for i, j, _ in pairs:
            t = T[platform].loc[i]
            if t.txn_type == "cancelled":
                b = P[platform].loc[j]
                out.append(Finding("PP-04", "gap", f"{bill(b)} is a sale in Petpooja but {txn(platform, t)} is cancelled.", platform, "dining", b.invoice_no, b.order_ts.date(),
                                   expected=money(b.total), actual=0, diff=money(-b.total)))
        loose, lp, lt = _pair(P[platform].loc[lp], T[platform].loc[lt], tol, 0, same_day=True)
        if loose:
            ctx.summary["dine_match"][f"{platform}:dining"]["paired"] += len(loose)
            out.append(Finding("PP-04", "info", f"{platform.title()} dining: {len(loose)} bills pair with a platform transaction of the same amount on the same day but at a different "
                               f"time (likely booking time vs bill time).", platform, "dining", "time-offset", None,
                               details=[{"bill": P[platform].loc[j].invoice_no, "pos_time": str(P[platform].loc[j].order_ts), "txn": T[platform].loc[i].order_id,
                                         "platform_time": str(T[platform].loc[i].order_ts), "amount": money(P[platform].loc[j].total), "gap_min": round(g)} for i, j, g in loose]))
        left_pos[platform], left_plat[platform] = P[platform].loc[lp], T[platform].loc[lt]

    # pass 3: cross-platform among leftovers
    for pf in list(left_pos):
        for other in list(left_plat):
            if other == pf or not len(left_pos[pf]) or not len(left_plat[other]):
                continue
            pairs, lp, lt = _pair(left_pos[pf], left_plat[other], tol, mins)
            for i, j, _ in pairs:
                b, t = left_pos[pf].loc[j], left_plat[other].loc[i]
                out.append(Finding("PP-04", "warn", f"{bill(b)} is tagged {pf.title()} in Petpooja but {txn(other, t)} matches it — mis-tagged payment type.", pf, "dining",
                                   b.invoice_no, b.order_ts.date(), expected=money(b.total), actual=money(t.bill_amount), diff=0.0))
            ctx.summary["dine_match"][f"{other}:dining"]["paired"] += len(pairs)
            left_pos[pf], left_plat[other] = left_pos[pf].loc[lp], left_plat[other].loc[lt]

    # pass 4: bills rung under a non-platform payment type
    mapped = set(ctx.cfg.pos_payment_type)
    other_bills = ctx.pos_bill[~ctx.pos_bill.payment_type.isin(mapped | {"Online"}) & (ctx.pos_bill.status.str.lower() == "success")]
    for platform in list(left_plat):
        if not len(left_plat[platform]):
            continue
        lo, hi = ctx.summary["dine_match"][f"{platform}:dining"]["window"]
        cand = other_bills[other_bills.order_ts.dt.date.between(pd.Timestamp(lo).date(), pd.Timestamp(hi).date())]
        pairs, _lp, lt = _pair(cand, left_plat[platform][left_plat[platform].txn_type == "sale"], tol, min(mins, 30))
        for i, j, _ in pairs:
            b, t = cand.loc[j], left_plat[platform].loc[i]
            out.append(Finding("PP-04", "warn", f"{txn(platform, t)} has no bill tagged {platform.title()}, but {bill(b)} matches it — probably rung under the wrong payment type.",
                               platform, "dining", t.order_id, t.order_ts.date(), expected=money(b.total), actual=money(t.bill_amount), diff=0.0))
            ctx.summary["dine_match"][f"{platform}:dining"]["paired"] += 1
        left_plat[platform] = left_plat[platform].drop(index=[i for i, _, _ in pairs])

    # pass 5: split payments across whatever is left
    all_pos = pd.concat(left_pos.values()) if left_pos else pd.DataFrame()
    plat_frames = [d.assign(_pf=k) for k, d in left_plat.items() if len(d)]
    all_plat = pd.concat(plat_frames) if plat_frames else pd.DataFrame()
    if len(all_pos) and len(all_plat):
        splits, used_p, used_b = _splits(all_pos, all_plat.reset_index(drop=True).set_index(all_plat.index.astype(str) + all_plat["_pf"]), tol, mins)
    else:
        splits, used_p, used_b = [], set(), set()
    if splits:
        pl = all_plat.reset_index(drop=True).set_index(all_plat.index.astype(str) + all_plat["_pf"])
        for kind, pi, bj in splits:
            ts = [pl.loc[i] for i in pi]; bs = [all_pos.loc[j] for j in bj]
            tags = {t._pf for t in ts} | {(ctx.cfg.pos_payment_type.get(b.payment_type) or {}).get("platform", b.payment_type) for b in bs}
            cross = len(tags) > 1
            out.append(Finding("PP-04", "warn" if cross else "info",
                               "Split payment: " + " + ".join(bill(b) for b in bs) + " ↔ " + " + ".join(txn(t._pf, t) for t in ts) + (" — spans platforms/payment tags." if cross else "."),
                               ts[0]._pf, "dining", ts[0].order_id, ts[0].order_ts.date(), expected=money(sum(b.total for b in bs)), actual=money(sum(t.bill_amount for t in ts)), diff=0.0))
        left_pos = {k: d.drop(index=[j for j in d.index if j in used_b]) for k, d in left_pos.items()}
        left_plat = {k: d[~(d.index.astype(str) + k).isin(used_p)] for k, d in left_plat.items()}

    # pass 6 (last resort): same table — bill and transaction within 5 minutes — but the amounts differ
    for platform in list(left_pos):
        lp, lt = left_pos[platform], left_plat.get(platform, T[platform].iloc[0:0])
        near = sorted((abs((b.order_ts - t.order_ts).total_seconds()) / 60, i, j) for i, t in lt.iterrows() for j, b in lp.iterrows()
                      if t.txn_type == "sale" and abs((b.order_ts - t.order_ts).total_seconds()) / 60 <= 5)
        used_i, used_j = set(), set()
        for gap, i, j in near:
            if i in used_i or j in used_j:
                continue
            used_i.add(i); used_j.add(j)
            b, t = lp.loc[j], lt.loc[i]
            out.append(Finding("PP-04", "gap", f"{platform.title()} dining: Petpooja bill {b.invoice_no} is {inr(b.total)} but the platform transaction {t.order_id} "
                               f"({t.order_ts:%d %b %H:%M}, {round(gap)} min apart) is {inr(t.bill_amount)} — amount differs by {inr(t.bill_amount - b.total)}.",
                               platform, "dining", b.invoice_no, b.order_ts.date(), expected=money(b.total), actual=money(t.bill_amount), diff=money(t.bill_amount - b.total)))
            ctx.summary["dine_match"][f"{platform}:dining"]["paired"] += 1
        left_pos[platform] = lp.drop(index=list(used_j)); left_plat[platform] = lt.drop(index=list(used_i))

    for platform in left_pos:                                       # leftovers = real gaps
        for b in left_pos[platform].itertuples():
            out.append(Finding("PP-04", "gap", f"{bill(b)} has no platform transaction — no payout will arrive for it.", platform, "dining", b.invoice_no, b.order_ts.date(),
                               expected=money(b.total), actual=0, diff=money(-b.total)))
    for platform in left_plat:
        for t in left_plat[platform].itertuples():
            if t.txn_type == "cancelled":
                continue
            out.append(Finding("PP-04", "gap", f"{txn(platform, t)} has no Petpooja bill.", platform, "dining", t.order_id, t.order_ts.date(),
                               expected=0, actual=money(t.bill_amount), diff=money(t.bill_amount)))
    return out


@rule("PP-06", "Platform with no payout report (bank-level only)")
def no_report_platforms(ctx: Ctx) -> list[Finding]:
    """EazyDiner and Ownly have no payout report, so per-order matching is impossible. Shows POS sales for the period next to what the bank
    received from that platform. Payouts trail sales, so the two will not agree exactly and no deduction % is inferred."""
    out = []
    lag = ctx.lag()
    for platform in ("eazydiner", "ownly"):
        if len(ctx.plat_rows(platform)):
            continue                         # a report exists; the other rules cover it
        if platform == "ownly":
            win = ctx.window("pos_online")
            sales = ctx.pos_online[ctx.pos_online.order_from == "ownly"]
        else:
            win = ctx.window("pos_bill")
            types = [t for t, v in ctx.cfg.pos_payment_type.items() if v["platform"] == platform]
            sales = ctx.pos_bill[ctx.pos_bill.payment_type.isin(types) & (ctx.pos_bill.status.str.lower() == "success")]
        if not win:
            continue
        sales = sales[sales.order_ts.dt.date.between(*win)]
        if not len(sales):
            continue
        total = float(sales.total.sum())
        credits = ctx.bank[(ctx.bank.payer_platform == platform) & ctx.bank.txn_date.between(win[0], win[1] + lag)]
        received = float(credits.deposit.sum())
        msg = (f"{platform.title()}: no payout report, so this cannot be reconciled order by order. Petpooja shows {len(sales)} sales worth {inr(total)} for "
               f"{win[0]:%d %b}–{win[1]:%d %b}; the bank received {inr(received)} in {len(credits)} credits between {win[0]:%d %b} and {(win[1] + lag):%d %b} "
               f"(includes payouts for the previous period's sales)." if len(credits) else
               f"{platform.title()}: no payout report and no bank credits identified for it. Petpooja shows {len(sales)} sales worth {inr(total)} for {win[0]:%d %b}–{win[1]:%d %b} "
               f"with nothing traceable in the bank (payer pattern not configured, or paid by another route).")
        out.append(Finding("PP-06", "info", msg, platform, "dining" if platform == "eazydiner" else "delivery", None, win[0], expected=money(total), actual=money(received),
                           details={"window": [str(win[0]), str(win[1])], "credits": [{"date": str(r.txn_date), "amount": money(r.deposit)} for r in credits.itertuples()]}))
    return out


@rule("POS-01", "Cash bills treated as complimentary")
def complimentary(ctx: Ctx) -> list[Finding]:
    """Petpooja 'Cash' payment-type bills are treated as complimentary (no receipt expected). Reported so the exclusion is visible, alongside UPI
    credits in the bank for the same period — those cannot be explained by any other source in this tool."""
    win = ctx.window("pos_bill")
    if not win:
        return []
    b = ctx.pos_bill[ctx.pos_bill.payment_type.isin(ctx.cfg.complimentary_types) & ctx.pos_bill.order_ts.dt.date.between(*win)]
    if not len(b):
        return []
    by_status = b.groupby("status").total.agg(["count", "sum"])
    upi = ctx.bank[ctx.bank.remarks.str.startswith("UPI/") & (ctx.bank.deposit > 0) & ctx.bank.txn_date.between(*win)]
    parts = ", ".join(f"{s}: {int(r['count'])} bills {inr(r['sum'])}" for s, r in by_status.iterrows())
    return [Finding("POS-01", "info", f"{len(b)} Petpooja bills ({inr(b.total.sum())}) paid as '{'/'.join(ctx.cfg.complimentary_types)}' are treated as complimentary and "
                    f"excluded ({parts}). In the same period {len(upi)} UPI credits totalling {inr(upi.deposit.sum())} reached the bank and are not explained by any "
                    f"report loaded here.", None, None, "cash-as-complimentary", win[0], expected=money(b.total.sum()), actual=money(upi.deposit.sum()),
                    diff=money(upi.deposit.sum() - b.total.sum()), details={"by_status": {s: [int(r["count"]), money(r["sum"])] for s, r in by_status.iterrows()}})]

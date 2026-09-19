"""Runs every registered rule over what is currently in the database and stores the run + findings."""
from __future__ import annotations

import json
from decimal import Decimal

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from . import config as cfgmod
from .rules import RULES, Ctx, Finding


def _frame(conn: psycopg.Connection, sql: str, ts_cols=()) -> pd.DataFrame:
    cur = conn.execute(sql)
    cols = [d.name for d in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)
    for c in cols:
        if df[c].dtype == object and len(df) and isinstance(df[c].dropna().iloc[0] if df[c].notna().any() else None, Decimal):
            df[c] = df[c].astype(float)
    for c in ts_cols:
        df[c] = pd.to_datetime(df[c])
    return df


def load_ctx(conn: psycopg.Connection, cfg) -> Ctx:
    ctx = Ctx(cfg=cfg,
              bank=_frame(conn, "select * from bank_txn"),
              pos_bill=_frame(conn, "select * from pos_bill", ["order_ts"]),
              pos_online=_frame(conn, "select * from pos_online_order", ["order_ts"]),
              plat=_frame(conn, "select * from plat_order", ["order_ts"]),
              adj=_frame(conn, "select * from plat_adjustment"))
    covers = {"bank_icici": ["bank"], "pos_bills": ["pos_bill"], "pos_online": ["pos_online"], "zomato_delivery": ["plat:zomato:delivery"],
              "zomato_dining": ["plat:zomato:dining"], "swiggy_orders": ["plat:swiggy:delivery", "plat:swiggy:dining"]}
    for r in conn.execute("select kind, min(period_from) lo, max(period_to) hi from upload group by kind").fetchall():
        for key in covers.get(r["kind"], []):
            ctx.coverage[key] = (r["lo"], r["hi"])
    return ctx


def _flow(ctx: Ctx) -> list[dict]:
    """Per platform/product for the compared window: POS sales → platform bill → fees → net payout."""
    rows = []
    for (platform, product), g in ctx.plat.groupby(["platform", "product"]):
        if product == "delivery":
            win = ctx.window("pos_online", f"plat:{platform}:{product}")
        else:
            win = ctx.window("pos_bill", f"plat:{platform}:{product}")
        if not win:
            continue
        g = g[g.order_ts.dt.date.between(*win) & (g.txn_type != "cancelled")]
        if product == "delivery":
            pos = ctx.pos_online[(ctx.pos_online.order_from == platform) & ctx.pos_online.order_ts.dt.date.between(*win) & (ctx.pos_online.status.str.lower() == "success")]
        else:
            types = [t for t, v in ctx.cfg.pos_payment_type.items() if v["platform"] == platform and v["product"] == product]
            pos = ctx.pos_bill[ctx.pos_bill.payment_type.isin(types) & ctx.pos_bill.order_ts.dt.date.between(*win) & (ctx.pos_bill.status.str.lower() == "success")]
        fees = float((g.commission_amt + g.other_fees + g.gst_on_fees).sum())
        bill = float(g.bill_amount.sum())
        rows.append({"platform": platform, "product": product, "from": str(win[0]), "to": str(win[1]), "pos_orders": len(pos), "pos_total": round(float(pos.total.sum()), 2),
                     "platform_orders": len(g), "platform_bill": round(bill, 2), "commission": round(float(g.commission_amt.sum()), 2),
                     "other_fees": round(float(g.other_fees.sum()), 2), "gst_on_fees": round(float(g.gst_on_fees.sum()), 2),
                     "take_pct": round(fees / bill * 100, 2) if bill else None, "net_payable": round(float(g.net_payable.sum()), 2),
                     "withheld_other": round(bill - fees - float(g.net_payable.sum()), 2)})
    return rows


def run(conn: psycopg.Connection, cfg=None) -> int:
    cfg = cfg or cfgmod.load()
    ctx = load_ctx(conn, cfg)
    findings: list[Finding] = []
    for r in RULES:
        findings.extend(r.fn(ctx))
    ctx.summary["flow"] = _flow(ctx)
    ctx.summary["rules"] = {r.id: {"title": r.title, "description": r.description} for r in RULES}
    cover = {k: [str(v[0]), str(v[1])] for k, v in ctx.coverage.items()}
    dumps = lambda o: json.dumps(o, default=str)
    with conn.transaction():
        run_id = conn.execute("insert into run (coverage, summary) values (%s,%s) returning id", (Jsonb(cover, dumps=dumps), Jsonb(ctx.summary, dumps=dumps))).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                "insert into finding (run_id, rule_id, severity, platform, product, ref, on_date, expected, actual, diff, message, details) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(run_id, f.rule_id, f.severity, f.platform, f.product, f.ref, f.on_date, f.expected, f.actual, f.diff, f.message,
                  Jsonb(f.details, dumps=dumps) if f.details is not None else None) for f in findings])
    return run_id

"""Rules on a tiny synthetic month with known answers (no real data)."""
from datetime import date, datetime
from decimal import Decimal as D

from psycopg.types.json import Jsonb

from recon import engine


def bank(conn, d, utr, amount, payer="zomato", product="delivery", n=[0]):
    n[0] += 1
    conn.execute("insert into bank_txn (row_hash, txn_date, remarks, deposit, utr, payer_platform, payer_product) values (%s,%s,%s,%s,%s,%s,%s)",
                 (f"h{n[0]}", d, f"NEFT-{utr}-X", amount, utr, payer, product))


def order(conn, platform, product, oid, ts, net, utr, pct=24, bill=118, commissionable=100, extras=None, other=D("1.93"), typ="sale"):
    comm = round(D(commissionable) * D(str(pct)) / 100, 2)
    gst = round((comm + other) * D("0.18"), 2)
    extras = extras if extras is not None else {"A": str(bill), "E": str(D(bill) - D(net)), "F": "0"}
    conn.execute("""insert into plat_order (platform, product, order_id, order_ts, txn_type, bill_amount, commissionable, commission_pct, commission_amt, other_fees,
                    gst_on_fees, net_payable, utr, extras) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                 (platform, product, oid, ts, typ, bill, commissionable, pct, comm, other, gst, net, utr, Jsonb(extras)))


def pos_online(conn, frm, no, ts, total, status="Success"):
    conn.execute("insert into pos_online_order (order_from, client_order_no, order_ts, status, total) values (%s,%s,%s,%s,%s)", (frm, no, ts, status, total))


def cover(conn, kind, lo=date(2026, 4, 1), hi=date(2026, 4, 30)):
    conn.execute("insert into upload (kind, filename, sha256, period_from, period_to) values (%s,%s,%s,%s,%s)", (kind, kind, kind + str(lo) + str(hi), lo, hi))


def run(conn, cfg, kinds=("zomato_delivery", "pos_online", "pos_bills", "zomato_dining", "swiggy_orders")):
    cover(conn, "bank_icici", date(2026, 4, 1), date(2026, 5, 15))
    for k in kinds:
        cover(conn, k)
    rid = engine.run(conn, cfg)
    out = {}
    for f in conn.execute("select * from finding where run_id=%s", (rid,)).fetchall():
        out.setdefault(f["rule_id"], []).append(f)
    return out, conn.execute("select summary from run where id=%s", (rid,)).fetchone()["summary"]


def test_clean_settlement_produces_no_gap(conn, cfg):
    order(conn, "zomato", "delivery", "O1", datetime(2026, 4, 10, 12), 60, "CITIN1")
    order(conn, "zomato", "delivery", "O2", datetime(2026, 4, 11, 12), 40, "CITIN1", extras={"A": "118", "E": "78", "F": "0"})
    bank(conn, date(2026, 4, 15), "CITIN1", 100)
    pos_online(conn, "zomato", "O1", datetime(2026, 4, 10, 12), 118)
    pos_online(conn, "zomato", "O2", datetime(2026, 4, 11, 12), 118)
    found, summary = run(conn, cfg)
    assert not [f for r in ("BK-01", "BK-02", "PI-01", "PI-02", "PI-03", "PI-04", "PP-01", "PP-02", "PP-03") for f in found.get(r, [])]
    assert summary["settlement"]["zomato:delivery"]["matched"] == 1


def test_wrong_commission_rate_is_a_gap(conn, cfg):
    order(conn, "zomato", "delivery", "O1", datetime(2026, 4, 10, 12), 60, "CITIN1", pct=30)
    bank(conn, date(2026, 4, 15), "CITIN1", 60)
    found, _ = run(conn, cfg)
    (f,) = found["PI-01"]
    assert f["severity"] == "gap" and f["actual"] == 30 and f["expected"] == 24
    assert f["diff"] == D("6.00")                   # 100 base x 6 pts


def test_missing_bank_credit_is_a_gap_but_after_statement_is_pending(conn, cfg):
    order(conn, "zomato", "delivery", "O1", datetime(2026, 4, 10, 12), 60, "CITIN_MISSING")
    order(conn, "zomato", "delivery", "O2", datetime(2026, 5, 25, 12), 60, "CITIN_LATE")      # settles after the 15 May statement end
    found, _ = run(conn, cfg)
    gaps = [f for f in found["BK-01"] if f["severity"] == "gap"]
    assert [g["ref"] for g in gaps] == ["CITIN_MISSING"] and gaps[0]["diff"] == D("-60.00")
    assert any(f["severity"] == "info" and f["ref"] == "after-statement" for f in found["BK-01"])


def test_bank_shortfall_equal_to_swiggy_ad_is_explained(conn, cfg):
    order(conn, "swiggy", "delivery", "S1", datetime(2026, 4, 8, 12), 1000, "AXIS1", pct=26, extras={"F": "1300", "S": "300", "V": "0", "X1": "0", "X2": "0"}, bill=1300)
    conn.execute("insert into plat_adjustment (platform, product, kind, ref, adj_date, period_to, amount) values ('swiggy','delivery','top_picks_ads','p','2026-04-05','2026-04-11',-206.5)")
    bank(conn, date(2026, 4, 15), "AXIS1", D("793.50"), payer="swiggy")
    found, summary = run(conn, cfg)
    assert "BK-02" not in found and "BK-04" not in found
    assert summary["settlement"]["swiggy:delivery"]["via_adjustment"] == 1


def test_unexplained_shortfall_is_a_gap_and_unmatched_ad_is_reported(conn, cfg):
    order(conn, "swiggy", "delivery", "S1", datetime(2026, 4, 8, 12), 1000, "AXIS1", pct=26, extras={"F": "1300", "S": "300", "V": "0", "X1": "0", "X2": "0"}, bill=1300)
    order(conn, "swiggy", "delivery", "S2", datetime(2026, 4, 20, 12), 500, "AXIS2", pct=26, extras={"F": "800", "S": "300", "V": "0", "X1": "0", "X2": "0"}, bill=800)
    conn.execute("insert into plat_adjustment (platform, product, kind, ref, adj_date, period_to, amount) values ('swiggy','delivery','top_picks_ads','p','2026-04-05','2026-04-11',-206.5)")
    bank(conn, date(2026, 4, 15), "AXIS1", 1000, payer="swiggy")           # ad never deducted
    bank(conn, date(2026, 4, 28), "AXIS2", 450, payer="swiggy")            # 50 short, no adjustment explains it
    found, _ = run(conn, cfg)
    assert [f["ref"] for f in found["BK-02"] if f["severity"] == "gap"] == ["AXIS2"]
    assert len(found["BK-04"]) == 1 and found["BK-04"][0]["diff"] == D("206.50")


def test_bank_credit_beyond_report_edge_is_boundary_not_gap(conn, cfg):
    order(conn, "zomato", "delivery", "O1", datetime(2026, 4, 1, 9), 60, "CITIN1")          # first day of the uploaded report
    bank(conn, date(2026, 4, 8), "CITIN1", 90)                                             # bank paid 30 more: Mar 30-31 orders not in file
    found, _ = run(conn, cfg)
    (f,) = found["BK-02"]
    assert f["severity"] == "info" and f["diff"] == D("30.00")


def test_pos_order_missing_from_payout_and_amount_mismatch(conn, cfg):
    order(conn, "zomato", "delivery", "O1", datetime(2026, 4, 10, 12), 60, "CITIN1", bill=118)
    bank(conn, date(2026, 4, 15), "CITIN1", 60)
    pos_online(conn, "zomato", "O1", datetime(2026, 4, 10, 12), 130)                       # 12 rupees off
    pos_online(conn, "zomato", "O9", datetime(2026, 4, 11, 12), 200)                       # never paid
    found, _ = run(conn, cfg)
    assert [f["ref"] for f in found["PP-01"]] == ["O9"]
    assert [f["ref"] for f in found["PP-03"]] == ["O1"] and found["PP-03"][0]["diff"] == D("-12.00")


def test_one_sided_dinein_and_split_payment(conn, cfg):
    conn.execute("insert into pos_bill (invoice_no, order_ts, payment_type, status, total) values ('1','2026-04-10 13:00','Other [zomato]()','Success',300)")
    conn.execute("insert into pos_bill (invoice_no, order_ts, payment_type, status, total) values ('2','2026-04-10 13:02','Other [zomato]()','Success',200)")
    conn.execute("insert into pos_bill (invoice_no, order_ts, payment_type, status, total) values ('3','2026-04-10 15:00','Other [zomato]()','Success',999)")
    order(conn, "zomato", "dining", "T1", datetime(2026, 4, 10, 13, 5), 450, "CITIN9", bill=500, commissionable=500, extras={"tips": "0", "adjustment": "0"}, pct=6, other=D(0))
    found, _ = run(conn, cfg)
    kinds = sorted((f["severity"], f["message"][:24]) for f in found["PP-04"])
    assert any(f["severity"] == "info" and f["message"].startswith("Split payment") for f in found["PP-04"])
    gaps = [f for f in found["PP-04"] if f["severity"] == "gap"]
    assert [g["ref"] for g in gaps] == ["3"]                                                 # 999 bill has no platform transaction


def test_refund_shares_transaction_id_with_its_sale(conn, cfg):
    order(conn, "zomato", "dining", "T1", datetime(2026, 4, 10, 13), 100, "CITIN9", bill=100, extras={"tips": "0", "adjustment": "0"})
    order(conn, "zomato", "dining", "T1", datetime(2026, 4, 12, 13), -100, "CITIN9", bill=-100, typ="refund", extras={"tips": "0", "adjustment": "0"})
    assert conn.execute("select count(*) n from plat_order").fetchone()["n"] == 2


def test_platform_report_that_omits_the_last_days_does_not_hide_missing_orders(conn, cfg):
    """Coverage comes from the period the report states (Apr 1-30), not from its last order (Apr 10)."""
    order(conn, "zomato", "delivery", "O1", datetime(2026, 4, 10, 12), 60, "CITIN1")
    bank(conn, date(2026, 4, 15), "CITIN1", 60)
    pos_online(conn, "zomato", "O1", datetime(2026, 4, 10, 12), 118)
    pos_online(conn, "zomato", "O_LATE", datetime(2026, 4, 29, 12), 300)
    found, _ = run(conn, cfg)
    assert [f["ref"] for f in found["PP-01"]] == ["O_LATE"]

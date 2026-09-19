-- Every uploaded file, for audit and idempotency (same bytes are never ingested twice).
create table if not exists upload (
  id           serial primary key,
  kind         text not null,
  filename     text not null,
  sha256       text not null unique,
  uploaded_at  timestamptz not null default now(),
  period_from  date,
  period_to    date,
  rows_read    int not null default 0,
  rows_new     int not null default 0,
  note         text
);

-- ICICI statement lines. Bank "Tran. Id" is NOT unique, so rows are de-duplicated on a content hash;
-- overlapping statement uploads therefore never double count.
create table if not exists bank_txn (
  id             bigserial primary key,
  row_hash       text not null unique,
  upload_id      int references upload(id),
  tran_id        text,
  txn_date       date not null,
  posted_at      timestamp,
  remarks        text not null,
  withdrawal     numeric(14,2) not null default 0,
  deposit        numeric(14,2) not null default 0,
  balance        numeric(14,2),
  utr            text,              -- NEFT reference embedded in narration
  payer_platform text,              -- zomato | swiggy | eazydiner (from config/payers.toml)
  payer_product  text               -- delivery | dining | null
);
create index if not exists bank_txn_utr on bank_txn(utr);
create index if not exists bank_txn_date on bank_txn(txn_date);

-- Petpooja "Order Summary" (one row per invoice, collapsed from the item-level report).
create table if not exists pos_bill (
  id            bigserial primary key,
  upload_id     int references upload(id),
  invoice_no    text not null,
  order_ts      timestamp not null,
  payment_type  text not null,
  status        text not null,
  order_type    text,
  area          text,
  total         numeric(14,2) not null,
  unique (invoice_no, order_ts)
);
create index if not exists pos_bill_ts on pos_bill(order_ts);

-- Petpooja "Sales Report: Online Platforms" (delivery orders that carry the platform's order id).
create table if not exists pos_online_order (
  id              bigserial primary key,
  upload_id       int references upload(id),
  order_from      text not null,           -- zomato | swiggy | ownly
  client_order_no text not null,
  invoice_no      text,
  order_ts        timestamp not null,
  status          text not null,
  total           numeric(14,2) not null,
  unique (order_from, client_order_no)
);

-- One row per platform order / transaction, normalised from Zomato and Swiggy payout reports.
create table if not exists plat_order (
  id              bigserial primary key,
  upload_id       int references upload(id),
  platform        text not null,           -- zomato | swiggy
  product         text not null,           -- delivery | dining
  order_id        text not null,
  order_ts        timestamp not null,
  txn_type        text not null,           -- sale | refund | cancelled
  bill_amount     numeric(14,2) not null,  -- comparable to the POS bill total
  commissionable  numeric(14,2),           -- base the platform applied its % to
  commission_pct  numeric(7,3),
  commission_amt  numeric(14,2),
  other_fees      numeric(14,2) not null default 0,  -- payment-mechanism etc.
  gst_on_fees     numeric(14,2),
  net_payable     numeric(14,2) not null,  -- what the platform says it pays out for this row
  utr             text,
  settlement_date date,
  extras          jsonb not null default '{}',  -- raw report components the rules re-derive from
  unique (platform, product, order_id, txn_type)   -- a refund reuses its sale's transaction id
);
create index if not exists plat_order_utr on plat_order(utr);

-- Non-order deductions/additions (ads, etc.). Zomato carries the UTR; Swiggy does not.
create table if not exists plat_adjustment (
  id          bigserial primary key,
  upload_id   int references upload(id),
  platform    text not null,
  product     text not null,
  kind        text not null,
  ref         text not null,
  adj_date    date not null,
  period_to   date,
  amount      numeric(14,2) not null,      -- negative = deduction
  utr         text,
  unique (platform, product, kind, ref, adj_date)
);

create table if not exists run (
  id          serial primary key,
  started_at  timestamptz not null default now(),
  coverage    jsonb not null default '{}',
  summary     jsonb not null default '{}'
);

create table if not exists finding (
  id        bigserial primary key,
  run_id    int not null references run(id) on delete cascade,
  rule_id   text not null,
  severity  text not null check (severity in ('gap','warn','info')),
  platform  text,
  product   text,
  ref       text,                          -- UTR / order id / bucket
  on_date   date,
  expected  numeric(14,2),
  actual    numeric(14,2),
  diff      numeric(14,2),
  message   text not null,
  details   jsonb
);
create index if not exists finding_run on finding(run_id, rule_id);

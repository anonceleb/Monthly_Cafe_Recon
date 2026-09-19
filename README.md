# recon-v2 — small, rule-based reconciliation

Petpooja (what you sold) → Zomato / Swiggy payout reports (what they say they owe) → ICICI statement (what arrived).
Reports are loaded into Postgres, written rules run over them, and the gaps come out as findings.

## Run it

```bash
cd "recon-v2"
uv sync
uv run python -m recon serve          # http://127.0.0.1:8790  → Upload, then read Overview / Findings
```

Database `kredo_recon` on local Postgres (override with `RECON_DSN`). The schema is created on first use.

CLI, same code path as the web page:

```bash
uv run python -m recon ingest "<folder or files>"   # detect, parse, load
uv run python -m recon run                          # run every rule
uv run python -m recon reset                        # wipe the database
uv run pytest                                       # uses kredo_recon_test, never the real DB
```

## Routine use

Upload whatever you have, in any order. The file type is recognised from its layout, not its name.
Re-uploading is safe: identical files are skipped, overlapping periods update rows instead of duplicating them.
Each upload re-runs every rule over everything held, so a new month adds to the picture.

| File | Notes |
|---|---|
| ICICI Detailed Statement (xlsx) | rows de-duplicated on content — the bank's `Tran. Id` is not unique |
| Petpooja Order Summary (csv) + Sales Report: Online Platforms (xlsx) | order summary for dine-in payment types, online report for platform order ids |
| Zomato Settlement Report (delivery) + Consolidated payout report (dining) | |
| Swiggy consolidate-annexure-orders / -adjustment (csv) | orders csv carries delivery **and** Dineout |

Not needed: Swiggy monthly annexure xlsx (duplicates the csvs), Swiggy invoice summary, Petpooja item/purchase reports.
EazyDiner and Ownly have no payout report, so they are compared at bank level only (rule PP-06).

## How a settlement is reconciled

The unit is the **UTR** (one bank credit), not the calendar month — weekly payouts straddle month ends.
`bank credit = Σ order net payable + adjustments (ads etc.)`. Swiggy reports ads without saying which payout they
came out of, so each is assigned to the earliest bank credit whose shortfall equals it.

## Rules (`recon/rules/`, plain Python; `config/*.toml` holds the numbers)

- **PI-01..05** payout integrity — commission %, fee arithmetic, GST on fees, net vs the report's own components, missing UTR
- **PP-01..06 / POS-01** Petpooja vs payout — order-id match (delivery), amount+time match with split-payment and mis-tag detection (dine-in), declared gaps
- **BK-01..04** payout vs bank — missing credit, amount difference, unexplained credit, unmatched adjustment

Severity: **gap** needs action · **warn** worth a look · **info** explained (period edge, declared gap, split payment).
Only the overlap of what each side covers is compared; coverage comes from the period a file *states*
(Zomato, Petpooja online) or, for Swiggy csvs that state none, the rows snapped to month edges.

## Things to confirm

- `config/rates.toml` rates were **read off the April reports**, not from your agreements.
- `config/payers.toml`: Petpooja `Cash` is treated as complimentary, per your instruction.

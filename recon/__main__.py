"""CLI:  uv run python -m recon ingest <files or folders…> | run | reset | serve"""
from __future__ import annotations

import sys
from pathlib import Path

from . import db, engine, ingest


def _files(paths: list[str]):
    for p in map(Path, paths):
        yield from sorted(f for f in p.rglob("*") if f.suffix.lower() in (".csv", ".xlsx", ".xlsm")) if p.is_dir() else [p]


def main(argv: list[str]) -> int:
    cmd, args = (argv[0] if argv else "help"), argv[1:]
    if cmd == "serve":
        import uvicorn
        uvicorn.run("recon.web:app", host="127.0.0.1", port=int(args[0]) if args else 8790, reload=False)
        return 0
    conn = db.connect()
    db.init_schema(conn)
    if cmd == "reset":
        db.reset(conn); print("database wiped and schema recreated")
    elif cmd == "ingest":
        for f in _files(args):
            r = ingest.ingest(conn, f)
            print(f"{r['status']:9s} {str(r['label'] or r['kind']):32s} {f.name[:50]:50s} {r['rows_new']}/{r['rows_read']}  {r['note'] or ''}"[:200])
    elif cmd == "run":
        rid = engine.run(conn)
        print(f"run {rid}")
        for r in conn.execute("select rule_id, severity, count(*) n, coalesce(sum(abs(diff)),0) amt from finding where run_id=%s group by 1,2 order by 1,2", (rid,)).fetchall():
            print(f"  {r['rule_id']:7s} {r['severity']:5s} n={r['n']:4d}  ₹{r['amt']:>12,.2f}")
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

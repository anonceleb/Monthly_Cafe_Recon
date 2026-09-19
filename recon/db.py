import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("RECON_DSN", "host=localhost dbname=kredo_recon user=ashwin")
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(dsn: str | None = None) -> psycopg.Connection:
    # autocommit: every `with conn.transaction():` is then a real BEGIN/COMMIT, not a savepoint inside a never-committed transaction.
    return psycopg.connect(dsn or DSN, row_factory=dict_row, autocommit=True)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()


def reset(conn: psycopg.Connection) -> None:
    """Wipe everything (dev/test only)."""
    conn.execute("drop schema public cascade; create schema public;")
    conn.commit()
    init_schema(conn)

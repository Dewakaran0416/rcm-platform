"""Small SQLite helpers shared by the modules."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def rows(con: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def replace_table(con: sqlite3.Connection, ddl: str, table: str, records: list[dict]) -> None:
    """(Re)create a module output table and load records into it."""
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.executescript(ddl)
    if records:
        cols = list(records[0].keys())
        con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in records])
    con.commit()


def has_source(con, name: str) -> bool:
    """Was this optional input supplied (authorizations, fee_schedule, coverages, synthetic ...)?"""
    try:
        return con.execute("SELECT 1 FROM data_sources WHERE source = ?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return True

"""Write a Dataset to SQLite and to Excel-friendly CSVs."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .generator import Dataset

SCHEMA = Path(__file__).with_name("schema.sql")


def to_sqlite(ds: Dataset, path: str | Path) -> Path:
    path = Path(path)
    if path.exists():
        try:
            path.unlink()
        except PermissionError as e:
            raise SystemExit(f"{path} is open in another program (a DB viewer or Excel?). Close it and run again.") from e
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for table in Dataset.TABLES:
        rows = getattr(ds, table)
        if not rows:
            continue
        cols = list(rows[0].keys())
        con.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    con.close()
    return path


def to_csv(ds: Dataset, folder: str | Path) -> Path:
    """One CSV per table. *_cents columns are written as dollars (2 dp) for Excel."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for table in Dataset.TABLES:
        rows = getattr(ds, table)
        if not rows:
            continue
        cols = list(rows[0].keys())
        out_cols = [c.replace("_cents", "") for c in cols]
        with open(folder / f"{table}.csv", "w", newline="", encoding="utf-8-sig") as fh:  # BOM for Excel
            w = csv.writer(fh)
            w.writerow(out_cols)
            for r in rows:
                w.writerow([f"{r[c] / 100:.2f}" if c.endswith("_cents") and r[c] is not None else
                            ("" if r[c] is None else r[c]) for c in cols])
    return folder

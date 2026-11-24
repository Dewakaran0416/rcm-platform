"""Ingestion: load a company's own data into the platform.

    from rcm.ingest import ingest
    report = ingest(out="data", csv_dir="exports", x835_dir="era", deidentify_key="...")

Sources (use either claims source, plus 835s if you have them):
  csv_dir   folder of template CSV/Excel files   (python -m rcm templates --out templates)
  x837_dir  folder of 837P files
  x835_dir  folder of 835 files, applied to the loaded claims
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..db import connect, replace_table
from ..store import to_sqlite
from ..validate import validate
from ..x12 import x835, x837
from .deidentify import deidentify, new_key
from .loader import Issues, build_dataset, read_folder
from .remits import apply_835
from .templates import TEMPLATES, write_templates

ISSUES_DDL = """
CREATE TABLE ingest_issues (
    file TEXT, row INTEGER, column_name TEXT, severity TEXT NOT NULL, message TEXT NOT NULL
);
"""


def ingest(out: str | Path = "data", csv_dir: str | Path | None = None, x837_dir: str | Path | None = None,
           x835_dir: str | Path | None = None, deidentify_key: str | None = None, today: date | None = None,
           log=print) -> dict:
    if bool(csv_dir) == bool(x837_dir):
        raise ValueError("Give exactly one claims source: csv_dir or x837_dir")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    issues = Issues()
    if csv_dir:
        log(f"Reading templates from {csv_dir}")
        records, source = read_folder(csv_dir, issues), "csv"
    else:
        log(f"Reading 837 files from {x837_dir}")
        records, source = x837.parse_837_folder(x837_dir), "837"
        for c in records["claims"]:
            if c["_bad"]:
                issues.add("837", None, "claim_id", "error",
                           f"claim {c['claim_id']} is missing a date of service, billing NPI, payer or diagnosis; excluded")
        for p in records["patients"]:
            if p["_bad"]:
                issues.add("837", None, "dob", "error", "a patient/subscriber has no date of birth; their claims are excluded")
    ds, stats = build_dataset(records, issues, source, today=today)
    db = to_sqlite(ds, out / "rcm.sqlite")
    con = connect(db)
    report = {"loaded": stats}
    if x835_dir:
        log(f"Applying 835 files from {x835_dir}")
        payments = x835.parse_folder_any(x835_dir)
        report["835"] = apply_835(con, payments)
        if report["835"]["unmatched_claims"]:
            issues.add("835", None, "claim_id", "warning",
                       f"{report['835']['unmatched_claims']} claims on the 835s were not in the loaded claims "
                       "(load the matching claims, or they were billed outside this extract)")
    if deidentify_key:
        log("De-identifying")
        report["deidentified"] = deidentify(con, deidentify_key, today)
    # Issue messages name original claim numbers so the billing team can fix the source rows.
    # On a de-identified load they stay out of the dataset: separate file, clearly marked internal.
    if deidentify_key:
        issues_file = out / "ingest_issues_INTERNAL_contains_claim_numbers.csv"
        report.get("835", {}).pop("unmatched_examples", None)
    else:
        issues_file = out / "ingest_issues.csv"
        replace_table(con, ISSUES_DDL, "ingest_issues",
                      [{"file": i["file"], "row": i["row"], "column_name": i["column"], "severity": i["severity"],
                        "message": i["message"]} for i in issues.items])
    con.close()
    issues.write_csv(issues_file)
    checks = validate(db)
    report["data_checks"] = {k: v for k, v in checks.items() if v}
    report["issues"] = {"errors": len(issues.errors),
                        "warnings": sum(i["severity"] == "warning" for i in issues.items),
                        "file": str(issues_file)}
    (out / "ingest_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


__all__ = ["ingest", "write_templates", "TEMPLATES", "deidentify", "new_key", "apply_835"]

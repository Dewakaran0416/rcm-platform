"""End-to-end runs: run() generates synthetic data first; run_modules() works on any database (synthetic or loaded)."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from . import analytics, claim_status, dashboard, posting, predict, recovery, scrubber
from .db import connect, has_source
from .generator import Config, generate
from .store import to_sqlite
from .validate import validate
from .x12 import x276, x835, x837


def run(out: str | Path = "data", cfg: Config | None = None, csv: bool = True, model: bool = True,
        registered_vendor_payers: set[str] | None = None, log=print) -> dict:
    """Synthetic end-to-end run: generate the data, then every module."""
    cfg = cfg or Config()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log("[  0.0s] Module 0  generate synthetic data")
    db = to_sqlite(generate(cfg), out / "rcm.sqlite")
    failures = {k: n for k, n in validate(db).items() if n}
    if failures:
        raise RuntimeError(f"Foundation validation failed: {failures}")
    report = run_modules(db, out, as_of=cfg.as_of, csv=csv, model=model,
                         registered_vendor_payers=registered_vendor_payers, log=log, t0=t0)
    report["seed"] = cfg.seed
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def default_as_of(con) -> date:
    """Latest date seen in the data (payment, submission or service), so loaded history isn't aged against today."""
    d = con.execute("""SELECT MAX(x) FROM (SELECT MAX(adjudication_date) AS x FROM claims
                       UNION ALL SELECT MAX(submission_date) FROM claims
                       UNION ALL SELECT MAX(date_of_service) FROM encounters)""").fetchone()[0]
    return date.fromisoformat(d) if d else date.today()


def run_modules(db: str | Path, out: str | Path | None = None, as_of: date | None = None, csv: bool = True,
                model: bool = True, registered_vendor_payers: set[str] | None = None, x835_dir: str | Path | None = None,
                x277_dir: str | Path | None = None, log=print, t0: float | None = None) -> dict:
    """Modules 1-6 on an existing database: synthetic, or a company's data loaded with `rcm ingest`."""
    db = Path(db)
    out = Path(out) if out else db.parent
    out.mkdir(parents=True, exist_ok=True)
    t0 = t0 or time.time()
    con = connect(db)
    synthetic = has_source(con, "synthetic")
    as_of = as_of or default_as_of(con)
    steps = {}

    def step(name):
        log(f"[{time.time() - t0:5.1f}s] {name}")

    step("Module 1  scrub claims + write 837P")
    scrubber.run(con)
    steps["scrubber"] = scrubber.evaluate(con)
    # synthetic: every sent claim; real: only claims not yet billed (the ones you'd actually send next)
    steps["scrubber"]["claims_in_837"] = sum(x837.write_all(con, out / "x12" / "837", only_unbilled=not synthetic).values())

    step("Module 2  read 835s and post payments")
    # posting re-reads 835 files: the client's own if given, otherwise rebuilt from the stored payments
    # (needed after de-identification, when the original files no longer match the hashed claim IDs)
    if x835_dir:
        payments = x835.parse_folder_any(x835_dir)
    else:
        x835.write_all(con, out / "x12" / "835")
        payments = x835.parse_folder(out / "x12" / "835")
    steps["posting"] = posting.post(con, payments)
    steps["posting"].update(posting.evaluate(con))

    if model:
        step("Module 3  train + score denial model")
        try:
            steps["prediction"] = predict.train_and_score(con)
        except SystemExit as e:          # scikit-learn missing
            steps["prediction"] = {"skipped": str(e)}

    step("Module 4  build recovery queue + draft appeals")
    steps["recovery"] = recovery.run(con, as_of, out / "appeals")
    steps["recovery"].update(recovery.evaluate(con))

    step("Module 5  claim status (276/277) + channel routing")
    responses = None
    if x277_dir:
        responses = {}
        for f in sorted(Path(x277_dir).rglob("*")):
            if f.is_file():
                text = f.read_text(encoding="ascii", errors="replace")
                if text.lstrip().startswith("ISA") and ("*277*" in text[:600] or "005010X212" in text[:600]):
                    responses.update({r["claim_id"]: r for r in x276.parse_277(text)})
    steps["status"] = claim_status.run(con, as_of, out / "x12" / "status", registered_vendor_payers,
                                       responses_277=responses)

    step("Module 6  KPIs + dashboard")
    kpis = analytics.compute(con, as_of)
    dashboard.write(con, kpis, steps, out / "dashboard.html")
    if csv:
        (out / "csv").mkdir(parents=True, exist_ok=True)
        _export_outputs(con, out / "csv", base_tables=True)
    report = {"as_of": as_of.isoformat(), "data": "synthetic" if synthetic else "loaded",
              "kpis": kpis, "modules": steps, "seconds": round(time.time() - t0, 1)}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    step("done")
    con.close()
    return report


BASE_TABLES = ["payers", "providers", "patients", "coverages", "authorizations", "encounters", "claims", "claim_lines",
               "claim_events", "remits", "remit_claims", "remit_adjustments", "fee_schedule"]


def _export_outputs(con, folder: Path, base_tables: bool = False) -> None:
    """Module output tables (and optionally the base tables) as CSV, so everything opens straight in Excel."""
    import csv
    existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables = (BASE_TABLES if base_tables else []) + ["scrub_edits", "postings", "underpayments", "denial_queue",
                                                     "denial_risk", "recovery_queue", "status_worklist"]
    for table in [t for t in tables if t in existing]:
        cur = con.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        with open(folder / f"{table}.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow([c.replace("_cents", "") for c in cols])
            for r in cur:
                w.writerow([(f"{v / 100:.2f}" if c.endswith("_cents") and v is not None else ("" if v is None else v))
                            for c, v in zip(cols, r)])

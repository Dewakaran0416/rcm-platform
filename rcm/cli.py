"""Command line.

  python -m rcm pipeline                                   synthetic data, every module
  python -m rcm templates --out templates [--example]      blank (or filled example) import templates
  python -m rcm ingest --csv templates --x835 era --out client --deidentify --run
  python -m rcm ingest --x837 claims837 --x835 era --out client --run
  python -m rcm pipeline --db client/rcm.sqlite            modules on loaded data
  python -m rcm export-templates --db client/rcm.sqlite --out flat
  python -m rcm validate client/rcm.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .generator import Config, generate
from .store import to_csv, to_sqlite
from .validate import summary, validate


def _kpi_line(rep: dict, out: Path) -> None:
    k = rep["kpis"]
    print(f"\nNet collection rate {k['net_collection_rate']:.1%} | initial denial rate {k['initial_denial_rate']:.1%} | "
          f"clean claim rate {k['clean_claim_rate']:.1%} | days in A/R {k['days_in_ar']:.1f}")
    print(f"Dashboard: {out / 'dashboard.html'}   Report: {out / 'report.json'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rcm", description="RCM platform")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Generate a synthetic dataset only")
    g.add_argument("--out", default="data", help="output folder (default: data)")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--patients", type=int, default=2500)
    g.add_argument("--encounters", type=int, default=12000)
    g.add_argument("--as-of", default="2026-11-15", help="snapshot date YYYY-MM-DD")
    g.add_argument("--no-csv", action="store_true")

    pl = sub.add_parser("pipeline", help="Run every module: on synthetic data, or on a loaded database with --db")
    pl.add_argument("--db", help="run on this existing database (from `rcm ingest`) instead of generating data")
    pl.add_argument("--out", help="output folder (default: data, or the --db folder)")
    pl.add_argument("--seed", type=int, default=42)
    pl.add_argument("--patients", type=int, default=2500)
    pl.add_argument("--encounters", type=int, default=12000)
    pl.add_argument("--as-of", help="snapshot date YYYY-MM-DD (default: 2026-11-15 synthetic, latest date in the data with --db)")
    pl.add_argument("--x835", help="with --db: post from these 835 files instead of the stored payments")
    pl.add_argument("--x277", help="with --db: folder of payer 277 responses to route claim-status follow-up")
    pl.add_argument("--no-csv", action="store_true")
    pl.add_argument("--no-model", action="store_true", help="skip Module 3 (no scikit-learn needed)")
    pl.add_argument("--ai-vendor-registered", nargs="*", default=[], metavar="PAYER_ID",
                    help="payers where your AI vendor + BAA are on file (unlocks AI voice for 'Conditional' payers)")

    t = sub.add_parser("templates", help="Write the import templates and a column guide")
    t.add_argument("--out", default="templates")
    t.add_argument("--example", action="store_true", help="fill them with a small synthetic example instead of blanks")

    ing = sub.add_parser("ingest", help="Load a company's data (templates or 837s, plus 835s)")
    src = ing.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="folder of filled templates (CSV or .xlsx)")
    src.add_argument("--x837", help="folder of 837P files")
    ing.add_argument("--x835", help="folder of 835 files to apply to the claims")
    ing.add_argument("--out", default="client_data")
    ing.add_argument("--deidentify", action="store_true", help="hash IDs, drop names, DOB to year, ZIP to 3 digits")
    ing.add_argument("--key", help="de-identification key (or set RCM_DEID_KEY); generated if not given")
    ing.add_argument("--run", action="store_true", help="run every module on the loaded data afterwards")
    ing.add_argument("--no-model", action="store_true")
    ing.add_argument("--as-of", help="snapshot date for the modules (default: latest date in the data)")

    ex = sub.add_parser("export-templates", help="Write a database out in the import-template format")
    ex.add_argument("--db", required=True)
    ex.add_argument("--out", required=True)

    v = sub.add_parser("validate", help="Run integrity checks on a database")
    v.add_argument("db")

    args = ap.parse_args(argv)

    if args.cmd == "generate":
        cfg = Config(seed=args.seed, n_patients=args.patients, n_encounters=args.encounters,
                     as_of=date.fromisoformat(args.as_of))
        ds = generate(cfg)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        db = to_sqlite(ds, out / "rcm.sqlite")
        if not args.no_csv:
            to_csv(ds, out / "csv")
        print(json.dumps(summary(db), indent=2))
        failures = {k: n for k, n in validate(db).items() if n}
        if failures:
            print("VALIDATION FAILED:", json.dumps(failures, indent=2), file=sys.stderr)
            return 1
        print(f"\nAll checks passed. Wrote {db}" + ("" if args.no_csv else f" and {out / 'csv'}"))
        return 0

    if args.cmd == "pipeline":
        from .pipeline import run, run_modules
        if args.db:
            out = Path(args.out) if args.out else Path(args.db).parent
            rep = run_modules(args.db, out, as_of=date.fromisoformat(args.as_of) if args.as_of else None,
                              csv=not args.no_csv, model=not args.no_model,
                              registered_vendor_payers=set(args.ai_vendor_registered),
                              x835_dir=args.x835, x277_dir=args.x277)
        else:
            out = Path(args.out or "data")
            cfg = Config(seed=args.seed, n_patients=args.patients, n_encounters=args.encounters,
                         as_of=date.fromisoformat(args.as_of or "2026-11-15"))
            rep = run(out, cfg, csv=not args.no_csv, model=not args.no_model,
                      registered_vendor_payers=set(args.ai_vendor_registered))
        _kpi_line(rep, out)
        return 0

    if args.cmd == "templates":
        from .ingest import write_templates
        out = Path(args.out)
        write_templates(out)
        if args.example:
            import tempfile
            from .ingest.export import export_templates
            with tempfile.TemporaryDirectory() as tmp:
                db = to_sqlite(generate(Config(seed=1, n_patients=50, n_encounters=200)), Path(tmp) / "ex.sqlite")
                export_templates(db, out)
            print(f"Example templates (synthetic) and TEMPLATE_GUIDE.md written to {out}")
        else:
            print(f"Blank templates and TEMPLATE_GUIDE.md written to {out}")
        return 0

    if args.cmd == "ingest":
        from .ingest import ingest, new_key
        key = None
        if args.deidentify:
            key = args.key or os.environ.get("RCM_DEID_KEY")
            if not key:
                key = new_key()
                print("De-identification key (store it somewhere safe, NOT next to the data; reuse it so\n"
                      f"later loads link to the same patients):\n\n    {key}\n")
        out = Path(args.out)
        rep = ingest(out, csv_dir=args.csv, x837_dir=args.x837, x835_dir=args.x835, deidentify_key=key)
        L = rep["loaded"]
        print(f"\nLoaded {L['claims_loaded']:,} claims ({L['lines_loaded']:,} lines), {L['patients']:,} patients, "
              f"{L['payers']} payers, {L['providers']} providers. Excluded {L['claims_excluded']} claims with errors.")
        if "835" in rep:
            r = rep["835"]
            print(f"835s: {r['payments']:,} payments applied to {r['claims_applied']:,} claims; "
                  f"{r['unmatched_claims']} claims on the 835s weren't in the load; {r['reversals']} reversals skipped.")
        if rep.get("deidentified"):
            print("De-identified: IDs hashed, names removed, DOB to year, ZIP to 3 digits.")
        print(f"Issues: {rep['issues']['errors']} errors, {rep['issues']['warnings']} warnings -> {rep['issues']['file']}")
        if rep["data_checks"]:
            print("Data checks to review:", json.dumps(rep["data_checks"]))
        if args.run:
            from .pipeline import run_modules
            mrep = run_modules(out / "rcm.sqlite", out, as_of=date.fromisoformat(args.as_of) if args.as_of else None,
                               model=not args.no_model)
            _kpi_line(mrep, out)
        else:
            print(f"\nNext: python -m rcm pipeline --db {out / 'rcm.sqlite'}")
        return 0

    if args.cmd == "export-templates":
        from .ingest.export import export_templates
        counts = export_templates(args.db, args.out)
        print(json.dumps(counts, indent=2))
        return 0

    if args.cmd == "validate":
        res = validate(args.db)
        for k, n in res.items():
            print(f"{'PASS' if n == 0 else 'CHECK':5}  {k}" + (f"  ({n} rows)" if n else ""))
        return 0 if not any(res.values()) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

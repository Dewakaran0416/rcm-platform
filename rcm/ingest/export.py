"""Write a database back out in the import-template format.

Useful two ways: run it on the synthetic data to get a filled example of every
template, and use it to hand a client their cleaned data back in a flat form.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..db import connect, rows
from .templates import TEMPLATES

QUERIES = {
    "patients": "SELECT patient_id, first_name, last_name, dob, sex, state, zip, mrn FROM patients",
    "claims": """SELECT c.claim_id, c.patient_id, c.payer_id, p.payer_name, cv.member_id, c.billing_npi,
                        e.rendering_npi, e.date_of_service, e.place_of_service, c.frequency_code, c.dx1, c.dx2, c.dx3, c.dx4,
                        c.prior_auth_number, c.submission_date, c.payer_claim_number
                 FROM claims c JOIN encounters e USING (encounter_id) JOIN payers p USING (payer_id)
                 JOIN coverages cv ON cv.coverage_id = c.coverage_id""",
    "claim_lines": """SELECT claim_id, line_number, cpt_code, modifier1, modifier2, units, charge_cents AS charge, dx_pointer
                      FROM claim_lines""",
    "payers": """SELECT payer_id, payer_name, line_of_business, state, timely_filing_days, appeal_limit_days, ai_call_status
                 FROM payers""",
    "providers": """SELECT provider_npi AS npi, provider_name AS name, provider_type, specialty, taxonomy_code, tax_id, state,
                           billing_npi FROM providers""",
    "coverages": """SELECT patient_id, payer_id, member_id, group_number, coverage_rank, effective_date, termination_date,
                           copay_cents AS copay, deductible_cents AS deductible, coinsurance_pct FROM coverages""",
    "authorizations": "SELECT patient_id, payer_id, cpt_code, auth_number, status, valid_from, valid_to FROM authorizations",
    "fee_schedule": "SELECT payer_id, cpt_code, allowed_cents AS allowed_amount, allowed_pct FROM fee_schedule",
}
MONEY = {"charge", "copay", "deductible", "allowed_amount"}


def export_templates(db: str | Path, folder: str | Path) -> dict[str, int]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    counts = {}
    for table, cols in TEMPLATES.items():
        data = rows(con, QUERIES[table])
        names = [c[0] for c in cols]
        with open(folder / f"{table}.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(names)
            for r in data:
                w.writerow([("" if r.get(c) is None else f"{r[c] / 100:.2f}" if c in MONEY else r[c]) for c in names])
        counts[table] = len(data)
    con.close()
    return counts

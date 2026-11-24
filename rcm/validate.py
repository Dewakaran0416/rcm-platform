"""Integrity checks every generated dataset must pass.

Run against the SQLite file so the same checks work on any data that
lands in the model later (including real data loaded by a client).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CHECKS = {
    # referential integrity is enforced by FKs; these are business rules
    "line charges sum to claim charge":
        """SELECT c.claim_id FROM claims c JOIN claim_lines l USING (claim_id)
           GROUP BY c.claim_id HAVING SUM(l.charge_cents) <> MAX(c.total_charge_cents)""",
    "each paid line balances: charge = paid + adjustments (per payment)":
        """SELECT rl.remit_id, rl.claim_id, rl.line_number FROM remit_lines rl
           LEFT JOIN remit_adjustments a ON a.remit_id = rl.remit_id AND a.claim_id = rl.claim_id AND a.line_number = rl.line_number
           GROUP BY rl.remit_id, rl.claim_id, rl.line_number
           HAVING MAX(rl.charge_cents) <> MAX(rl.paid_cents) + COALESCE(SUM(a.amount_cents), 0)""",
    "paid lines: allowed = paid + PR":
        """SELECT l.claim_id, l.line_number FROM claim_lines l
           LEFT JOIN remit_adjustments a ON a.claim_id = l.claim_id AND a.line_number = l.line_number AND a.group_code = 'PR'
           WHERE l.line_status = 'Paid'
           GROUP BY l.claim_id, l.line_number
           HAVING MAX(l.allowed_cents) <> MAX(l.paid_cents) + COALESCE(SUM(a.amount_cents), 0)""",
    "claim paid = sum of line paid":
        """SELECT c.claim_id FROM claims c JOIN claim_lines l USING (claim_id)
           GROUP BY c.claim_id HAVING SUM(l.paid_cents) <> MAX(c.paid_cents)""",
    "remit total = sum of its claims":
        """SELECT r.remit_id FROM remits r JOIN remit_claims rc USING (remit_id)
           GROUP BY r.remit_id HAVING SUM(rc.paid_cents) <> MAX(r.total_paid_cents)""",
    "every adjudicated claim has exactly one remit":
        """SELECT c.claim_id FROM claims c LEFT JOIN remit_claims rc USING (claim_id)
           WHERE c.adjudication_date IS NOT NULL GROUP BY c.claim_id HAVING COUNT(rc.remit_id) <> 1""",
    "no remit for unadjudicated claims":
        """SELECT rc.claim_id FROM remit_claims rc JOIN claims c USING (claim_id) WHERE c.adjudication_date IS NULL""",
    "dates in order: DOS <= submission <= received <= adjudication":
        """SELECT c.claim_id FROM claims c JOIN encounters e USING (encounter_id)
           WHERE c.submission_date < e.date_of_service
              OR c.payer_received_date < c.submission_date
              OR c.adjudication_date < c.payer_received_date""",
    "every denied line has a ground-truth driver":
        """SELECT l.claim_id, l.line_number FROM claim_lines l
           LEFT JOIN truth_denial_drivers t USING (claim_id, line_number)
           WHERE l.line_status = 'Denied' AND t.driver IS NULL""",
    "timely-filing denials really are late":
        """SELECT c.claim_id FROM claims c JOIN encounters e USING (encounter_id)
           JOIN payers p USING (payer_id) JOIN remit_adjustments a USING (claim_id)
           WHERE a.carc_code = '29'
             AND julianday(c.submission_date) - julianday(e.date_of_service) <= p.timely_filing_days""",
    "eligibility denials fall after coverage end":
        """SELECT c.claim_id FROM claims c JOIN encounters e USING (encounter_id)
           JOIN coverages cv ON cv.coverage_id = c.coverage_id JOIN remit_adjustments a USING (claim_id)
           WHERE a.carc_code = '27' AND (cv.termination_date IS NULL OR e.date_of_service <= cv.termination_date)""",
    "paid lines priced at contract unless a known underpayment":
        """SELECT l.claim_id, l.line_number FROM claim_lines l JOIN claims c USING (claim_id)
           JOIN fee_schedule f ON f.payer_id = c.payer_id AND f.cpt_code = l.cpt_code
           LEFT JOIN truth_underpayments u USING (claim_id, line_number)
           WHERE l.line_status = 'Paid' AND u.claim_id IS NULL
             AND ABS(l.allowed_cents - l.charge_cents * f.allowed_pct) > 1""",
    "missing-info denials have blank dx pointers":
        """SELECT DISTINCT a.claim_id FROM remit_adjustments a JOIN claim_lines l USING (claim_id, line_number)
           WHERE a.carc_code = '16' AND l.dx_pointer <> ''""",
    "Medicare patients are 65+":
        """SELECT pt.patient_id FROM patients pt JOIN coverages cv USING (patient_id) JOIN payers p USING (payer_id)
           WHERE p.line_of_business IN ('Medicare FFS','Medicare Advantage')
             AND ((SELECT CAST(substr(MIN(date_of_service),1,4) AS INTEGER) FROM encounters)
                  - CAST(substr(pt.dob,1,4) AS INTEGER)) < 65""",
}


# These only hold for generated data (one remit per claim, contract pricing, answer keys...).
SYNTHETIC_ONLY = {
    "remit total = sum of its claims", "every adjudicated claim has exactly one remit",
    "every denied line has a ground-truth driver", "timely-filing denials really are late",
    "eligibility denials fall after coverage end", "paid lines priced at contract unless a known underpayment",
    "missing-info denials have blank dx pointers", "Medicare patients are 65+", "paid lines: allowed = paid + PR",
}


def is_synthetic(con) -> bool:
    try:
        return con.execute("SELECT 1 FROM data_sources WHERE source = 'synthetic'").fetchone() is not None
    except sqlite3.Error:
        return True


def validate(db_path: str | Path, synthetic: bool | None = None) -> dict[str, int]:
    """Return {check name: number of failing rows}. All zeros = pass.

    On loaded (real) data only the checks that hold for any data run; on real data a
    non-zero count is a data-quality warning to look at, not a crash."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    synthetic = is_synthetic(con) if synthetic is None else synthetic
    results = {name: len(con.execute(sql).fetchall()) for name, sql in CHECKS.items()
               if synthetic or name not in SYNTHETIC_ONLY}
    results["foreign-key violations"] = len(con.execute("PRAGMA foreign_key_check").fetchall())
    con.close()
    return results


def summary(db_path: str | Path) -> dict:
    con = sqlite3.connect(db_path)
    q = lambda sql: con.execute(sql).fetchall()
    out = {
        "counts": {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in
                   ["patients", "encounters", "claims", "claim_lines", "remits", "remit_adjustments"]},
        "status_mix": dict(q("SELECT current_status, COUNT(*) FROM claims GROUP BY 1 ORDER BY 2 DESC")),
        "denial_rate_by_family": {
            fam: round(rate, 3) for fam, rate in q(
                """SELECT p.payer_family,
                          AVG(CASE WHEN c.current_status IN ('Denied','Partially Paid') THEN 1.0 ELSE 0 END)
                   FROM claims c JOIN payers p USING (payer_id)
                   WHERE c.adjudication_date IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")},
        "denials_by_carc": dict(q(
            """SELECT carc_code, COUNT(*) FROM remit_adjustments
               WHERE carc_code NOT IN ('1','2','3','45') GROUP BY 1 ORDER BY 2 DESC""")),
        "dollars": {k: round(v / 100, 2) for k, v in zip(
            ["billed", "paid", "patient_resp"],
            q("SELECT SUM(total_charge_cents), SUM(paid_cents), SUM(patient_resp_cents) FROM claims")[0])},
        "denied_dollars": round(q("SELECT SUM(charge_cents) FROM claim_lines WHERE line_status='Denied'")[0][0] / 100, 2),
    }
    con.close()
    return out

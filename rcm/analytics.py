"""Module 6 — Revenue cycle KPIs.

Definitions (all over the snapshot in the database):
  Net collection rate   (payer paid + patient responsibility) / contracted allowed amount, adjudicated
                        claims (duplicates excluded); assumes patient balances are collected
  Gross collection rate payer paid / charges
  Initial denial rate   adjudicated claims with >= 1 denied line / adjudicated claims
  Clean claim rate      adjudicated claims paid in full on first pass (no front-end rejection, no denial)
  Days in A/R           open A/R / average daily charges over the last 90 days
  Open A/R              unadjudicated claim charges + open (workable) denied dollars
"""
from __future__ import annotations

from datetime import date, timedelta

from .db import rows


def _one(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0] or 0


def compute(con, as_of: date) -> dict:
    adj = "c.adjudication_date IS NOT NULL"
    charges = _one(con, f"SELECT SUM(total_charge_cents) FROM claims c WHERE {adj}")
    paid = _one(con, f"SELECT SUM(paid_cents) FROM claims c WHERE {adj}")
    pr = _one(con, f"SELECT SUM(patient_resp_cents) FROM claims c WHERE {adj}")
    contractual = _one(con, "SELECT SUM(amount_cents) FROM latest_remit_adjustments WHERE carc_code = '45'")
    # what the contract says these services should have paid in total (payer + patient)
    expected_allowed = _one(con, f"""SELECT SUM(COALESCE(f.allowed_cents * MAX(l.units, 1), l.charge_cents * f.allowed_pct))
                                     FROM claim_lines l JOIN claims c USING (claim_id)
                                     JOIN fee_schedule f ON f.payer_id = c.payer_id AND f.cpt_code = l.cpt_code
                                     WHERE {adj} AND c.frequency_code = '1'
                                       AND l.claim_id NOT IN (SELECT claim_id FROM latest_remit_adjustments WHERE carc_code = '18')""")
    n_adj = _one(con, f"SELECT COUNT(*) FROM claims c WHERE {adj}")
    n_denied = _one(con, f"SELECT COUNT(*) FROM claims c WHERE {adj} AND current_status IN ('Denied','Partially Paid')")
    denied_cents = _one(con, "SELECT SUM(charge_cents) FROM claim_lines WHERE line_status = 'Denied'")
    rejected = {r[0] for r in con.execute("SELECT DISTINCT claim_id FROM claim_events WHERE status = 'Rejected'")}
    clean = sum(1 for (cid,) in con.execute(f"SELECT claim_id FROM claims c WHERE {adj} AND current_status = 'Paid'")
                if cid not in rejected)

    pending = rows(con, """SELECT c.total_charge_cents, e.date_of_service FROM claims c JOIN encounters e USING (encounter_id)
                           WHERE c.adjudication_date IS NULL""")
    try:
        open_denials = rows(con, """SELECT r.denied_cents, e.date_of_service FROM recovery_queue r
                                    JOIN claims c USING (claim_id) JOIN encounters e USING (encounter_id)
                                    WHERE r.status = 'Open'""")
    except Exception:
        open_denials = []
    buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "91-120": 0, "120+": 0}
    for amount, dos in [(p["total_charge_cents"], p["date_of_service"]) for p in pending] + \
                       [(d["denied_cents"], d["date_of_service"]) for d in open_denials]:
        age = (as_of - date.fromisoformat(dos)).days
        key = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "91-120" if age <= 120 else "120+"
        buckets[key] += amount
    open_ar = sum(buckets.values())
    since = (as_of - timedelta(days=90)).isoformat()
    last90 = _one(con, """SELECT SUM(c.total_charge_cents) FROM claims c JOIN encounters e USING (encounter_id)
                          WHERE e.date_of_service > ? AND c.frequency_code = '1'""", (since,))
    days_to_pay = _one(con, """SELECT AVG(julianday(c.adjudication_date) - julianday(e.date_of_service))
                               FROM claims c JOIN encounters e USING (encounter_id) WHERE c.adjudication_date IS NOT NULL""")
    lag = _one(con, """SELECT AVG(julianday(c.submission_date) - julianday(e.date_of_service))
                       FROM claims c JOIN encounters e USING (encounter_id) WHERE c.submission_date IS NOT NULL""")

    monthly = rows(con, """SELECT substr(e.date_of_service, 1, 7) AS month, SUM(c.total_charge_cents) AS charges,
                                  SUM(c.paid_cents) AS paid
                           FROM claims c JOIN encounters e USING (encounter_id) GROUP BY 1 ORDER BY 1""")
    by_family = rows(con, """SELECT p.payer_family AS family, COUNT(*) AS claims,
                                    AVG(CASE WHEN c.current_status IN ('Denied','Partially Paid') THEN 1.0 ELSE 0 END) AS denial_rate,
                                    SUM(c.paid_cents) AS paid
                             FROM claims c JOIN payers p USING (payer_id) WHERE c.adjudication_date IS NOT NULL
                             GROUP BY 1 ORDER BY denial_rate DESC""")
    by_carc = rows(con, """SELECT a.carc_code, COUNT(*) AS lines, SUM(a.amount_cents) AS cents
                           FROM latest_remit_adjustments a WHERE a.carc_code NOT IN ('1','2','3','45')
                           GROUP BY 1 ORDER BY cents DESC""")
    return {
        "as_of": as_of.isoformat(),
        "charges": charges / 100, "payer_paid": paid / 100, "patient_resp": pr / 100,
        "contractual": contractual / 100, "denied": denied_cents / 100,
        # with no contract rates loaded, fall back to charges less contractual write-downs
        "net_collection_rate": ((paid + pr) / expected_allowed if expected_allowed else
                                (paid + pr) / (charges - contractual) if charges > contractual else 0),
        "ncr_basis": "contract" if expected_allowed else "charges less contractual",
        "gross_collection_rate": paid / charges if charges else 0,
        "initial_denial_rate": n_denied / n_adj if n_adj else 0,
        "clean_claim_rate": clean / n_adj if n_adj else 0,
        "claims_adjudicated": n_adj,
        "open_ar": open_ar / 100,
        "days_in_ar": (open_ar / (last90 / 90)) if last90 else 0,
        "ar_aging": {k: v / 100 for k, v in buckets.items()},
        "avg_days_dos_to_payment": round(days_to_pay, 1),
        "avg_submission_lag_days": round(lag, 1),
        "monthly": [{"month": m["month"], "charges": m["charges"] / 100, "paid": m["paid"] / 100} for m in monthly],
        "denial_by_family": [{"family": f["family"], "claims": f["claims"], "denial_rate": f["denial_rate"],
                              "paid": f["paid"] / 100} for f in by_family],
        "denial_by_carc": [{"carc": d["carc_code"], "lines": d["lines"], "dollars": d["cents"] / 100} for d in by_carc],
    }

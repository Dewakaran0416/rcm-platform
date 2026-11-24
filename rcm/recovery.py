"""Module 4 — Denial recovery.

Turns the denial queue from payment posting into a ranked work queue:
root cause from CARC/RARC, the action to take, the team that owns it, the
deadline (payer appeal limit or corrected-claim filing limit), and a priority
score = denied $ x chance of recovery x urgency. Appeal letters are drafted
from templates for the items that need one.

Only operational data is used. Ground truth is used in evaluate() to check
the recoverability calls.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .db import replace_table, rows
from .reference import CARC, ICD10, cpt_info, driver_for

DDL = """
CREATE TABLE recovery_queue (
    claim_id        TEXT NOT NULL REFERENCES claims(claim_id),
    line_number     INTEGER NOT NULL,
    payer_id        TEXT NOT NULL,
    cpt_code        TEXT NOT NULL,
    carc_code       TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    action          TEXT NOT NULL,
    action_type     TEXT NOT NULL,     -- CORRECTED_CLAIM / APPEAL / REBILL / RETRO_AUTH / PATIENT_BILL / CLOSE
    owner_team      TEXT NOT NULL,
    denied_cents    INTEGER NOT NULL,
    p_recover       REAL NOT NULL,
    deadline        TEXT,
    days_left       INTEGER,
    priority        REAL NOT NULL,
    status          TEXT NOT NULL,     -- Open / Expired / Close
    PRIMARY KEY (claim_id, line_number)
);
"""

# denial driver -> (root cause, action, action type, owner team, base chance of recovery, deadline basis)
# The driver comes from the CARC (reference/carc_map.py), so real-world codes land in the right row.
PLAYBOOK = {
    "eligibility":  ("Eligibility", "Verify coverage; bill the correct payer or the patient", "REBILL", "Patient Access", 0.60, "filing"),
    "no_auth": ("Authorization", "Request retro-authorization, then appeal with the auth", "RETRO_AUTH", "Authorization", 0.50, "appeal"),
    "timely_filing": ("Timely filing", "Appeal only with proof of timely filing; otherwise write off", "APPEAL", "AR Follow-up", 0.08, "appeal"),
    "bundling": ("Bundling / modifier", "Send corrected claim with modifier 25/59 if documented", "CORRECTED_CLAIM", "Coding", 0.70, "filing"),
    "medical_necessity": ("Medical necessity", "Appeal with medical records and coverage criteria", "APPEAL", "Clinical Appeals", 0.45, "appeal"),
    "duplicate": ("Duplicate", "Confirm the original claim paid; close", "CLOSE", "AR Follow-up", 0.03, "appeal"),
    "missing_info": ("Missing information", "Send corrected claim (frequency 7) with the missing data", "CORRECTED_CLAIM", "Billing", 0.85, "filing"),
    "non_covered": ("Non-covered", "Check ABN / waiver; bill the patient if allowed", "PATIENT_BILL", "Patient Financial Services", 0.15, "appeal"),
}
PLAYBOOK["coordination"] = ("Coordination of benefits", "Update COB / other insurance; bill the primary payer first",
                            "REBILL", "Patient Access", 0.55, "filing")
DEFAULT = ("Other", "Review remit and payer policy", "APPEAL", "AR Follow-up", 0.30, "appeal")
RECOVERABLE_THRESHOLD = 0.40


def build_queue(con, as_of: date) -> list[dict]:
    denials = rows(con, """
        SELECT d.*, e.date_of_service, p.appeal_limit_days, p.timely_filing_days
        FROM denial_queue d JOIN claims c USING (claim_id) JOIN encounters e USING (encounter_id)
        JOIN payers p ON p.payer_id = d.payer_id""")
    out = []
    for d in denials:
        cause, action, atype, team, p, basis = PLAYBOOK.get(driver_for(d["carc_code"]), DEFAULT)
        if basis == "appeal":
            deadline = date.fromisoformat(d["payment_date"]) + timedelta(days=d["appeal_limit_days"])
        else:  # corrected claim / rebill must land inside the filing limit (floor of 30 days after the denial)
            deadline = max(date.fromisoformat(d["date_of_service"]) + timedelta(days=d["timely_filing_days"]),
                           date.fromisoformat(d["payment_date"]) + timedelta(days=30))
        days_left = (deadline - as_of).days
        if atype == "CLOSE":
            status, priority = "Close", 0.0
        elif days_left < 0:
            status, priority = "Expired", 0.0
        else:
            urgency = 1 + min(2.0, 30 / max(days_left, 1))     # x1 when far away, up to x3 inside ~10 days
            status, priority = "Open", d["denied_cents"] / 100 * p * urgency
        out.append({"claim_id": d["claim_id"], "line_number": d["line_number"], "payer_id": d["payer_id"],
                    "cpt_code": d["cpt_code"], "carc_code": d["carc_code"], "root_cause": cause, "action": action,
                    "action_type": atype, "owner_team": team, "denied_cents": d["denied_cents"], "p_recover": p,
                    "deadline": deadline.isoformat(), "days_left": days_left, "priority": round(priority, 2),
                    "status": status})
    out.sort(key=lambda r: -r["priority"])
    return out


def appeal_letter(con, claim_id: str, line_number: int) -> str:
    q = rows(con, """
        SELECT r.*, c.payer_claim_number, c.dx1, c.prior_auth_number, e.date_of_service, p.payer_name,
               pt.first_name, pt.last_name, pt.dob, cv.member_id, pr.provider_name, pr.provider_npi
        FROM recovery_queue r JOIN claims c USING (claim_id) JOIN encounters e USING (encounter_id)
        JOIN payers p ON p.payer_id = r.payer_id JOIN patients pt ON pt.patient_id = c.patient_id
        JOIN coverages cv ON cv.coverage_id = c.coverage_id JOIN providers pr ON pr.provider_npi = c.billing_npi
        WHERE r.claim_id = ? AND r.line_number = ?""", (claim_id, line_number))[0]
    ground = {
        "50": ("The service was medically necessary. The enclosed records document the patient's condition "
               f"({q['dx1']} – {ICD10.get(q['dx1'], 'see records')}) and the clinical findings that support "
               f"{q['cpt_code']} ({cpt_info(q['cpt_code'])[0].lower()}). [Clinician: add the specific findings and the "
               "coverage criteria met.]"),
        "197": ("Authorization was obtained retroactively / was in process at the time of service. "
                "[Add the authorization number and the reason it was not on the original claim.]"),
        "29": ("The claim was originally submitted within the filing limit. Enclosed is proof of timely filing "
               "(clearinghouse acceptance report). [Attach the 999/277CA acceptance.]"),
    }.get({"medical_necessity": "50", "no_auth": "197", "timely_filing": "29"}.get(driver_for(q["carc_code"]), ""),
          "[State the grounds for reconsideration.]")
    return f"""{q['provider_name'].replace(' (synthetic)', '')}
NPI {q['provider_npi']}

{q['payer_name']} — Provider Appeals

RE: Request for reconsideration
Patient: {q['first_name']} {q['last_name']}   DOB: {q['dob']}   Member ID: {q['member_id']}
Claim: {q['claim_id']}   Payer claim #: {q['payer_claim_number']}   DOS: {q['date_of_service']}
Service: {q['cpt_code']}   Billed: ${q['denied_cents'] / 100:,.2f}
Denial: CARC {q['carc_code']} – {CARC.get(q['carc_code'], '')}
Appeal deadline: {q['deadline']}

We request reconsideration of the denial above.

{ground}

Please reprocess the service for payment. Enclosed: claim copy, remittance, supporting documentation.

Sincerely,
Billing Office

[DRAFT generated from synthetic data — review before sending]
"""


def run(con, as_of: date, letters_dir: str | Path | None = None, n_letters: int = 25) -> dict:
    queue = build_queue(con, as_of)
    replace_table(con, DDL, "recovery_queue", queue)
    written = 0
    if letters_dir:
        folder = Path(letters_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for r in queue:
            if written >= n_letters:
                break
            if r["status"] == "Open" and r["action_type"] in ("APPEAL", "RETRO_AUTH"):
                (folder / f"appeal_{r['claim_id']}_L{r['line_number']}.txt").write_text(
                    appeal_letter(con, r["claim_id"], r["line_number"]), encoding="utf-8")
                written += 1
    open_items = [r for r in queue if r["status"] == "Open"]
    return {
        "queue_items": len(queue), "open": len(open_items),
        "expired": sum(r["status"] == "Expired" for r in queue),
        "closed_no_revenue": sum(r["status"] == "Close" for r in queue),
        "open_denied_dollars": round(sum(r["denied_cents"] for r in open_items) / 100, 2),
        "expected_recovery_dollars": round(sum(r["denied_cents"] * r["p_recover"] for r in open_items) / 100, 2),
        "due_in_14_days": sum(1 for r in open_items if r["days_left"] <= 14),
        "appeal_letters_drafted": written,
        "by_team": dict(con.execute("""SELECT owner_team, COUNT(*) FROM recovery_queue WHERE status='Open'
                                       GROUP BY 1 ORDER BY 2 DESC""").fetchall()),
    }


def evaluate(con) -> dict:
    r = rows(con, """SELECT q.p_recover, q.priority, q.denied_cents, q.status, t.is_recoverable
                     FROM recovery_queue q JOIN truth_denial_drivers t USING (claim_id, line_number)""")
    correct = sum((x["p_recover"] >= RECOVERABLE_THRESHOLD) == bool(x["is_recoverable"]) for x in r)
    open_items = sorted([x for x in r if x["status"] == "Open"], key=lambda x: -x["priority"])
    top = open_items[: max(1, len(open_items) // 5)]
    rec_total = sum(x["denied_cents"] for x in open_items if x["is_recoverable"])
    rec_top = sum(x["denied_cents"] for x in top if x["is_recoverable"])
    return {"recoverability_accuracy": round(correct / len(r), 3) if r else None,
            "top20pct_share_of_recoverable_dollars": round(rec_top / rec_total, 3) if rec_total else None}

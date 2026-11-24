"""Apply a client's 835 files to the claims already loaded.

This turns loaded claims into adjudicated A/R: remits, line-level CAS, paid /
allowed / status per line, claim status and an 835 event. Payments are applied
in date order; every payment is kept (remit_claims / remit_lines / remit_adjustments),
and when a claim appears again (denied, then paid on appeal) its latest payment
sets the claim's current status. Reversals (CLP02 = 22) are counted, not applied.
Payment posting (Module 2) later re-reads the same files and reconciles.
"""
from __future__ import annotations

from ..db import rows
from ..posting import effective_lines
from ..reference import is_denial

PATIENT_SHARE = {"1", "2", "3"}


def apply_835(con, payments: list[dict]) -> dict:
    con.execute("PRAGMA foreign_keys = ON")
    claims = {c["claim_id"]: c for c in rows(con, "SELECT claim_id, payer_id, billing_npi, submission_date FROM claims")}
    providers = {r[0] for r in con.execute("SELECT provider_npi FROM providers")}
    line_info, by_claim = {}, {}
    for r in rows(con, "SELECT claim_id, line_number, units, cpt_code, charge_cents FROM claim_lines ORDER BY claim_id, line_number"):
        line_info[(r["claim_id"], r["line_number"])] = r
        by_claim.setdefault(r["claim_id"], []).append(r)
    remit_ids = {r[0] for r in con.execute("SELECT remit_id FROM remits")}
    stats = {"payments": 0, "claims_applied": 0, "unmatched_claims": 0, "reversals": 0, "lines_applied": 0}
    unmatched = []
    touched = set()

    for p in sorted(payments, key=lambda x: (x.get("payment_date") or "", x.get("trace_number") or "")):
        matched = [c for c in p["claims"] if c["claim_id"] in claims]
        for c in p["claims"]:
            if c["claim_id"] not in claims:
                unmatched.append(c["claim_id"])
        if not matched:
            continue
        first = claims[matched[0]["claim_id"]]
        rid = p.get("trace_number") or f"ERA-{len(remit_ids) + 1}"
        base, n = rid, 1
        while rid in remit_ids:
            n += 1
            rid = f"{base}-{n}"
        remit_ids.add(rid)
        payee = p.get("payee_npi") if p.get("payee_npi") in providers else first["billing_npi"]
        con.execute("INSERT INTO remits VALUES (?,?,?,?,?,?)",
                    (rid, first["payer_id"], payee, p.get("payment_method") or ("ACH" if p["total_paid_cents"] else "NON"),
                     p["payment_date"], p["total_paid_cents"]))
        stats["payments"] += 1
        done_in_remit = set()
        for c in matched:
            cid = c["claim_id"]
            if c.get("status_code") == "22":
                stats["reversals"] += 1
                continue
            if cid in done_in_remit:
                continue
            done_in_remit.add(cid)
            con.execute("INSERT INTO remit_claims VALUES (?,?,?,?,?,?,?)",
                        (rid, cid, c.get("status_code") or "1", c["charge_cents"], c["paid_cents"],
                         c["patient_resp_cents"], c.get("payer_claim_number") or ""))
            for ln in effective_lines(c, line_info, by_claim.get(cid, [])):
                n_ = ln["line_number"]
                rarc = (ln.get("rarc_codes") or [None])[0]
                for a in ln["adjustments"]:
                    con.execute("""INSERT INTO remit_adjustments (remit_id, claim_id, line_number, group_code, carc_code,
                                   rarc_code, amount_cents) VALUES (?,?,?,?,?,?,?)""",
                                (rid, cid, n_, a["group_code"], a["carc_code"], rarc, a["amount_cents"]))
                con.execute("INSERT OR REPLACE INTO remit_lines VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (rid, cid, n_, ln["cpt_code"] or line_info[(cid, n_)]["cpt_code"],
                             (ln.get("modifiers") or [None])[0] or None,
                             (ln.get("modifiers") or [None, None])[1] if len(ln.get("modifiers") or []) > 1 else None,
                             ln.get("units") or line_info[(cid, n_)]["units"], ln["charge_cents"], ln["paid_cents"],
                             ln["allowed_cents"]))
                pr = sum(a["amount_cents"] for a in ln["adjustments"] if a["group_code"] == "PR" and a["carc_code"] in PATIENT_SHARE)
                denied = ln["paid_cents"] == 0 and any(is_denial(a["group_code"], a["carc_code"]) for a in ln["adjustments"])
                allowed = 0 if denied else (ln["allowed_cents"] if ln["allowed_cents"] is not None else ln["paid_cents"] + pr)
                con.execute("UPDATE claim_lines SET allowed_cents = ?, paid_cents = ?, line_status = ? WHERE claim_id = ? AND line_number = ?",
                            (allowed, ln["paid_cents"], "Denied" if denied else "Paid", cid, n_))
                stats["lines_applied"] += 1
            statuses = [r[0] for r in con.execute("SELECT line_status FROM claim_lines WHERE claim_id = ?", (cid,))]
            done = [s for s in statuses if s != "Pending"] or ["Denied"]
            status = "Paid" if all(s == "Paid" for s in done) else "Denied" if all(s == "Denied" for s in done) else "Partially Paid"
            paid = con.execute("SELECT SUM(paid_cents) FROM claim_lines WHERE claim_id = ?", (cid,)).fetchone()[0] or 0
            pr_total = c["patient_resp_cents"]
            con.execute("""UPDATE claims SET current_status = ?, paid_cents = ?, patient_resp_cents = ?, adjudication_date = ?,
                           payer_received_date = COALESCE(payer_received_date, submission_date),
                           payer_claim_number = COALESCE(NULLIF(?, ''), payer_claim_number) WHERE claim_id = ?""",
                        (status, paid, pr_total, p["payment_date"], c.get("payer_claim_number") or "", cid))
            con.execute("INSERT INTO claim_events (claim_id, event_date, status, source, status_category, note) VALUES (?,?,?,?,?,?)",
                        (cid, p["payment_date"], status, "Payer-835", "F2" if status == "Denied" else "F1", f"ERA {rid}"))
            touched.add(cid)
    # remits that only carried reversals or duplicates: drop empty ones
    con.execute("DELETE FROM remits WHERE remit_id NOT IN (SELECT remit_id FROM remit_claims)")
    stats["claims_applied"] = len(touched)
    stats["unmatched_claims"] = len(set(unmatched))
    con.execute("INSERT OR REPLACE INTO data_sources VALUES ('835', ?)",
                (f"{stats['payments']} payments, {stats['claims_applied']} claims",))
    con.commit()
    stats["unmatched_examples"] = sorted(set(unmatched))[:10]
    return stats

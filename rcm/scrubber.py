"""Module 1 — Claim scrubber.

Runs pre-submission edits on every claim, using only what a billing team
knows before the claim goes out: the claim, the coverage on file (as a
270/271 would return it), the authorizations on file, and the claims already
sent. It never looks at the payer's answer or the ground-truth tables; those
are used only afterwards, in evaluate(), to measure how many denials the
edits would have prevented.
"""
from __future__ import annotations

from datetime import date

from .db import has_source, replace_table, rows
from .reference import BUNDLING_PAIRS, cpt_info, driver_for, is_denial

DDL = """
CREATE TABLE scrub_edits (
    claim_id     TEXT NOT NULL REFERENCES claims(claim_id),
    line_number  INTEGER,              -- NULL = claim-level edit
    edit_code    TEXT NOT NULL,
    severity     TEXT NOT NULL,        -- HARD (do not send) / WARN (review)
    message      TEXT NOT NULL,
    fix          TEXT NOT NULL
);
"""

# edit_code -> (severity, the denial driver it prevents, fixable before submission?)
EDITS = {
    "ELIG":   ("HARD", "eligibility", True),
    "AUTH":   ("HARD", "no_auth", True),
    "BUNDLE": ("HARD", "bundling", True),
    "DXNEC":  ("WARN", "medical_necessity", True),
    "TFL":    ("HARD", "timely_filing", False),
    "DUP":    ("HARD", "duplicate", False),   # stops rework; the original claim still pays, so no revenue at stake
    "INFO":   ("HARD", "missing_info", True),
}


def scrub(con) -> list[dict]:
    claims = rows(con, """
        SELECT c.*, e.date_of_service, e.patient_id AS enc_patient, p.timely_filing_days, p.payer_name,
               cv.termination_date
        FROM claims c JOIN encounters e USING (encounter_id) JOIN payers p USING (payer_id)
        JOIN coverages cv ON cv.coverage_id = c.coverage_id
        ORDER BY c.submission_date IS NULL, c.submission_date, c.claim_id""")
    lines_by_claim = {}
    for ln in rows(con, "SELECT * FROM claim_lines ORDER BY claim_id, line_number"):
        lines_by_claim.setdefault(ln["claim_id"], []).append(ln)
    auths = {}
    for a in rows(con, "SELECT * FROM authorizations WHERE status = 'Approved'"):
        auths.setdefault((a["patient_id"], a["payer_id"], a["cpt_code"]), []).append(a)

    check_auth = has_source(con, "authorizations")   # no auth file loaded -> can't know, so don't flag
    edits: list[dict] = []
    sent: dict[tuple, str] = {}        # (patient, DOS, CPT set) -> first claim sent

    def add(claim_id, line, code, message, fix):
        edits.append({"claim_id": claim_id, "line_number": line, "edit_code": code,
                      "severity": EDITS[code][0], "message": message, "fix": fix})

    for c in claims:
        cid, dos = c["claim_id"], c["date_of_service"]
        lines = lines_by_claim.get(cid, [])
        codes = {ln["cpt_code"]: ln for ln in lines}
        dx = [c["dx1"], c["dx2"], c["dx3"], c["dx4"]]

        # ELIG — coverage on file ended before the date of service
        if c["termination_date"] and dos > c["termination_date"]:
            add(cid, None, "ELIG", f"Coverage with {c['payer_name']} ended {c['termination_date']}, before DOS {dos}",
                "Re-verify eligibility (270/271); find current coverage or bill patient")

        # TFL — planned send date is past the payer's filing limit
        if c["submission_date"]:
            lag = (date.fromisoformat(c["submission_date"]) - date.fromisoformat(dos)).days
            if lag > c["timely_filing_days"]:
                add(cid, None, "TFL", f"{lag} days from DOS; {c['payer_name']} limit is {c['timely_filing_days']}",
                    "Send only with proof of timely filing; otherwise route to write-off review")
            elif lag > c["timely_filing_days"] - 15:
                pass  # close to limit: the claim-status module prioritises these

        # DUP — same patient, DOS and services already sent
        key = (c["patient_id"], dos, tuple(sorted(codes)))
        if key in sent:
            add(cid, None, "DUP", f"Same patient, DOS and services as {sent[key]}",
                "Do not send; follow up on the original claim instead")
        elif c["submission_date"]:
            sent[key] = cid

        for ln in lines:
            cpt, n = ln["cpt_code"], ln["line_number"]
            # INFO — line not linked to a diagnosis
            if not ln["dx_pointer"]:
                add(cid, n, "INFO", f"Line {n} ({cpt}) has no diagnosis pointer", "Link the line to the supporting diagnosis")
            # AUTH — auth-required service with no valid approved auth on file
            if check_auth and cpt_info(cpt)[2]:
                valid = [a for a in auths.get((c["patient_id"], c["payer_id"], cpt), [])
                         if a["valid_from"] <= dos <= a["valid_to"]]
                if not valid:
                    add(cid, n, "AUTH", f"{cpt} needs prior authorization; none on file for DOS {dos}",
                        "Request retro-authorization before sending, or hold")
                elif not c["prior_auth_number"]:
                    add(cid, n, "AUTH", f"Auth {valid[0]['auth_number']} on file but not on claim",
                        "Add the auth number (REF*G1)")
            # DXNEC — the pointed (primary) diagnosis doesn't support the procedure
            allowed = cpt_info(cpt)[4]
            if allowed and ln["dx_pointer"]:
                primary = dx[int(ln["dx_pointer"].split(",")[0]) - 1]
                if primary and not any(primary.startswith(p) for p in allowed):
                    add(cid, n, "DXNEC", f"{cpt} with primary dx {primary} may fail medical necessity",
                        f"Review documentation; supporting dx families: {', '.join(allowed)}")

        # BUNDLE — pair billed without the modifier that separates it
        for prim, sec, mod, denied in BUNDLING_PAIRS:
            if prim in codes and sec in codes:
                ln = codes[denied]
                if mod not in (ln["modifier1"], ln["modifier2"]):
                    add(cid, ln["line_number"], "BUNDLE",
                        f"{denied} billed with {prim} without modifier {mod}",
                        f"Add modifier {mod} if documentation supports a separate service")
    return edits


def run(con) -> list[dict]:
    edits = scrub(con)
    replace_table(con, DDL, "scrub_edits", edits)
    return edits


def denied_lines(con) -> list[dict]:
    """Denied lines with their main denial CARC (largest denial-type adjustment) and driver."""
    out = []
    best = {}
    for a in rows(con, """SELECT a.claim_id, a.line_number, a.group_code, a.carc_code, a.amount_cents, l.charge_cents
                          FROM latest_remit_adjustments a JOIN claim_lines l USING (claim_id, line_number)
                          WHERE l.line_status = 'Denied'"""):
        if not is_denial(a["group_code"], a["carc_code"]):
            continue
        key = (a["claim_id"], a["line_number"])
        if key not in best or a["amount_cents"] > best[key]["amount_cents"]:
            best[key] = a
    for (cid, ln), a in best.items():
        out.append({"claim_id": cid, "line_number": ln, "carc_code": a["carc_code"],
                    "driver": driver_for(a["carc_code"]), "charge_cents": a["charge_cents"]})
    return out


def evaluate(con) -> dict:
    """Score the edits against what the payer actually did (adjudicated claims only).

    The denial reason comes from the payer's own CARC on the 835, so this works the
    same on real data (no answer key needed)."""
    denied = denied_lines(con)
    flagged = {}
    for e in rows(con, "SELECT * FROM scrub_edits"):
        flagged.setdefault((e["claim_id"], e["line_number"]), set()).add(e["edit_code"])

    def edits_for(claim_id, line):
        return flagged.get((claim_id, line), set()) | flagged.get((claim_id, None), set())

    by_driver = {}
    prevented_cents = 0
    for d in denied:
        codes = edits_for(d["claim_id"], d["line_number"])
        caught = any(EDITS[c][1] == d["driver"] for c in codes)
        if d["driver"] == "other":
            caught = False
        s = by_driver.setdefault(d["driver"], {"denied_lines": 0, "caught": 0})
        s["denied_lines"] += 1
        s["caught"] += caught
        if caught and any(EDITS[c][1] == d["driver"] and EDITS[c][2] for c in codes):
            prevented_cents += d["charge_cents"]
    for s in by_driver.values():
        s["recall"] = round(s["caught"] / s["denied_lines"], 3)

    adjudicated_lines = rows(con, """
        SELECT l.claim_id, l.line_number, l.line_status FROM claim_lines l JOIN claims c USING (claim_id)
        WHERE c.adjudication_date IS NOT NULL""")
    flagged_lines = [x for x in adjudicated_lines if edits_for(x["claim_id"], x["line_number"])]
    hits = sum(1 for x in flagged_lines if x["line_status"] == "Denied")
    total_denied = len(denied)
    total_caught = sum(s["caught"] for s in by_driver.values())
    return {
        "denied_lines": total_denied,
        "caught_before_submission": total_caught,
        "recall": round(total_caught / total_denied, 3) if total_denied else 0,
        "precision": round(hits / len(flagged_lines), 3) if flagged_lines else 0,
        "preventable_dollars": round(prevented_cents / 100, 2),
        "by_driver": dict(sorted(by_driver.items())),
        "edits_by_code": dict(con.execute("SELECT edit_code, COUNT(*) FROM scrub_edits GROUP BY 1 ORDER BY 2 DESC").fetchall()),
    }

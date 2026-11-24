"""Module 2 — Payment posting.

Works from parsed 835 files (not the remit tables) so it would work the same
on a client's real ERAs. For each payment it:
  1. matches every CLP to an open claim (unmatched -> exception queue)
  2. checks the 835 balances (charge = paid + adjustments, per line)
  3. posts payment, contractual, patient responsibility and denial amounts
  4. compares allowed to the contract (fee_schedule) to find underpayments
  5. sends every denied line to the denial queue for Module 4
"""
from __future__ import annotations

from .db import rows
from .reference import is_denial

DDL = """
CREATE TABLE postings (
    trace_number        TEXT NOT NULL,
    payment_date        TEXT NOT NULL,
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    line_number         INTEGER NOT NULL,
    charge_cents        INTEGER NOT NULL,
    paid_cents          INTEGER NOT NULL,
    contractual_cents   INTEGER NOT NULL,   -- CO-45
    patient_resp_cents  INTEGER NOT NULL,   -- PR-1/2/3
    denied_cents        INTEGER NOT NULL,   -- CO/OA/PI other than 45, and PR-96
    allowed_cents       INTEGER
);
CREATE TABLE posting_exceptions (
    trace_number  TEXT NOT NULL,
    claim_id      TEXT,
    issue         TEXT NOT NULL
);
CREATE TABLE underpayments (
    claim_id               TEXT NOT NULL REFERENCES claims(claim_id),
    line_number            INTEGER NOT NULL,
    payer_id               TEXT NOT NULL,
    cpt_code               TEXT NOT NULL,
    expected_allowed_cents INTEGER NOT NULL,
    actual_allowed_cents   INTEGER NOT NULL,
    variance_cents         INTEGER NOT NULL,
    payment_date           TEXT NOT NULL
);
CREATE TABLE denial_queue (
    claim_id      TEXT NOT NULL REFERENCES claims(claim_id),
    line_number   INTEGER NOT NULL,
    payer_id      TEXT NOT NULL,
    cpt_code      TEXT NOT NULL,
    group_code    TEXT NOT NULL,
    carc_code     TEXT NOT NULL,
    rarc_code     TEXT,
    denied_cents  INTEGER NOT NULL,
    payment_date  TEXT NOT NULL,
    PRIMARY KEY (claim_id, line_number)
);
"""

PATIENT_RESP_CARCS = {"1", "2", "3"}
VARIANCE_MIN_CENTS = 100       # flag an underpayment only if it's at least $1 ...
VARIANCE_MIN_PCT = 0.02        # ... and at least 2% of the expected allowed


def expected_allowed(fee: dict, payer_id: str, cpt: str, charge_cents: int, units: int) -> int | None:
    """Contracted allowed amount: a fixed rate per unit if the contract has one, else a share of billed charge."""
    f = fee.get((payer_id, cpt))
    if not f:
        return None
    if f["allowed_cents"] is not None:
        return int(f["allowed_cents"]) * max(int(units or 1), 1)
    if f["allowed_pct"] is not None:
        return int(round(charge_cents * f["allowed_pct"]))
    return None


def post(con, payments: list[dict]) -> dict:
    """Post 835 payments. A claim can appear in several 835s (denied, then paid on appeal):
    every payment is posted, and the line's latest result decides whether it is still denied."""
    claims = {c["claim_id"]: c for c in rows(con, "SELECT claim_id, payer_id FROM claims")}
    line_units, by_claim = {}, {}
    for r in rows(con, "SELECT claim_id, line_number, units, cpt_code, charge_cents FROM claim_lines ORDER BY claim_id, line_number"):
        line_units[(r["claim_id"], r["line_number"])] = r
        by_claim.setdefault(r["claim_id"], []).append(r)
    fee = {(f["payer_id"], f["cpt_code"]): f for f in rows(con, "SELECT * FROM fee_schedule")}
    seen = set()
    postings, exceptions = [], []
    latest = {}          # (claim, line) -> (payment_date, denial record or None, underpayment record or None)

    for p in sorted(payments, key=lambda x: (x.get("payment_date") or "", x.get("trace_number") or "")):
        trace = p.get("trace_number", "?")
        if sum(c["paid_cents"] for c in p["claims"]) - p.get("plb_cents", 0) != p["total_paid_cents"]:
            exceptions.append({"trace_number": trace, "claim_id": None, "issue": "BPR total != sum of CLP paid"})
        for c in p["claims"]:
            cid = c["claim_id"]
            if cid not in claims:
                exceptions.append({"trace_number": trace, "claim_id": cid, "issue": "Claim not found in AR"})
                continue
            if (trace, cid, c.get("status_code")) in seen:
                exceptions.append({"trace_number": trace, "claim_id": cid, "issue": "Same claim twice in one payment"})
                continue
            seen.add((trace, cid, c.get("status_code")))
            if c.get("status_code") == "22":            # reversal of an earlier payment: posted, not re-adjudicated
                exceptions.append({"trace_number": trace, "claim_id": cid, "issue": "Reversal (CLP02=22) - review"})
                continue
            payer_id = claims[cid]["payer_id"]
            for ln in effective_lines(c, line_units, by_claim.get(cid, [])):
                adj = ln["adjustments"]
                total_adj = sum(a["amount_cents"] for a in adj)
                if ln["charge_cents"] != ln["paid_cents"] + total_adj:
                    exceptions.append({"trace_number": trace, "claim_id": cid,
                                       "issue": f"Line {ln['line_number']} out of balance"})
                contractual = sum(a["amount_cents"] for a in adj if a["carc_code"] == "45" and a["group_code"] in ("CO", "OA"))
                pr = sum(a["amount_cents"] for a in adj if a["group_code"] == "PR" and a["carc_code"] in PATIENT_RESP_CARCS)
                denied_adj = [a for a in adj if is_denial(a["group_code"], a["carc_code"])]
                denied = sum(a["amount_cents"] for a in denied_adj)
                postings.append({"trace_number": trace, "payment_date": p["payment_date"], "claim_id": cid,
                                 "line_number": ln["line_number"], "charge_cents": ln["charge_cents"],
                                 "paid_cents": ln["paid_cents"], "contractual_cents": contractual,
                                 "patient_resp_cents": pr, "denied_cents": denied, "allowed_cents": ln["allowed_cents"]})
                den = und = None
                if denied_adj and ln["paid_cents"] == 0:
                    main = max(denied_adj, key=lambda a: a["amount_cents"])
                    den = {"claim_id": cid, "line_number": ln["line_number"], "payer_id": payer_id,
                           "cpt_code": ln["cpt_code"], "group_code": main["group_code"], "carc_code": main["carc_code"],
                           "rarc_code": ln["rarc_codes"][0] if ln["rarc_codes"] else None,
                           "denied_cents": denied, "payment_date": p["payment_date"]}
                else:
                    allowed = ln["allowed_cents"] if ln["allowed_cents"] is not None else ln["paid_cents"] + pr
                    units = line_units.get((cid, ln["line_number"]), {}).get("units", ln.get("units", 1))
                    expected = expected_allowed(fee, payer_id, ln["cpt_code"], ln["charge_cents"], units)
                    if expected is not None:
                        variance = expected - allowed
                        if variance >= VARIANCE_MIN_CENTS and variance >= expected * VARIANCE_MIN_PCT:
                            und = {"claim_id": cid, "line_number": ln["line_number"], "payer_id": payer_id,
                                   "cpt_code": ln["cpt_code"], "expected_allowed_cents": expected,
                                   "actual_allowed_cents": allowed, "variance_cents": variance,
                                   "payment_date": p["payment_date"]}
                latest[(cid, ln["line_number"])] = (den, und)

    denials = [d for d, _ in latest.values() if d]
    under = [u for _, u in latest.values() if u]
    con.executescript("DROP TABLE IF EXISTS postings; DROP TABLE IF EXISTS posting_exceptions;"
                      "DROP TABLE IF EXISTS underpayments; DROP TABLE IF EXISTS denial_queue;")
    con.executescript(DDL)
    for table, recs in [("postings", postings), ("posting_exceptions", exceptions),
                        ("underpayments", under), ("denial_queue", denials)]:
        if recs:
            cols = list(recs[0])
            con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in recs])
    con.commit()
    return {"payments": len(payments), "claims_posted": len({x["claim_id"] for x in postings}),
            "lines_posted": len(postings), "exceptions": len(exceptions), "underpayments": len(under),
            "denied_lines": len(denials),
            "posted_paid_dollars": round(sum(x["paid_cents"] for x in postings) / 100, 2),
            "underpaid_dollars": round(sum(x["variance_cents"] for x in under) / 100, 2)}


def effective_lines(clp: dict, line_info: dict, claim_lines: list[dict]) -> list[dict]:
    """Service lines of one CLP, each with a line number.

    Lines are matched to the claim by REF*6R when present, else by CPT and charge.
    When the payer sent only claim-level adjustments (no SVC loops, common on full
    denials), each claim line gets the claim-level result in proportion to its charge."""
    cid = clp["claim_id"]
    if clp["lines"]:
        used, out = set(), []
        for ln in clp["lines"]:
            n = ln.get("line_number")
            if n is None or (cid, n) not in line_info or n in used:
                n = next((cl["line_number"] for cl in claim_lines if cl["line_number"] not in used
                          and cl["cpt_code"] == ln["cpt_code"] and cl["charge_cents"] == ln["charge_cents"]), None) or \
                    next((cl["line_number"] for cl in claim_lines if cl["line_number"] not in used
                          and cl["cpt_code"] == ln["cpt_code"]), None)
            if n is None:
                continue
            used.add(n)
            # claim-level adjustments (if any) ride on the first line so totals still balance
            extra = clp.get("adjustments", []) if not out else []
            out.append({**ln, "line_number": n, "adjustments": ln["adjustments"] + extra})
        return out
    total = sum(cl["charge_cents"] for cl in claim_lines) or 1
    out, paid_left = [], clp["paid_cents"]
    adj = clp.get("adjustments", [])
    for i, cl in enumerate(claim_lines):
        last = i == len(claim_lines) - 1
        share = cl["charge_cents"] / total
        paid = paid_left if last else int(round(clp["paid_cents"] * share))
        paid_left -= paid
        rest = cl["charge_cents"] - paid
        line_adj = []
        if adj and rest:
            weights = [a["amount_cents"] for a in adj]
            wsum = sum(weights) or 1
            left = rest
            for j, a in enumerate(adj):
                amt = left if j == len(adj) - 1 else int(round(rest * weights[j] / wsum))
                left -= amt
                line_adj.append({**a, "amount_cents": amt})
        elif rest:
            line_adj.append({"group_code": "CO", "carc_code": "45" if paid else "16", "amount_cents": rest})
        out.append({"line_number": cl["line_number"], "cpt_code": cl["cpt_code"], "modifiers": [],
                    "charge_cents": cl["charge_cents"], "paid_cents": paid, "units": cl["units"],
                    "allowed_cents": None, "adjustments": line_adj, "rarc_codes": clp.get("rarc_codes", [])})
    return out


def evaluate(con) -> dict:
    """Round trip vs the source remits, and underpayment detection vs ground truth."""
    db_paid = con.execute("SELECT COALESCE(SUM(paid_cents),0) FROM remit_claims").fetchone()[0]
    posted = con.execute("SELECT COALESCE(SUM(paid_cents),0) FROM postings").fetchone()[0]
    db_adj = con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM remit_adjustments").fetchone()[0]
    posted_adj = con.execute("SELECT COALESCE(SUM(contractual_cents+patient_resp_cents+denied_cents),0) FROM postings").fetchone()[0]
    try:
        truth = {(r[0], r[1]) for r in con.execute("SELECT claim_id, line_number FROM truth_underpayments")}
    except Exception:
        truth = set()
    found = {(r[0], r[1]) for r in con.execute("SELECT claim_id, line_number FROM underpayments")}
    db_denied = {(r[0], r[1]) for r in con.execute("SELECT claim_id, line_number FROM claim_lines WHERE line_status='Denied'")}
    queued = {(r[0], r[1]) for r in con.execute("SELECT claim_id, line_number FROM denial_queue")}
    tp = len(truth & found)
    return {
        "paid_matches_source": posted == db_paid,
        "adjustments_match_source": posted_adj == db_adj,
        "denials_queued_match_source": queued == db_denied,
        "underpayment_recall": round(tp / len(truth), 3) if truth else None,
        "underpayment_precision": round(tp / len(found), 3) if found else None,
    }

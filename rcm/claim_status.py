"""Module 5 — Claim status follow-up and channel routing.

1. Finds every open claim that needs follow-up (no 835, past its follow-up
   date) and every unbilled claim at risk of missing its filing limit.
2. Sends a 276 per payer and reads the 277 back (a payer simulator answers
   from the claim's event history, since there's no live clearinghouse here).
3. When EDI can't settle it ("thin" 277: still pending well past normal
   turnaround, or no record at the payer), routes the follow-up to a channel
   the payer actually permits, per the Payer AI-Call Policy Registry.

AI voice is never the default, and is only used where policy allows it.
The voice agent itself (telephony) is out of scope; this module produces
the routed worklist and a call script with the required AI disclosure.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .db import replace_table, rows
from .x12 import x276

DDL = """
CREATE TABLE status_worklist (
    claim_id         TEXT PRIMARY KEY REFERENCES claims(claim_id),
    payer_id         TEXT NOT NULL,
    reason           TEXT NOT NULL,     -- NO_RESPONSE / STALE_PENDING / AWAITING_277 / UNBILLED_NEAR_TFL / UNBILLED_PAST_TFL
    days_open        INTEGER NOT NULL,
    charge_cents     INTEGER NOT NULL,
    last_277_category TEXT,
    channel          TEXT NOT NULL,     -- EDI_276 / PORTAL / IVR_BOT / AI_VOICE / HUMAN_CALL / BILLING_TEAM
    channel_reason   TEXT NOT NULL,
    priority         REAL NOT NULL,
    call_script      TEXT
);
"""

FOLLOW_UP_AFTER_DAYS = 30      # no 835 by then -> send a 276
STALE_PENDING_DAYS = 45        # still P1 after this -> escalate beyond EDI
TFL_WARNING_DAYS = 30          # unbilled within this many days of the filing limit

# ai_call_status -> (escalation channel, why)
ROUTING = {
    "Prohibited":      ("PORTAL", "Payer does not accept AI or automated callers; use portal or a staff call"),
    "Restricted":      ("IVR_BOT", "Payer IVR self-service only; payer will not release information to an AI caller via a rep"),
    "IVR-required":    ("IVR_BOT", "Payer requires its IVR for claim status (authenticate NPI/PTAN/TIN)"),
    "Conditional":     ("HUMAN_CALL", "AI calls allowed only after the AI vendor and BAA are registered with the payer"),
    "Silent":          ("IVR_BOT", "Policy unpublished: IVR self-service only until payer confirms AI callers are allowed"),
    "Not yet checked": ("IVR_BOT", "Policy not yet researched: IVR self-service only"),
    "Allowed":         ("AI_VOICE", "Payer allows AI voice agents"),
}


def _route(policy: str, registered_vendor_payers: set[str], payer_id: str) -> tuple[str, str]:
    if policy == "Conditional" and payer_id in registered_vendor_payers:
        return "AI_VOICE", "AI vendor + BAA registered with this payer"
    return ROUTING.get(policy, ("HUMAN_CALL", "Unknown policy: staff call"))


def call_script(c: dict, channel: str) -> str | None:
    if channel not in ("AI_VOICE", "IVR_BOT"):
        return None
    if channel == "IVR_BOT":
        return (f"IVR self-service only. Enter NPI {c['billing_npi']}, member ID {c['member_id']}, "
                f"DOS {c['date_of_service']}, billed ${c['total_charge_cents'] / 100:,.2f}. "
                "Capture claim status, ICN, and paid/denied detail. Do not request a live representative.")
    return ("Opening (required): \"Hello, this is an automated AI assistant calling on behalf of "
            f"{c['provider_name'].replace(' (synthetic)', '')}, NPI {c['billing_npi']}, about a claim status. "
            "This call may be recorded. Would you like me to transfer to a staff member instead?\"\n"
            f"Ask for: status of claim for member {c['member_id']}, DOS {c['date_of_service']}, "
            f"billed ${c['total_charge_cents'] / 100:,.2f}; ICN; expected pay date or denial reason.\n"
            "Stop and hand off to staff if asked, if verification fails, or if the rep declines to speak with an AI.")


def run(con, as_of: date, out_dir: str | Path | None = None,
        registered_vendor_payers: set[str] | None = None, simulate_payer: bool | None = None,
        responses_277: dict[str, dict] | None = None) -> dict:
    """simulate_payer: answer 276s from the claim history (synthetic data only; defaults to on for synthetic).
    responses_277: real 277 results by claim_id (from parse_277), used instead of the simulator."""
    from .db import has_source
    registered_vendor_payers = registered_vendor_payers or set()
    if simulate_payer is None:
        simulate_payer = has_source(con, "synthetic") and responses_277 is None
    responses_277 = responses_277 or {}
    payers = {p["payer_id"]: p for p in rows(con, "SELECT * FROM payers")}
    open_claims = rows(con, """
        SELECT c.*, e.date_of_service, cv.member_id, pt.first_name, pt.last_name, pt.dob, pr.provider_name
        FROM claims c JOIN encounters e USING (encounter_id) JOIN coverages cv ON cv.coverage_id = c.coverage_id
        JOIN patients pt ON pt.patient_id = c.patient_id JOIN providers pr ON pr.provider_npi = c.billing_npi
        WHERE c.adjudication_date IS NULL""")
    events = {}
    for e in rows(con, "SELECT * FROM claim_events WHERE source = 'Payer-277' ORDER BY event_date, event_id"):
        events[e["claim_id"]] = e     # latest payer status

    work, to_query = [], {}
    for c in open_claims:
        if c["submission_date"] is None:                         # unbilled
            p = payers[c["payer_id"]]
            age = (as_of - date.fromisoformat(c["date_of_service"])).days
            left = p["timely_filing_days"] - age
            if left <= TFL_WARNING_DAYS:
                reason = "UNBILLED_PAST_TFL" if left < 0 else "UNBILLED_NEAR_TFL"
                work.append({"claim_id": c["claim_id"], "payer_id": c["payer_id"], "reason": reason, "days_open": age,
                             "charge_cents": c["total_charge_cents"], "last_277_category": None,
                             "channel": "BILLING_TEAM",
                             "channel_reason": f"{left} days to filing limit" if left >= 0 else f"{-left} days past filing limit",
                             "priority": round(c["total_charge_cents"] / 100 * (3 if left >= 0 else 0.2), 2),
                             "call_script": None})
            continue
        days = (as_of - date.fromisoformat(c["submission_date"])).days
        if days >= FOLLOW_UP_AFTER_DAYS:
            to_query.setdefault(c["payer_id"], []).append({**c, "days": days})

    # 276 out, 277 back (simulated payer), parse, route
    folder = Path(out_dir) if out_dir else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    n_276 = 0
    for ctl, (payer_id, items) in enumerate(sorted(to_query.items()), start=1):
        payer = payers[payer_id]
        req = x276.build_276([{"claim_id": i["claim_id"], "payer_claim_number": i["payer_claim_number"],
                               "member_id": i["member_id"], "last_name": i["last_name"], "first_name": i["first_name"],
                               "dob": i["dob"], "billing_npi": i["billing_npi"],
                               "total_charge_cents": i["total_charge_cents"], "dos": i["date_of_service"]} for i in items],
                             payer, control=ctl)
        responses = []
        for i in items:
            ev = events.get(i["claim_id"])
            if ev is None:
                responses.append({"claim_id": i["claim_id"], "category": "A4", "status_code": "35",   # not found
                                  "status_date": as_of.isoformat(), "total_charge_cents": i["total_charge_cents"]})
            else:
                responses.append({"claim_id": i["claim_id"], "category": ev["status_category"] or "P1",
                                  "status_code": "20" if (ev["status_category"] or "P1") == "P1" else "1",
                                  "status_date": ev["event_date"], "total_charge_cents": i["total_charge_cents"]})
        if folder:
            (folder / f"276_{payer_id}.x12").write_text(req, encoding="ascii", errors="replace")
        if simulate_payer:
            resp = x276.build_277(responses, payer, control=ctl)
            if folder:
                (folder / f"277_{payer_id}.x12").write_text(resp, encoding="ascii", errors="replace")
            parsed = {r["claim_id"]: r for r in x276.parse_277(resp)}
        else:
            parsed = responses_277
        n_276 += len(items)
        for i in items:
            r = parsed.get(i["claim_id"])
            if r is None:          # real data, no 277 back yet: the 276 is the action
                work.append({"claim_id": i["claim_id"], "payer_id": payer_id, "reason": "AWAITING_277",
                             "days_open": i["days"], "charge_cents": i["total_charge_cents"], "last_277_category": None,
                             "channel": "EDI_276", "channel_reason": "276 written; load the payer's 277 response to route follow-up",
                             "priority": round(i["total_charge_cents"] / 100 * (1 + i["days"] / 30), 2), "call_script": None})
                continue
            thin = r["category"] == "A4" or (r["category"] in ("P1", "A2", "A1") and i["days"] >= STALE_PENDING_DAYS)
            if thin:
                channel, why = _route(payer["ai_call_status"], registered_vendor_payers, payer_id)
                reason = "NO_RESPONSE" if r["category"] == "A4" else "STALE_PENDING"
            else:
                channel, why, reason = "EDI_276", "277 shows normal processing; recheck in 7 days", "STALE_PENDING"
            # older and bigger first; closer to the appeal/filing cliff matters most
            priority = i["total_charge_cents"] / 100 * (1 + i["days"] / 30)
            work.append({"claim_id": i["claim_id"], "payer_id": payer_id, "reason": reason, "days_open": i["days"],
                         "charge_cents": i["total_charge_cents"], "last_277_category": r["category"],
                         "channel": channel, "channel_reason": why, "priority": round(priority, 2),
                         "call_script": call_script(i, channel)})

    work.sort(key=lambda w: -w["priority"])
    replace_table(con, DDL, "status_worklist", work)
    return {
        "worklist": len(work), "claims_sent_276": n_276,
        "by_channel": dict(con.execute("SELECT channel, COUNT(*) FROM status_worklist GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "by_reason": dict(con.execute("SELECT reason, COUNT(*) FROM status_worklist GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "ai_voice_calls_to_prohibited_payers": con.execute(
            """SELECT COUNT(*) FROM status_worklist w JOIN payers p USING (payer_id)
               WHERE w.channel = 'AI_VOICE' AND p.ai_call_status IN ('Prohibited','Restricted','IVR-required')""").fetchone()[0],
    }

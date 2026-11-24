"""835 (005010X221A1) writer and parser.

The writer turns the remits in the database into real 835 files (one
interchange per payer, one transaction set per payment). The parser reads
any 835 back into plain dicts; payment posting works from the parsed files,
not from the database, so the round trip is genuinely exercised.
"""
from __future__ import annotations

from pathlib import Path

from ..db import rows
from . import COMP, component_sep, envelope, money, read_segments, seg

VERSION = "005010X221A1"


def _cents(x: str) -> int:
    return int(round(float(x or 0) * 100))


# ------------------------------------------------------------------ writer
def build_835(con, payer_id: str, control: int = 1) -> str:
    payer = rows(con, "SELECT * FROM payers WHERE payer_id = ?", (payer_id,))[0]
    remits = rows(con, "SELECT * FROM remits WHERE payer_id = ? ORDER BY payment_date, remit_id", (payer_id,))
    if not remits:
        return ""
    providers = {p["provider_npi"]: p for p in rows(con, "SELECT * FROM providers")}
    rc_by_remit, lines_by_claim, adj_by_line = {}, {}, {}
    for rc in rows(con, """SELECT rc.*, e.date_of_service FROM remit_claims rc JOIN claims c USING (claim_id)
                           JOIN encounters e USING (encounter_id) WHERE c.payer_id = ? ORDER BY rc.claim_id""", (payer_id,)):
        rc_by_remit.setdefault(rc["remit_id"], []).append(rc)
    for ln in rows(con, """SELECT rl.* FROM remit_lines rl JOIN remits r USING (remit_id)
                           WHERE r.payer_id = ? ORDER BY rl.line_number""", (payer_id,)):
        lines_by_claim.setdefault((ln["remit_id"], ln["claim_id"]), []).append(ln)
    for a in rows(con, """SELECT a.* FROM remit_adjustments a JOIN remits r USING (remit_id)
                          WHERE r.payer_id = ? ORDER BY a.adjustment_id""", (payer_id,)):
        adj_by_line.setdefault((a["remit_id"], a["claim_id"], a["line_number"]), []).append(a)

    bodies = []
    for r in remits:
        payee = providers[r["payee_npi"]]
        pay_date = r["payment_date"].replace("-", "")
        handling = "I" if r["total_paid_cents"] > 0 else "H"
        body = [seg("BPR", handling, money(r["total_paid_cents"]), "C", r["payment_method"], *([""] * 11), pay_date),
                seg("TRN", "1", r["remit_id"], "1" + payer_id.ljust(9, "0")[:9]),
                seg("DTM", "405", pay_date),
                seg("N1", "PR", payer["payer_name"]),
                seg("N1", "PE", payee["provider_name"].replace(" (synthetic)", ""), "XX", payee["provider_npi"]),
                seg("LX", "1")]
        for rc in rc_by_remit.get(r["remit_id"], []):
            body.append(seg("CLP", rc["claim_id"], rc["claim_status_code"], money(rc["charge_cents"]),
                            money(rc["paid_cents"]), money(rc["patient_resp_cents"]), "12", rc["payer_claim_number"]))
            for ln in lines_by_claim.get((r["remit_id"], rc["claim_id"]), []):
                proc = COMP.join(x for x in ["HC", ln["cpt_code"], ln["modifier1"] or "", ln["modifier2"] or ""] if x)
                body.append(seg("SVC", proc, money(ln["charge_cents"]), money(ln["paid_cents"]), "", ln["units"]))
                body.append(seg("DTM", "472", rc["date_of_service"].replace("-", "")))
                adjs = adj_by_line.get((r["remit_id"], rc["claim_id"], ln["line_number"]), [])
                by_group = {}
                for a in adjs:
                    by_group.setdefault(a["group_code"], []).append(a)
                for group, items in by_group.items():
                    for i in range(0, len(items), 6):        # a CAS holds up to 6 reason/amount pairs
                        trip = []
                        for a in items[i:i + 6]:
                            trip += [a["carc_code"], money(a["amount_cents"]), ""]
                        body.append(seg("CAS", group, *trip))
                body.append(seg("REF", "6R", str(ln["line_number"])))      # line item control number
                if ln["allowed_cents"] is not None:
                    body.append(seg("AMT", "B6", money(ln["allowed_cents"])))
                for rarc in sorted({a["rarc_code"] for a in adjs if a["rarc_code"]}):
                    body.append(seg("LQ", "HE", rarc))
        # provider-level adjustment (PLB) for anything in the payment that isn't claim payments
        # (reversals, offsets, interest): BPR = sum(CLP paid) - PLB
        diff = sum(rc["paid_cents"] for rc in rc_by_remit.get(r["remit_id"], [])) - r["total_paid_cents"]
        if diff:
            body.append(seg("PLB", payee["provider_npi"], pay_date[:4] + "1231", "WO" + COMP + r["remit_id"], money(diff)))
        bodies.append(body)
    return envelope(bodies, sender=payer_id, receiver="RCMPLATFORM", functional_id="HP", version=VERSION, control=control)


def write_all(con, folder: str | Path) -> dict[str, int]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for (pid,) in con.execute("SELECT DISTINCT payer_id FROM remits ORDER BY payer_id").fetchall():
        text = build_835(con, pid, control=len(out) + 1)
        (folder / f"835_{pid}.x12").write_text(text, encoding="ascii", errors="replace")
        out[pid] = con.execute("SELECT COUNT(*) FROM remits WHERE payer_id = ?", (pid,)).fetchone()[0]
    return out


# ------------------------------------------------------------------ parser
def parse_835(text: str) -> list[dict]:
    """Return one dict per payment (ST..SE): header, claims, service lines, adjustments."""
    comp = component_sep(text)
    payments, pay, clm, svc = [], None, None, None
    for s in read_segments(text):
        tag = s[0]
        get = lambda i: s[i] if len(s) > i else ""
        if tag == "ST":
            pay = {"claims": [], "payer_name": None, "payee_npi": None, "payer_id": None, "plb_cents": 0}
            clm = svc = None
        elif tag == "BPR":
            pay.update(handling=get(1), total_paid_cents=_cents(get(2)), payment_method=get(4),
                       payment_date=f"{get(16)[:4]}-{get(16)[4:6]}-{get(16)[6:8]}")
        elif tag == "TRN":
            pay["trace_number"] = get(2)
        elif tag == "N1" and get(1) == "PR":
            pay["payer_name"] = get(2)
            if get(3) in ("XV", "PI"):
                pay["payer_id"] = get(4)
        elif tag == "REF" and get(1) == "2U" and pay is not None and svc is None and clm is None:
            pay["payer_id"] = get(2)
        elif tag == "N1" and get(1) == "PE":
            pay["payee_npi"] = get(4)
        elif tag == "CLP":
            clm = {"claim_id": get(1), "status_code": get(2), "charge_cents": _cents(get(3)),
                   "paid_cents": _cents(get(4)), "patient_resp_cents": _cents(get(5)),
                   "payer_claim_number": get(7), "lines": [], "adjustments": [], "rarc_codes": []}
            pay["claims"].append(clm)
            svc = None
        elif tag == "SVC":
            proc = get(1).split(comp)
            svc = {"cpt_code": proc[1] if len(proc) > 1 else "", "modifiers": proc[2:],
                   "charge_cents": _cents(get(2)), "paid_cents": _cents(get(3)),
                   "units": int(get(5) or 1), "line_number": None, "allowed_cents": None,
                   "adjustments": [], "rarc_codes": []}
            clm["lines"].append(svc)
        elif tag == "CAS" and clm is not None:
            group = get(1)
            target = svc["adjustments"] if svc is not None else clm["adjustments"]   # before any SVC = claim level
            for i in range(2, len(s), 3):
                if s[i]:
                    target.append({"group_code": group, "carc_code": s[i],
                                   "amount_cents": _cents(s[i + 1] if i + 1 < len(s) else 0)})
        elif tag == "MOA" and clm is not None:
            clm["rarc_codes"] += [x for x in s[3:] if x and not x.replace(".", "").isdigit()]
        elif tag == "PLB" and pay is not None:
            pay["plb_cents"] += sum(_cents(s[i]) for i in range(4, len(s), 2) if s[i])
        elif tag == "REF" and get(1) == "6R" and svc is not None:
            svc["line_number"] = int(get(2))
        elif tag == "AMT" and get(1) == "B6" and svc is not None:
            svc["allowed_cents"] = _cents(get(2))
        elif tag == "LQ" and get(1) == "HE" and svc is not None:
            svc["rarc_codes"].append(get(2))
        elif tag == "SE":
            payments.append(pay)
            pay = None
    return payments


def parse_folder(folder: str | Path) -> list[dict]:
    out = []
    for f in sorted(Path(folder).glob("835_*.x12")):
        out.extend(parse_835(f.read_text(encoding="ascii")))
    return out


def parse_folder_any(folder: str | Path) -> list[dict]:
    """Every 835 in a folder, whatever the file names/extensions (detected by content)."""
    out = []
    for f in sorted(Path(folder).rglob("*")):
        if not f.is_file() or f.stat().st_size > 200_000_000:
            continue
        head = f.read_bytes()[:800].decode("ascii", errors="replace")
        if head.lstrip().startswith("ISA") and ("005010X221" in head or "*835*" in head or "|835|" in head):
            out.extend(parse_835(f.read_text(encoding="ascii", errors="replace")))
    return out

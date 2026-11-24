"""837P (005010X222A1) writer — one interchange per payer, one transaction set per billing provider."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ..db import rows
from . import COMP, envelope, money, seg

VERSION = "005010X222A1"
# The data model holds state and ZIP but no street address or city, so 837s carry placeholders there.
# Fine for synthetic data and for review; for real submission, send from the PM system (see README).
ADDRESS_PLACEHOLDER = "ADDRESS NOT IN DATA"
SUBMITTER_ID = "RCMPLATFORM"

# claim filing indicator (SBR09) by line of business
FILING = {"Commercial": "CI", "Medicare FFS": "MB", "Medicare Advantage": "16",
          "Medicaid FFS": "MC", "Medicaid MCO": "HM", "Military": "CH"}


def _dx(code: str | None) -> str:
    return (code or "").replace(".", "")


def claim_segments(c: dict, lines: list[dict], patient: dict, coverage: dict, payer: dict, rendering: dict,
                   hl_parent: int, hl_next: int) -> tuple[list[str], int]:
    s = [seg("HL", hl_next, hl_parent, "22", "0"),
         seg("SBR", "P", "18", coverage["group_number"] or "", "", "", "", "", "", FILING[payer["line_of_business"]]),
         seg("NM1", "IL", "1", patient["last_name"], patient["first_name"], "", "", "", "MI", coverage["member_id"]),
         seg("N3", ADDRESS_PLACEHOLDER),
         seg("N4", "ANYTOWN", patient["state"], patient["zip"]),
         seg("DMG", "D8", patient["dob"].replace("-", ""), patient["sex"]),
         seg("NM1", "PR", "2", payer["payer_name"], "", "", "", "", "PI", payer["payer_id"]),
         seg("CLM", c["claim_id"], money(c["total_charge_cents"]), "", "",
             COMP.join([c["place_of_service"], "B", c["frequency_code"]]), "Y", "A", "Y", "Y")]
    if c["prior_auth_number"]:
        s.append(seg("REF", "G1", c["prior_auth_number"]))
    dx = [d for d in (c["dx1"], c["dx2"], c["dx3"], c["dx4"]) if d]
    s.append(seg("HI", *[("ABK" if i == 0 else "ABF") + COMP + _dx(d) for i, d in enumerate(dx)]))
    s += [seg("NM1", "82", "1", rendering["last"], rendering["first"], "", "", "", "XX", rendering["provider_npi"]),
          seg("PRV", "PE", "PXC", rendering["taxonomy_code"])]
    for ln in lines:
        proc = COMP.join(x for x in ["HC", ln["cpt_code"], ln["modifier1"] or "", ln["modifier2"] or ""])
        proc = proc.rstrip(COMP)
        s += [seg("LX", ln["line_number"]),
              seg("SV1", proc, money(ln["charge_cents"]), "UN", ln["units"], "", "", ln["dx_pointer"].replace(",", COMP)),
              seg("DTP", "472", "D8", c["date_of_service"].replace("-", "")),
              seg("REF", "6R", str(ln["line_number"]))]
    return s, hl_next + 1


def build_837(con, payer_id: str, claim_ids: list[str] | None = None, control: int = 1,
              unbilled_send_date: str | None = None) -> str:
    payer = rows(con, "SELECT * FROM payers WHERE payer_id = ?", (payer_id,))[0]
    where = "c.payer_id = ?" + ("" if unbilled_send_date else " AND c.submission_date IS NOT NULL")
    params: list = [payer_id]
    if claim_ids is not None:
        where += f" AND c.claim_id IN ({','.join('?' * len(claim_ids))})"
        params += claim_ids
    claims = rows(con, f"""SELECT c.*, e.date_of_service, e.place_of_service, e.rendering_npi
                           FROM claims c JOIN encounters e USING (encounter_id) WHERE {where}
                           ORDER BY c.billing_npi, c.claim_id""", params)
    if not claims:
        return ""
    for c in claims:
        if c["submission_date"] is None:
            c["submission_date"] = unbilled_send_date
    ids = [c["claim_id"] for c in claims]
    lines = {}
    for ln in rows(con, f"SELECT * FROM claim_lines WHERE claim_id IN ({','.join('?' * len(ids))}) ORDER BY line_number", ids):
        lines.setdefault(ln["claim_id"], []).append(ln)
    patients = {p["patient_id"]: p for p in rows(con, "SELECT * FROM patients")}
    coverages = {c["coverage_id"]: c for c in rows(con, "SELECT * FROM coverages")}
    providers = {p["provider_npi"]: p for p in rows(con, "SELECT * FROM providers")}
    for p in providers.values():
        name = p["provider_name"].replace("Dr. ", "").replace(" (synthetic)", "").split(" ")
        p["first"], p["last"] = name[0], " ".join(name[1:])

    bodies = []
    # one transaction set per billing provider per send date; BHT04 carries the batch date
    by_billing = {}
    for c in claims:
        by_billing.setdefault((c["billing_npi"], c["submission_date"]), []).append(c)
    for (billing_npi, sent), group in sorted(by_billing.items()):
        bp = providers[billing_npi]
        body = [seg("BHT", "0019", "00", f"{payer_id}{billing_npi[-4:]}{sent.replace('-', '')}", sent.replace("-", ""), "0900", "CH"),
                seg("NM1", "41", "2", "RCM PLATFORM", "", "", "", "", "46", SUBMITTER_ID),
                seg("PER", "IC", "EDI DESK", "TE", "5555550100"),
                seg("NM1", "40", "2", payer["payer_name"], "", "", "", "", "46", payer_id),
                seg("HL", "1", "", "20", "1"),
                seg("NM1", "85", "2", bp["provider_name"].replace(" (synthetic)", ""), "", "", "", "", "XX", billing_npi),
                seg("N3", ADDRESS_PLACEHOLDER),
                seg("N4", "ANYTOWN", bp["state"], "10001"),
                seg("REF", "EI", bp["tax_id"].replace("-", ""))]
        hl = 2
        for c in group:
            segs, hl = claim_segments(c, lines[c["claim_id"]], patients[c["patient_id"]],
                                      coverages[c["coverage_id"]], payer, providers[c["rendering_npi"]], 1, hl)
            body += segs
        bodies.append(body)
    return envelope(bodies, sender=SUBMITTER_ID, receiver=payer_id, functional_id="HC", version=VERSION, control=control)


def write_all(con, folder: str | Path, hard_edits_block: bool = True, only_unbilled: bool = False) -> dict[str, int]:
    """Write one 837P per payer. Claims with HARD scrub edits are held back when hard_edits_block is set.
    only_unbilled: write just the claims not yet sent (real data), dated today."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    held = set()
    if hard_edits_block:
        try:
            held = {r[0] for r in con.execute("SELECT DISTINCT claim_id FROM scrub_edits WHERE severity = 'HARD'")}
        except Exception:
            held = set()
    written = {}
    for (payer_id,) in con.execute("SELECT payer_id FROM payers ORDER BY payer_id").fetchall():
        cond = "submission_date IS NULL" if only_unbilled else "submission_date IS NOT NULL"
        ids = [r[0] for r in con.execute(
            f"SELECT claim_id FROM claims WHERE payer_id = ? AND {cond}", (payer_id,))
            if r[0] not in held]
        if not ids:
            continue
        text = build_837(con, payer_id, ids, control=len(written) + 1,
                         unbilled_send_date=date.today().isoformat() if only_unbilled else None)
        name = f"837P_{payer_id}_REVIEW.x12" if only_unbilled else f"837P_{payer_id}.x12"
        (folder / name).write_text(text, encoding="ascii", errors="replace")
        written[payer_id] = len(ids)
    return written


# ------------------------------------------------------------------ reader
def _date(d: str | None) -> str | None:
    d = (d or "").split("-")[0]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None


def _dollars_to_cents(x: str) -> int:
    return int(round(float(x or 0) * 100))


def parse_837(text: str) -> dict[str, list[dict]]:
    """Read an 837P (any sender's separators) into the import-template shape used by the loader.

    Handles subscriber-is-patient and separate patient (HL 23) loops, claim- or line-level
    dates, REF*G1 prior auth, and 2310B rendering providers. Patient IDs are built from
    member ID + DOB + name, since an 837 has no patient account number of its own."""
    import hashlib
    from . import component_sep, read_segments

    comp = component_sep(text)
    segs = read_segments(text)
    out = {"patients": [], "claims": [], "claim_lines": [], "payers": [], "providers": []}
    seen_pat, seen_payer = set(), set()
    row = 0
    sub = pat = billing = None
    payer = {}
    claim = None
    level = None          # which NM1 context we're in
    submitted = None
    hl_code = None

    def flush_patient(person):
        key = "|".join([person.get("member_id") or "", person.get("dob") or "",
                        (person.get("last_name") or "").upper(), (person.get("first_name") or "").upper()])
        pid = "X837-" + hashlib.sha1(key.encode()).hexdigest()[:12].upper()
        if pid not in seen_pat:
            seen_pat.add(pid)
            out["patients"].append({"_row": None, "_bad": person.get("dob") is None, "patient_id": pid,
                                    "first_name": person.get("first_name"), "last_name": person.get("last_name"),
                                    "dob": person.get("dob"), "sex": person.get("sex"), "state": person.get("state"),
                                    "zip": person.get("zip"), "mrn": None})
        return pid

    for s in segs:
        tag = s[0]
        g = lambda i: s[i] if len(s) > i else ""
        if tag == "BHT":
            submitted = _date(g(4))
        elif tag == "HL":
            hl_code = g(3)
            if hl_code == "20":
                billing = {"npi": None, "name": None, "state": None, "tax_id": None, "taxonomy": None}
                level = "85"
            elif hl_code == "22":
                sub, pat, payer, claim = {}, None, {}, None
            elif hl_code == "23":
                pat = {}
        elif tag == "NM1":
            level = g(1)
            if level == "85":
                billing.update(npi=g(9), name=g(3))
            elif level == "IL":
                sub.update(last_name=g(3), first_name=g(4), member_id=g(9))
            elif level == "QC":
                pat.update(last_name=g(3), first_name=g(4))
            elif level == "PR":
                payer = {"payer_id": g(9) or g(3), "payer_name": g(3)}
                if payer["payer_id"] not in seen_payer:
                    seen_payer.add(payer["payer_id"])
                    out["payers"].append({"_row": None, "_bad": False, **payer, "line_of_business": None,
                                          "state": None, "timely_filing_days": None, "appeal_limit_days": None,
                                          "ai_call_status": None})
            elif level == "82" and claim is not None:
                claim["rendering_npi"] = g(9)
                claim["_rendering_name"] = " ".join(x for x in (g(4), g(3)) if x)
        elif tag == "N4":
            target = billing if level == "85" else pat if (level == "QC" and pat is not None) else sub if level == "IL" else None
            if target is not None:
                target.update(state=g(2), zip=g(3)[:5])
        elif tag == "REF" and level == "85" and g(1) == "EI" and claim is None:
            billing["tax_id"] = g(2)
        elif tag == "PRV":
            if g(1) == "BI" and claim is None:
                billing["taxonomy"] = g(3)
            elif g(1) == "PE" and claim is not None:
                claim["_rendering_taxonomy"] = g(3)
        elif tag == "SBR":
            sub.update(rank={"P": 1, "S": 2, "T": 3}.get(g(1), 1), group=g(3) or None)
        elif tag == "DMG":
            person = pat if (pat is not None and level == "QC") else sub
            person.update(dob=_date(g(2)), sex=g(3) or "U")
        elif tag == "CLM":
            person = dict(sub)
            if pat:
                person.update({k: v for k, v in pat.items() if v})
            pid = flush_patient(person)
            pos = g(5).split(comp)
            row += 1
            claim = {"_row": row, "_bad": False, "claim_id": g(1), "patient_id": pid,
                     "payer_id": payer.get("payer_id"), "payer_name": payer.get("payer_name"),
                     "member_id": sub.get("member_id"), "billing_npi": billing["npi"], "rendering_npi": None,
                     "date_of_service": None, "place_of_service": pos[0] if pos else None,
                     "frequency_code": pos[2] if len(pos) > 2 else "1", "dx1": None, "dx2": None, "dx3": None,
                     "dx4": None, "prior_auth_number": None, "submission_date": submitted, "payer_claim_number": None,
                     "_group": sub.get("group"), "_rank": sub.get("rank"), "_billing_name": billing["name"],
                     "_billing_state": billing["state"], "_tax_id": billing["tax_id"],
                     "_billing_taxonomy": billing["taxonomy"], "_line": None}
            out["claims"].append(claim)
        elif claim is None:
            continue
        elif tag == "REF" and g(1) == "G1":
            claim["prior_auth_number"] = g(2)
        elif tag == "REF" and g(1) == "F8":
            claim["payer_claim_number"] = g(2)
        elif tag == "HI":
            codes = [x.split(comp)[1] for x in s[1:] if comp in x and x.split(comp)[0] in ("ABK", "ABF", "BK", "BF")]
            for i, code in enumerate(codes[:4]):
                claim[f"dx{i + 1}"] = code
        elif tag == "DTP" and g(1) == "472":
            d = _date(g(3))
            if claim["_line"] is not None:
                claim["_line"]["_dos"] = d
            elif d:
                claim["date_of_service"] = d
        elif tag == "LX":
            claim["_line"] = {"_row": row, "_bad": False, "_occ": len(out["claims"]) - 1,
                              "claim_id": claim["claim_id"], "line_number": int(g(1) or 0),
                              "cpt_code": None, "modifier1": None, "modifier2": None, "units": 1, "charge": 0,
                              "dx_pointer": "1", "_dos": None}
            out["claim_lines"].append(claim["_line"])
        elif tag == "SV1" and claim["_line"] is not None:
            proc = g(1).split(comp)
            ln = claim["_line"]
            ln.update(cpt_code=proc[1] if len(proc) > 1 else None, modifier1=(proc[2] if len(proc) > 2 else None) or None,
                      modifier2=(proc[3] if len(proc) > 3 else None) or None, charge=_dollars_to_cents(g(2)),
                      units=int(float(g(4) or 1)), dx_pointer=",".join(x for x in g(7).split(comp) if x))
        elif tag == "REF" and g(1) == "6R" and claim["_line"] is not None:
            pass
    out["claims"], out["claim_lines"] = _latest_version(out["claims"], out["claim_lines"])
    dates = {}
    for ln in out["claim_lines"]:
        if ln["_dos"]:
            dates.setdefault(ln["claim_id"], []).append(ln["_dos"])
    for c in out["claims"]:
        line_dates = dates.get(c["claim_id"])
        if c["date_of_service"] is None and line_dates:
            c["date_of_service"] = min(line_dates)
        c["_bad"] = c["date_of_service"] is None or not c["billing_npi"] or not c["dx1"] or not c["payer_id"]
    return out


def _latest_version(claims: list[dict], lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """A claim number sent more than once (corrected/replacement claims) keeps only its last version.
    Each line carries _occ, the index of the CLM occurrence it was read under."""
    last = {}
    for i, c in enumerate(claims):
        last[c["claim_id"]] = i
    keep_claims = [c for i, c in enumerate(claims) if last[c["claim_id"]] == i]
    keep_lines = [ln for ln in lines if last.get(ln["claim_id"]) == ln["_occ"]]
    return keep_claims, keep_lines


def parse_837_folder(folder) -> dict[str, list[dict]]:
    from pathlib import Path
    merged = {"patients": [], "claims": [], "claim_lines": [], "payers": [], "providers": []}
    seen = {"patients": set(), "payers": set()}
    latest, lines_of = {}, {}
    for f in sorted(Path(folder).iterdir()):
        if f.suffix.lower() not in (".x12", ".837", ".txt", ".edi", ".dat"):
            continue
        text = f.read_text(encoding="ascii", errors="replace")
        if not text.lstrip().startswith("ISA") or "*837*" not in text[:400] and "005010X222" not in text[:600]:
            continue
        part = parse_837(text)
        for t, key in (("patients", "patient_id"), ("payers", "payer_id")):
            for r in part[t]:
                if r[key] not in seen[t]:
                    seen[t].add(r[key])
                    merged[t].append(r)
        grouped = {}
        for ln in part["claim_lines"]:
            grouped.setdefault(ln["claim_id"], []).append(ln)
        for c in part["claims"]:                     # a later file replaces an earlier version of the claim
            latest[c["claim_id"]] = c
            lines_of[c["claim_id"]] = grouped.get(c["claim_id"], [])
    merged["claims"] = list(latest.values())
    merged["claim_lines"] = [ln for c in merged["claims"] for ln in lines_of[c["claim_id"]]]
    return merged

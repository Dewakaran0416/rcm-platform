"""276 claim status request writer, 277 response writer (payer simulator) and 277 parser."""
from __future__ import annotations

from datetime import datetime

from . import component_sep, envelope, money, read_segments, seg

V276 = "005010X212"


def build_276(items: list[dict], payer: dict, control: int = 1) -> str:
    """items: claim_id, payer_claim_number, member_id, last/first name, dob, billing_npi, total_charge_cents, dos."""
    now = datetime.now()
    body = [seg("BHT", "0010", "13", f"STAT{control:06d}", now.strftime("%Y%m%d"), now.strftime("%H%M")),
            seg("HL", "1", "", "20", "1"), seg("NM1", "PR", "2", payer["payer_name"], "", "", "", "", "PI", payer["payer_id"]),
            seg("HL", "2", "1", "21", "1"), seg("NM1", "41", "2", "RCM PLATFORM", "", "", "", "", "46", "RCMPLATFORM")]
    hl = 3
    by_npi = {}
    for it in items:
        by_npi.setdefault(it["billing_npi"], []).append(it)
    for npi, group in by_npi.items():
        prov_hl = hl
        body += [seg("HL", prov_hl, "2", "19", "1"), seg("NM1", "1P", "2", "BILLING PROVIDER", "", "", "", "", "XX", npi)]
        hl += 1
        for it in group:
            body += [seg("HL", hl, prov_hl, "22", "0"),
                     seg("DMG", "D8", it["dob"].replace("-", "")),
                     seg("NM1", "IL", "1", it["last_name"], it["first_name"], "", "", "", "MI", it["member_id"]),
                     seg("TRN", "1", it["claim_id"]),
                     seg("REF", "1K", it["payer_claim_number"] or ""),
                     seg("AMT", "T3", money(it["total_charge_cents"])),
                     seg("DTP", "472", "RD8", f"{it['dos'].replace('-', '')}-{it['dos'].replace('-', '')}")]
            hl += 1
    return envelope([body], sender="RCMPLATFORM", receiver=payer["payer_id"], functional_id="HR", version=V276, control=control)


def build_277(responses: list[dict], payer: dict, control: int = 1) -> str:
    """Payer-side simulator. responses: claim_id, category, status_code, status_date, total_charge_cents."""
    now = datetime.now()
    body = [seg("BHT", "0010", "08", f"RESP{control:06d}", now.strftime("%Y%m%d"), now.strftime("%H%M"), "DG"),
            seg("HL", "1", "", "20", "1"), seg("NM1", "PR", "2", payer["payer_name"], "", "", "", "", "PI", payer["payer_id"]),
            seg("HL", "2", "1", "21", "1"), seg("NM1", "41", "2", "RCM PLATFORM", "", "", "", "", "46", "RCMPLATFORM")]
    hl = 3
    for r in responses:
        body += [seg("HL", hl, "2", "22", "0"),
                 seg("TRN", "2", r["claim_id"]),
                 seg("STC", f"{r['category']}:{r['status_code']}", r["status_date"].replace("-", ""), "", money(r["total_charge_cents"]))]
        hl += 1
    return envelope([body], sender=payer["payer_id"], receiver="RCMPLATFORM", functional_id="HN", version=V276, control=control)


def parse_277(text: str) -> list[dict]:
    comp = component_sep(text)
    out, current = [], None
    for s in read_segments(text):
        if s[0] == "TRN" and len(s) > 2 and s[1] == "2":
            current = {"claim_id": s[2]}
            out.append(current)
        elif s[0] == "STC" and current is not None:
            cat, *code = s[1].split(comp)
            d = s[2] if len(s) > 2 else ""
            current.update(category=cat, status_code=code[0] if code else "",
                           status_date=f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else None)
    return out

"""Fill in payer details a client file usually lacks, from the built-in payer list and the payer name."""
from __future__ import annotations

import re

from ..reference import PAYERS

REGISTRY = {p[0]: dict(zip(["payer_id", "payer_name", "payer_family", "line_of_business", "state",
                            "timely_filing_days", "appeal_limit_days", "ai_call_status", "status_channel"], p))
            for p in PAYERS}
MCO_WORDS = ("FIDELIS", "MOLINA", "CENTENE", "AMERIGROUP", "HEALTHY BLUE", "COMMUNITY PLAN", "CARESOURCE",
             "WELLCARE", "SUNSHINE", "SUPERIOR", "MERIDIAN", "AMBETTER", "HEALTH FIRST", "METROPLUS")
DEFAULT_TFL = {"Medicare FFS": 365, "Medicare Advantage": 365, "Military": 365, "Medicaid FFS": 90,
               "Medicaid MCO": 90, "Commercial": 90}


def _norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper()).strip()


def classify(name: str) -> tuple[str, str]:
    """(payer_family, line_of_business) from a payer name."""
    n = _norm(name)
    has = lambda *w: any(x in n for x in w)
    if has("TRICARE", "TRIWEST", "CHAMPVA", "HUMANA MILITARY"):
        return "TRICARE", "Military"
    family = ("BCBS" if has("BLUE", "BCBS", "ANTHEM", "HIGHMARK", "PREMERA", "REGENCE", "CAREFIRST", "WELLMARK") else
              "Aetna" if has("AETNA") else "Humana" if has("HUMANA") else
              "UHC" if has("UNITED", "UHC", "OPTUM", "UMR") else "UPMC" if has("UPMC") else
              "Cigna" if has("CIGNA") else None)
    if has("MEDICAID", "MEDI CAL", "CHIP", "AHCCCS") or has(*MCO_WORDS):
        lob = "Medicaid MCO" if (family or has(*MCO_WORDS) or has("PLAN")) and not has("FFS", "FEE FOR") else "Medicaid FFS"
        return (family or "Medicaid"), lob
    if has("MEDICARE", "NOVITAS", "NORIDIAN", "PALMETTO", "NGS", "WPS", "CGS", "FIRST COAST"):
        if family or has("ADVANTAGE", " MA ", "MAPD", "SENIOR"):
            return (family or "Other"), "Medicare Advantage"
        return "Medicare", "Medicare FFS"
    if has("ADVANTAGE", "MAPD") and family:
        return family, "Medicare Advantage"
    return (family or "Other"), "Commercial"


def enrich(payer_id: str, name: str | None, given: dict | None = None) -> tuple[dict, list[str]]:
    """Return a full payers row and any notes about values that were defaulted."""
    given = {k: v for k, v in (given or {}).items() if v not in (None, "")}
    notes = []
    base = REGISTRY.get(payer_id)
    if base is None and name:
        nn = _norm(name)
        base = next((r for r in REGISTRY.values() if _norm(r["payer_name"]) == nn), None)
    if base:
        row = {**base, "payer_id": payer_id}
    else:
        fam, lob = classify(name or payer_id)
        row = {"payer_id": payer_id, "payer_name": name or payer_id, "payer_family": fam, "line_of_business": lob,
               "state": None, "timely_filing_days": None, "appeal_limit_days": None,
               "ai_call_status": "Not yet checked", "status_channel": "IVR" if lob == "Medicare FFS" else "EDI_276"}
    row.update({k: given[k] for k in ("payer_name", "line_of_business", "state", "timely_filing_days",
                                      "appeal_limit_days", "ai_call_status") if k in given})
    if "line_of_business" in given and not base:
        row["status_channel"] = "IVR" if given["line_of_business"] == "Medicare FFS" else "EDI_276"
    if row["timely_filing_days"] is None:
        row["timely_filing_days"] = DEFAULT_TFL.get(row["line_of_business"], 90)
        notes.append(f"timely filing limit not supplied; defaulted to {row['timely_filing_days']} days")
    if row["appeal_limit_days"] is None:
        row["appeal_limit_days"] = 90
        notes.append("appeal limit not supplied; defaulted to 90 days")
    row["timely_filing_days"] = int(row["timely_filing_days"])
    row["appeal_limit_days"] = int(row["appeal_limit_days"])
    return row, notes

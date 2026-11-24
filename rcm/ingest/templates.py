"""Import templates: one file per table, with the columns a billing system export must map to.

Required files: patients, claims, claim_lines.
Optional: payers, providers, coverages, authorizations, fee_schedule.
Missing optional files are derived where possible (payers and providers from
the claims, coverage from claim member IDs); edits that need a missing file
(e.g. the authorization edit) are switched off rather than guessed.
"""
from __future__ import annotations

from pathlib import Path

# table -> [(column, required, type, description)]
# types: text, date, money (dollars), int, pct (0-1 or "20%"), npi
TEMPLATES: dict[str, list[tuple[str, bool, str, str]]] = {
    "patients": [
        ("patient_id", True, "text", "Your patient/account identifier (any unique value)"),
        ("first_name", False, "text", "Removed if you de-identify"),
        ("last_name", False, "text", "Removed if you de-identify"),
        ("dob", True, "date", "Date of birth"),
        ("sex", False, "text", "M / F / U"),
        ("state", False, "text", "2-letter state"),
        ("zip", False, "text", "5-digit ZIP"),
        ("mrn", False, "text", "Medical record number"),
    ],
    "claims": [
        ("claim_id", True, "text", "Patient control number / claim number you sent (CLM01)"),
        ("patient_id", True, "text", "Must exist in patients"),
        ("payer_id", True, "text", "Payer ID (as in payers, or your clearinghouse payer ID)"),
        ("payer_name", False, "text", "Used to create the payer if payers.csv is not supplied"),
        ("member_id", False, "text", "Subscriber/member ID"),
        ("billing_npi", True, "npi", "Billing provider NPI"),
        ("rendering_npi", False, "npi", "Rendering provider NPI (defaults to billing NPI)"),
        ("date_of_service", True, "date", "Claim date of service (first DOS if a range)"),
        ("place_of_service", False, "text", "2-digit POS code (default 11)"),
        ("frequency_code", False, "text", "1 original, 7 replacement, 8 void (default 1)"),
        ("dx1", True, "text", "Primary ICD-10 (with or without the dot)"),
        ("dx2", False, "text", ""), ("dx3", False, "text", ""), ("dx4", False, "text", ""),
        ("prior_auth_number", False, "text", "Auth number sent on the claim (REF*G1)"),
        ("submission_date", False, "date", "Date sent to the payer; blank = not yet billed"),
        ("payer_claim_number", False, "text", "Payer ICN/DCN if known"),
    ],
    "claim_lines": [
        ("claim_id", True, "text", "Must exist in claims"),
        ("line_number", True, "int", "1, 2, 3 ..."),
        ("cpt_code", True, "text", "CPT/HCPCS"),
        ("modifier1", False, "text", ""), ("modifier2", False, "text", ""),
        ("units", False, "int", "Default 1"),
        ("charge", True, "money", "Billed amount for the line"),
        ("dx_pointer", False, "text", "Which dx support the line, e.g. 1 or 1,2 (default 1)"),
    ],
    "payers": [
        ("payer_id", True, "text", ""),
        ("payer_name", True, "text", ""),
        ("line_of_business", False, "text", "Commercial / Medicare FFS / Medicare Advantage / Medicaid FFS / Medicaid MCO / Military"),
        ("state", False, "text", "Blank = national"),
        ("timely_filing_days", False, "int", "Filing limit in days (default 90, or from the built-in payer list)"),
        ("appeal_limit_days", False, "int", "Appeal limit in days (default 90)"),
        ("ai_call_status", False, "text", "Prohibited / Restricted / Conditional / IVR-required / Silent / Allowed / Not yet checked"),
    ],
    "providers": [
        ("npi", True, "npi", ""),
        ("name", True, "text", ""),
        ("provider_type", False, "text", "Billing / Rendering"),
        ("specialty", False, "text", ""),
        ("taxonomy_code", False, "text", ""),
        ("tax_id", False, "text", ""),
        ("state", False, "text", ""),
        ("billing_npi", False, "npi", "For rendering providers: their group NPI"),
    ],
    "coverages": [
        ("patient_id", True, "text", ""),
        ("payer_id", True, "text", ""),
        ("member_id", True, "text", ""),
        ("group_number", False, "text", ""),
        ("coverage_rank", False, "int", "1 primary, 2 secondary (default 1)"),
        ("effective_date", False, "date", ""),
        ("termination_date", False, "date", "Blank = active; drives the eligibility edit"),
        ("copay", False, "money", ""), ("deductible", False, "money", ""),
        ("coinsurance_pct", False, "pct", "0.2 or 20%"),
    ],
    "authorizations": [
        ("patient_id", True, "text", ""),
        ("payer_id", True, "text", ""),
        ("cpt_code", True, "text", ""),
        ("auth_number", True, "text", ""),
        ("status", False, "text", "Approved / Denied / Pending (default Approved)"),
        ("valid_from", True, "date", ""),
        ("valid_to", True, "date", ""),
    ],
    "fee_schedule": [
        ("payer_id", True, "text", ""),
        ("cpt_code", True, "text", ""),
        ("allowed_amount", False, "money", "Contracted allowed per unit (use this OR allowed_pct)"),
        ("allowed_pct", False, "pct", "Allowed as a share of billed charge"),
    ],
}
REQUIRED_FILES = ["patients", "claims", "claim_lines"]

# Common export headers -> template column
ALIASES = {
    "dos": "date_of_service", "service_date": "date_of_service", "from_date": "date_of_service",
    "date_of_birth": "dob", "birth_date": "dob", "gender": "sex",
    "cpt": "cpt_code", "hcpcs": "cpt_code", "procedure_code": "cpt_code", "proc_code": "cpt_code",
    "mod1": "modifier1", "mod2": "modifier2", "modifier": "modifier1",
    "charge_amount": "charge", "billed_amount": "charge", "charges": "charge", "billed": "charge", "amount": "charge",
    "line": "line_number", "line_no": "line_number", "line_num": "line_number",
    "claim_number": "claim_id", "claim_no": "claim_id", "pcn": "claim_id", "patient_control_number": "claim_id",
    "account_number": "patient_id", "patient_account": "patient_id", "acct": "patient_id",
    "subscriber_id": "member_id", "policy_number": "member_id", "insurance_id": "member_id",
    "payer": "payer_name", "insurance": "payer_name", "insurance_name": "payer_name", "payor_id": "payer_id",
    "payor_name": "payer_name", "pos": "place_of_service",
    "icd1": "dx1", "icd2": "dx2", "icd3": "dx3", "icd4": "dx4", "diag1": "dx1", "diag2": "dx2", "diag3": "dx3",
    "diag4": "dx4", "primary_dx": "dx1", "pointer": "dx_pointer", "dx_ptr": "dx_pointer",
    "auth": "prior_auth_number", "auth_number_on_claim": "prior_auth_number", "authorization": "prior_auth_number",
    "bill_date": "submission_date", "billed_date": "submission_date", "sent_date": "submission_date",
    "icn": "payer_claim_number", "dcn": "payer_claim_number",
    "billing_provider_npi": "billing_npi", "rendering_provider_npi": "rendering_npi",
    "provider_npi": "npi", "provider_name": "name", "tfl": "timely_filing_days", "timely_filing": "timely_filing_days",
    "allowed": "allowed_amount", "contract_rate": "allowed_amount",
}


def write_templates(folder: str | Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for table, cols in TEMPLATES.items():
        (folder / f"{table}.csv").write_text(",".join(c[0] for c in cols) + "\n", encoding="utf-8-sig")
    lines = ["# Import templates", "",
             "Fill one CSV (or .xlsx with the same name) per table. Required: "
             + ", ".join(f"`{t}`" for t in REQUIRED_FILES) + ". The rest are optional.", "",
             "Dates: YYYY-MM-DD, MM/DD/YYYY, M/D/YY or YYYYMMDD. Money: 1234.5, $1,234.50.",
             "Column names are matched loosely (case, spaces, common export names like DOS, CPT, Billed Amount).", "",
             "Then: `python -m rcm ingest --csv <this folder> --x835 <folder of 835s> --out data --deidentify`", ""]
    for table, cols in TEMPLATES.items():
        lines += [f"## {table}.csv" + ("  (required)" if table in REQUIRED_FILES else "  (optional)"), "",
                  "| Column | Required | Type | Notes |", "|---|---|---|---|"]
        lines += [f"| {c} | {'yes' if r else ''} | {t} | {d} |" for c, r, t, d in cols]
        lines.append("")
    (folder / "TEMPLATE_GUIDE.md").write_text("\n".join(lines), encoding="utf-8")
    return folder

"""De-identify a loaded database in place.

What changes:
  - patient, claim, encounter, coverage and auth IDs, MRNs, member IDs, group numbers,
    auth numbers and payer claim numbers -> keyed hashes (HMAC-SHA256). The same key
    gives the same hash, so later loads link up; without the key they can't be reversed.
  - patient first/last names -> removed
  - date of birth -> January 1 of the birth year; anyone over 89 is grouped at 90
  - ZIP -> first 3 digits (00000 for the sparsely populated 3-digit areas)
  - the file is compacted afterwards so no old values stay in free pages

What stays: service, submission and payment dates, codes, amounts, payers and
provider NPIs, because the revenue cycle math depends on them.
That means this is not HIPAA Safe Harbor on its own (Safe Harbor also removes
service dates). Use it under a BAA, or have the dataset reviewed under the
Expert Determination method, before it leaves the client's control.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import date

RESTRICTED_ZIP3 = {"036", "059", "063", "102", "203", "556", "692", "790", "821", "823", "830", "831", "878",
                   "879", "884", "890", "893"}

# table -> {column: hash prefix}
HASHED = {
    "patients": {"patient_id": "P", "mrn": "M"},
    "coverages": {"coverage_id": "COV", "patient_id": "P", "member_id": "MBR", "group_number": "G"},
    "authorizations": {"auth_id": "AU", "patient_id": "P", "auth_number": "AUTH"},
    "encounters": {"encounter_id": "E", "patient_id": "P", "coverage_id": "COV"},
    "claims": {"claim_id": "C", "encounter_id": "E", "patient_id": "P", "coverage_id": "COV",
               "prior_auth_number": "AUTH", "payer_claim_number": "ICN"},
    "claim_lines": {"claim_id": "C"},
    "claim_events": {"claim_id": "C"},
    "remit_claims": {"claim_id": "C", "payer_claim_number": "ICN"},
    "remit_adjustments": {"claim_id": "C"},
    "remit_lines": {"claim_id": "C"},
    "truth_denial_drivers": {"claim_id": "C"},
    "truth_underpayments": {"claim_id": "C"},
}


def new_key() -> str:
    return secrets.token_hex(16)


def deidentify(con, key: str, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    k = key.encode()

    def h(prefix, value):
        if value is None or value == "":
            return value
        return prefix + hmac.new(k, str(value).encode(), hashlib.sha256).hexdigest()[:12].upper()

    def dob(value):
        if not value:
            return value
        year = max(int(str(value)[:4]), as_of.year - 90)
        return f"{year}-01-01"

    def zip3(value):
        z = (value or "")[:3]
        if len(z) < 3:
            return ""
        return "00000" if z in RESTRICTED_ZIP3 else z + "00"

    con.create_function("h", 2, h, deterministic=True)
    con.create_function("dob_year", 1, dob, deterministic=True)
    con.create_function("zip3", 1, zip3, deterministic=True)
    con.commit()
    con.execute("PRAGMA foreign_keys = OFF")
    existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {}
    for table, cols in HASHED.items():
        if table not in existing:
            continue
        sets = ", ".join(f"{c} = h('{p}', {c})" for c, p in cols.items())
        if table == "patients":
            sets += ", first_name = '', last_name = h('PT', patient_id), dob = dob_year(dob), zip = zip3(zip)"
        counts[table] = con.execute(f"UPDATE {table} SET {sets}").rowcount
    con.execute("DELETE FROM data_sources WHERE source = 'deidentified'")
    con.execute("INSERT INTO data_sources VALUES ('deidentified', 'keyed hash IDs; names removed; DOB to year; ZIP to 3 digits')")
    con.commit()
    problems = con.execute("PRAGMA foreign_key_check").fetchall()
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("VACUUM")
    return {"rows_changed": counts, "foreign_key_problems": len(problems)}

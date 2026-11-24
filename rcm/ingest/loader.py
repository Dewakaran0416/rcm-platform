"""Read client files (CSV / Excel templates, or parsed 837s), validate them, and build the platform database.

Bad rows never stop the whole load: every problem is reported with its file,
row and column, and any claim touched by an error is left out (with its
lines), so what loads is internally consistent.
Messages never echo patient names, DOB or member IDs. Claim IDs are included
so the billing team can find and fix the row; treat the report as internal.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from ..generator import Dataset
from ..reference import SPECIALTIES, cpt_info
from .payers import enrich
from .templates import ALIASES, REQUIRED_FILES, TEMPLATES

TAXONOMY_TO_SPECIALTY = {m["taxonomy"]: s for s, m in SPECIALTIES.items()}
PHI_COLUMNS = {"first_name", "last_name", "dob", "member_id", "mrn", "zip", "patient_id", "group_number"}


class Issues:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, file, row, column, severity, message, claim_id=None, patient_id=None):
        self.items.append({"file": file, "row": row, "column": column, "severity": severity,
                           "message": message, "_claim": claim_id, "_patient": patient_id})

    @property
    def errors(self):
        return [i for i in self.items if i["severity"] == "error"]

    def public(self) -> list[dict]:
        return [{k: v for k, v in i.items() if not k.startswith("_")} for i in self.items]

    def write_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["file", "row", "column", "severity", "message"])
            w.writeheader()
            w.writerows(self.public())


# ------------------------------------------------------------------ reading files
def norm_header(h: str) -> str:
    h = re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")
    return ALIASES.get(h, h)


def read_table(path: Path) -> list[dict]:
    """Rows as {normalised column: raw string}. CSV (UTF-8 or Windows-1252) or .xlsx (needs openpyxl)."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as e:
            raise SystemExit(f"{path.name}: reading Excel needs openpyxl (pip install openpyxl), or save as CSV") from e
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        headers = [norm_header(h) for h in next(it, [])]
        out = []
        for r in it:
            if r is None or all(v in (None, "") for v in r):
                continue
            out.append({h: ("" if v is None else v) for h, v in zip(headers, r) if h})
        return out
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as fh:
                reader = csv.reader(fh)
                headers = [norm_header(h) for h in next(reader, [])]
                return [{h: v.strip() for h, v in zip(headers, r) if h}
                        for r in reader if any(c.strip() for c in r)]
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"{path.name}: could not read the file encoding")


def find_file(folder: Path, table: str) -> Path | None:
    for f in folder.iterdir():
        if f.stem.lower() == table and f.suffix.lower() in (".csv", ".xlsx", ".xlsm"):
            return f
    return None


# ------------------------------------------------------------------ value parsing
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d")


def parse_date(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if re.fullmatch(r"\d{5}(\.0+)?", s):                       # Excel serial number
        return (date(1899, 12, 30) + timedelta(days=int(float(s)))).isoformat()
    s = s.split(" ")[0] if re.match(r"\d{4}-\d{2}-\d{2} ", s) else s
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    raise ValueError("not a date")


def parse_money(v) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v) * 100))
    s = str(v).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    return int(round(float(s) * 100)) * (-1 if neg else 1)


def parse_pct(v) -> float | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.endswith("%"):
        return float(s[:-1]) / 100
    x = float(s)
    return x / 100 if x > 1 else x


def parse_int(v) -> int | None:
    if v in (None, ""):
        return None
    return int(float(str(v).strip()))


def npi_ok(npi: str) -> bool:
    if not re.fullmatch(r"\d{10}", npi or ""):
        return False
    digits = [int(d) for d in "80840" + npi]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return total % 10 == 0


def norm_dx(code: str | None) -> str | None:
    if not code:
        return None
    c = str(code).strip().upper().replace(".", "")
    return c if len(c) <= 3 else f"{c[:3]}.{c[3:]}"


def norm_pointer(p, default: str = "") -> str:
    """'1,2' style pointer. Blank stays blank (a real claim defect the scrubber should see), unless the
    export has no pointer column at all, in which case `default` ("1") is used."""
    if p in (None, ""):
        return default
    parts = re.split(r"[,:;\s]+", str(p).strip())
    out = []
    for x in parts:
        if not x:
            continue
        if x.upper() in "ABCDEFGHIJKL" and len(x) == 1:
            x = str("ABCDEFGHIJKL".index(x.upper()) + 1)
        x = str(int(float(x)))
        if x not in out:
            out.append(x)
    return ",".join(out[:4]) or "1"


PARSERS = {"date": parse_date, "money": parse_money, "pct": parse_pct, "int": parse_int}


def typed_rows(table: str, raw: list[dict], issues: Issues, fname: str) -> list[dict]:
    """Template columns, typed. Rows with errors keep a '_bad' flag (and are dropped later)."""
    cols = TEMPLATES[table]
    if raw:
        present = set(raw[0])
        for c, req, _, _ in cols:
            if req and c not in present:
                issues.add(fname, None, c, "error", "required column missing from the file")
    out = []
    for i, r in enumerate(raw, start=2):                       # row 1 is the header
        rec, bad = {"_row": i}, False
        for c, req, typ, _ in cols:
            v = r.get(c, "")
            v = v.strip() if isinstance(v, str) else v
            if v in (None, ""):
                rec[c] = None
                if req:
                    issues.add(fname, i, c, "error", "required value is blank",
                               r.get("claim_id"), r.get("patient_id"))
                    bad = True
                continue
            try:
                rec[c] = PARSERS[typ](v) if typ in PARSERS else str(v).strip()
            except (ValueError, TypeError):
                shown = "" if c in PHI_COLUMNS else f" ('{v}')"
                issues.add(fname, i, c, "error", f"could not read as {typ}{shown}", r.get("claim_id"), r.get("patient_id"))
                rec[c] = None
                bad = True
            if typ == "npi" and rec[c] and not npi_ok(rec[c]):
                issues.add(fname, i, c, "warning", f"NPI {rec[c]} fails the NPI check digit")
        rec["_bad"] = bad
        out.append(rec)
    return out


def read_folder(folder: str | Path, issues: Issues) -> dict[str, list[dict]]:
    folder = Path(folder)
    records = {}
    for table in TEMPLATES:
        f = find_file(folder, table)
        if f is None:
            if table in REQUIRED_FILES:
                issues.add(f"{table}.csv", None, None, "error", "required file not found")
            continue
        raw = read_table(f)
        records.setdefault("_columns", {})[table] = set(raw[0]) if raw else set()
        records[table] = typed_rows(table, raw, issues, f.name)
    return records


# ------------------------------------------------------------------ build the dataset
def build_dataset(records: dict[str, list[dict]], issues: Issues, source: str, detail: str = "",
                  today: date | None = None) -> tuple[Dataset, dict]:
    """Template-shaped typed records -> Dataset in the platform schema (bad claims excluded)."""
    today = today or date.today()
    ds = Dataset()
    pointer_default = "" if "dx_pointer" in records.get("_columns", {}).get("claim_lines", {"dx_pointer"}) else "1"
    rec = lambda t: records.get(t, [])

    # --- patients
    patients, bad_patients = {}, set()
    for p in rec("patients"):
        pid = p["patient_id"]
        if p["_bad"] or pid is None:
            if pid:
                bad_patients.add(pid)
            continue
        if pid in patients:
            issues.add("patients", p["_row"], "patient_id", "error", "duplicate patient_id; second row ignored")
            continue
        patients[pid] = p

    # --- claims and lines
    lines_by_claim = defaultdict(list)
    bad_claims = set()
    for ln in rec("claim_lines"):
        if ln["claim_id"] is None:
            continue
        if ln["_bad"]:
            bad_claims.add(ln["claim_id"])
        lines_by_claim[ln["claim_id"]].append(ln)
    claims = {}
    for c in rec("claims"):
        cid = c["claim_id"]
        if cid is None:
            continue
        if c["_bad"]:
            bad_claims.add(cid)
            continue
        if cid in claims:
            issues.add("claims", c["_row"], "claim_id", "error", f"duplicate claim_id {cid}; claim excluded")
            bad_claims.add(cid)
            continue
        if c["patient_id"] not in patients:
            why = "patient row has errors" if c["patient_id"] in bad_patients else "patient_id not in patients"
            issues.add("claims", c["_row"], "patient_id", "error", f"{why}; claim excluded", cid)
            bad_claims.add(cid)
            continue
        if c["date_of_service"] and c["date_of_service"] > today.isoformat():
            issues.add("claims", c["_row"], "date_of_service", "warning", "date of service is in the future", cid)
        if c["submission_date"] and c["date_of_service"] and c["submission_date"] < c["date_of_service"]:
            issues.add("claims", c["_row"], "submission_date", "error", "submission date before date of service", cid)
            bad_claims.add(cid)
            continue
        claims[cid] = c
    for cid in lines_by_claim:
        if cid not in claims and cid not in bad_claims:
            issues.add("claim_lines", lines_by_claim[cid][0]["_row"], "claim_id", "error",
                       f"lines for claim {cid}, which is not in claims; lines excluded")
    for cid in list(claims):
        lines = lines_by_claim.get(cid, [])
        nums = [ln["line_number"] for ln in lines]
        if not lines:
            issues.add("claims", claims[cid]["_row"], "claim_id", "error", f"claim {cid} has no lines; excluded", cid)
            bad_claims.add(cid)
        elif len(nums) != len(set(nums)):
            issues.add("claim_lines", lines[0]["_row"], "line_number", "error", f"claim {cid} repeats a line number; excluded", cid)
            bad_claims.add(cid)
        if cid in bad_claims:
            claims.pop(cid)
    unknown_cpt = sorted({ln["cpt_code"] for cid in claims for ln in lines_by_claim[cid]
                          if cpt_info(ln["cpt_code"])[0] == ln["cpt_code"]})
    if unknown_cpt:
        issues.add("claim_lines", None, "cpt_code", "info",
                   f"{len(unknown_cpt)} CPT/HCPCS codes are outside the built-in rule set (no auth/dx edits for them): "
                   + ", ".join(unknown_cpt[:15]) + (" ..." if len(unknown_cpt) > 15 else ""))

    # --- payers
    given_payers = {p["payer_id"]: p for p in rec("payers") if not p["_bad"] and p["payer_id"]}
    payer_names = {c["payer_id"]: c.get("payer_name") for c in claims.values()}
    payers = {}
    for pid in sorted(set(given_payers) | set(payer_names)):
        g = given_payers.get(pid, {})
        row, notes = enrich(pid, g.get("payer_name") or payer_names.get(pid), g)
        payers[pid] = row
        for n in notes:
            issues.add("payers", None, pid, "warning", f"{pid}: {n}")

    # --- providers
    providers = {}
    for p in rec("providers"):
        if p["_bad"] or not p["npi"]:
            continue
        providers[p["npi"]] = {"provider_npi": p["npi"], "provider_name": p["name"] or p["npi"],
                               "provider_type": p["provider_type"] or "Rendering",
                               "specialty": p["specialty"] or TAXONOMY_TO_SPECIALTY.get(p["taxonomy_code"] or "", "Unknown"),
                               "taxonomy_code": p["taxonomy_code"] or "", "tax_id": p["tax_id"] or "",
                               "state": p["state"] or "", "billing_npi": p["billing_npi"]}
    for c in claims.values():
        b = c["billing_npi"]
        if b not in providers:
            providers[b] = {"provider_npi": b, "provider_name": c.get("_billing_name") or f"Billing provider {b}",
                            "provider_type": "Billing", "specialty": "Unknown", "taxonomy_code": c.get("_billing_taxonomy") or "",
                            "tax_id": c.get("_tax_id") or "", "state": c.get("_billing_state") or "", "billing_npi": None}
        r = c["rendering_npi"] or b
        if r not in providers:
            tax = c.get("_rendering_taxonomy") or ""
            providers[r] = {"provider_npi": r, "provider_name": c.get("_rendering_name") or f"Provider {r}",
                            "provider_type": "Rendering", "specialty": TAXONOMY_TO_SPECIALTY.get(tax, "Unknown"),
                            "taxonomy_code": tax, "tax_id": providers[b]["tax_id"], "state": providers[b]["state"],
                            "billing_npi": b}
    for p in providers.values():                          # dangling group NPI -> drop the link
        if p["billing_npi"] and p["billing_npi"] not in providers:
            p["billing_npi"] = None

    # --- coverages
    coverages, cov_key = {}, {}
    for cv in rec("coverages"):
        if cv["_bad"] or cv["patient_id"] not in patients:
            continue
        if cv["payer_id"] not in payers:
            row, _ = enrich(cv["payer_id"], None)
            payers[cv["payer_id"]] = row
        cid = f"COV-{len(coverages) + 1:07d}"
        coverages[cid] = {"coverage_id": cid, "patient_id": cv["patient_id"], "payer_id": cv["payer_id"],
                          "member_id": cv["member_id"], "group_number": cv["group_number"],
                          "coverage_rank": cv["coverage_rank"] or 1, "effective_date": cv["effective_date"] or "1900-01-01",
                          "termination_date": cv["termination_date"], "copay_cents": cv["copay"] or 0,
                          "deductible_cents": cv["deductible"] or 0, "coinsurance_pct": cv["coinsurance_pct"] or 0.0}
        cov_key.setdefault((cv["patient_id"], cv["payer_id"]), []).append(cid)
    for c in sorted(claims.values(), key=lambda x: x["date_of_service"]):
        key = (c["patient_id"], c["payer_id"])
        options = cov_key.get(key, [])
        match = next((o for o in options if coverages[o]["member_id"] == c["member_id"]), options[0] if options else None)
        if match is None:
            match = f"COV-{len(coverages) + 1:07d}"
            coverages[match] = {"coverage_id": match, "patient_id": c["patient_id"], "payer_id": c["payer_id"],
                                "member_id": c["member_id"] or "", "group_number": c.get("_group"),
                                "coverage_rank": c.get("_rank") or 1, "effective_date": c["date_of_service"],
                                "termination_date": None, "copay_cents": 0, "deductible_cents": 0, "coinsurance_pct": 0.0}
            cov_key.setdefault(key, []).append(match)
        c["_coverage_id"] = match

    # --- authorizations
    auths = []
    for a in rec("authorizations"):
        if a["_bad"] or a["patient_id"] not in patients:
            continue
        if a["payer_id"] not in payers:
            payers[a["payer_id"]], _ = enrich(a["payer_id"], None)
        auths.append({"auth_id": f"AU-{len(auths) + 1:07d}", "patient_id": a["patient_id"], "payer_id": a["payer_id"],
                      "cpt_code": a["cpt_code"], "auth_number": a["auth_number"], "status": a["status"] or "Approved",
                      "valid_from": a["valid_from"], "valid_to": a["valid_to"]})

    # --- fee schedule
    fees = {}
    for f in rec("fee_schedule"):
        if f["_bad"]:
            continue
        if f["allowed_amount"] is None and f["allowed_pct"] is None:
            issues.add("fee_schedule", f["_row"], "allowed_amount", "error", "needs allowed_amount or allowed_pct")
            continue
        if f["payer_id"] not in payers:
            payers[f["payer_id"]], _ = enrich(f["payer_id"], None)
        fees[(f["payer_id"], f["cpt_code"])] = {"payer_id": f["payer_id"], "cpt_code": f["cpt_code"],
                                                 "allowed_pct": f["allowed_pct"], "allowed_cents": f["allowed_amount"]}

    # --- write into the dataset
    ds.payers = list(payers.values())
    order = ["provider_npi", "provider_name", "provider_type", "specialty", "taxonomy_code", "tax_id", "state", "billing_npi"]
    groups = [p for p in providers.values() if not p["billing_npi"]]
    ds.providers = [{k: p[k] for k in order} for p in groups + [p for p in providers.values() if p["billing_npi"]]]
    used_patients = {c["patient_id"] for c in claims.values()} | {a["patient_id"] for a in auths} | \
                    {cv["patient_id"] for cv in coverages.values()}
    mrns = set()
    for pid in sorted(used_patients):
        p = patients[pid]
        mrn = p["mrn"] or pid
        if mrn in mrns:
            mrn = f"{mrn}~{pid}"
        mrns.add(mrn)
        ds.patients.append({"patient_id": pid, "mrn": mrn, "first_name": p["first_name"] or "",
                            "last_name": p["last_name"] or "", "dob": p["dob"], "sex": (p["sex"] or "U")[:1].upper(),
                            "state": p["state"] or "", "zip": (p["zip"] or "")[:5]})
    ds.coverages = list(coverages.values())
    ds.authorizations = auths
    ds.fee_schedule = list(fees.values())
    for cid, c in sorted(claims.items()):
        lines = sorted(lines_by_claim[cid], key=lambda x: x["line_number"])
        rendering = c["rendering_npi"] or c["billing_npi"]
        enc = f"E-{cid}"
        ds.encounters.append({"encounter_id": enc, "patient_id": c["patient_id"], "coverage_id": c["_coverage_id"],
                              "rendering_npi": rendering, "billing_npi": c["billing_npi"],
                              "date_of_service": c["date_of_service"], "place_of_service": c["place_of_service"] or "11",
                              "charge_entry_date": c["date_of_service"]})
        dx = [norm_dx(c[k]) for k in ("dx1", "dx2", "dx3", "dx4")]
        ds.claims.append({"claim_id": cid, "encounter_id": enc, "patient_id": c["patient_id"], "payer_id": c["payer_id"],
                          "coverage_id": c["_coverage_id"], "billing_npi": c["billing_npi"], "claim_type": "837P",
                          "frequency_code": c["frequency_code"] or "1", "dx1": dx[0], "dx2": dx[1], "dx3": dx[2], "dx4": dx[3],
                          "prior_auth_number": c["prior_auth_number"],
                          "total_charge_cents": sum(ln["charge"] for ln in lines),
                          "submission_date": c["submission_date"], "payer_received_date": None, "adjudication_date": None,
                          "payer_claim_number": c["payer_claim_number"],
                          "current_status": "Submitted" if c["submission_date"] else "Created",
                          "paid_cents": 0, "patient_resp_cents": 0})
        for ln in lines:
            ds.claim_lines.append({"claim_id": cid, "line_number": ln["line_number"], "cpt_code": ln["cpt_code"].upper(),
                                   "modifier1": ln["modifier1"], "modifier2": ln["modifier2"], "units": ln["units"] or 1,
                                   "charge_cents": ln["charge"], "dx_pointer": norm_pointer(ln["dx_pointer"], pointer_default),
                                   "allowed_cents": 0, "paid_cents": 0, "line_status": "Pending"})
        ds.claim_events.append({"claim_id": cid, "event_date": c["date_of_service"], "status": "Created",
                                "source": "PM", "status_category": None, "note": None})
        if c["submission_date"]:
            ds.claim_events.append({"claim_id": cid, "event_date": c["submission_date"], "status": "Submitted",
                                    "source": "PM", "status_category": None, "note": None})
    ds.data_sources = [{"source": source, "detail": detail or f"{len(ds.claims)} claims"}]
    for t in ("coverages", "authorizations", "fee_schedule"):
        if records.get(t):
            ds.data_sources.append({"source": t, "detail": f"{len(getattr(ds, t))} rows"})
    stats = {"claims_loaded": len(ds.claims), "claims_excluded": len(bad_claims),
             "lines_loaded": len(ds.claim_lines), "patients": len(ds.patients), "payers": len(ds.payers),
             "providers": len(ds.providers), "errors": len(issues.errors),
             "warnings": sum(i["severity"] == "warning" for i in issues.items)}
    return ds, stats

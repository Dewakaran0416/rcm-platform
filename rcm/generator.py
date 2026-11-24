"""Synthetic RCM data generator (Foundation module).

Produces a full claim lifecycle — patients, coverage, auths, encounters,
837 claims, 277-style status events and 835-style remits — with denials
driven by realistic causes, so later modules have something real to learn
from and act on. Everything is synthetic and reproducible from a seed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from .reference import (BUNDLING_PAIRS, CPT, ICD10, LOB_MIX, PAYER_BEHAVIOR,
                        PAYERS, SPECIALTIES)

FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
         "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Carlos", "Maria",
         "Daniel", "Nancy", "Matthew", "Lisa", "Anthony", "Betty", "Mark", "Sandra", "Priya", "Wei"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
        "Thompson", "White", "Harris", "Clark", "Lewis", "Walker", "Hall", "Young", "Patel", "Nguyen"]
STATES = ["PA", "NY", "TX", "MI", "KS", "AZ", "CA"]
PREVENTIVE = {"99396", "G0439"}          # covered preventive services: no copay, deductible or coinsurance


@dataclass
class Config:
    seed: int = 42
    n_patients: int = 2500
    n_encounters: int = 12000
    start: date = date(2026, 1, 1)
    months: int = 10
    as_of: date = date(2026, 11, 15)       # snapshot date: anything adjudicated later is still pending
    # base probabilities (scaled by each payer family's denial_mult)
    p_coverage_terminates: float = 0.035
    p_missing_auth: float = 0.12            # of auth-required services
    p_bad_dx: float = 0.035
    p_missing_modifier: float = 0.18        # of bundling pairs
    p_late_submission: float = 0.015
    p_missing_info: float = 0.018
    p_duplicate: float = 0.012
    p_non_covered: float = 0.008
    p_clearinghouse_reject: float = 0.035
    p_underpayment: float = 0.03            # paid lines priced below contract


@dataclass
class Dataset:
    payers: list = field(default_factory=list)
    providers: list = field(default_factory=list)
    patients: list = field(default_factory=list)
    coverages: list = field(default_factory=list)
    authorizations: list = field(default_factory=list)
    encounters: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    claim_lines: list = field(default_factory=list)
    claim_events: list = field(default_factory=list)
    remits: list = field(default_factory=list)
    remit_claims: list = field(default_factory=list)
    remit_adjustments: list = field(default_factory=list)
    truth_denial_drivers: list = field(default_factory=list)
    fee_schedule: list = field(default_factory=list)
    truth_underpayments: list = field(default_factory=list)
    data_sources: list = field(default_factory=list)
    remit_lines: list = field(default_factory=list)

    TABLES = ["payers", "providers", "patients", "coverages", "authorizations", "encounters", "claims",
              "claim_lines", "claim_events", "remits", "remit_claims", "remit_lines", "remit_adjustments", "truth_denial_drivers",
              "fee_schedule", "truth_underpayments", "data_sources"]


def luhn_npi(rng: random.Random) -> str:
    """Synthetic 10-digit NPI with a valid check digit (prefix 80840 per the NPI standard)."""
    base = "1" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    digits = [int(d) for d in "80840" + base]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check = (10 - total % 10) % 10
    return base + str(check)


def _d(x: date) -> str:
    return x.isoformat()


def _cents(dollars: float) -> int:
    return int(round(dollars * 100))


class Generator:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.rng = random.Random(self.cfg.seed)
        self.ds = Dataset()
        self.payer = {p[0]: dict(zip(
            ["payer_id", "payer_name", "payer_family", "line_of_business", "state",
             "timely_filing_days", "appeal_limit_days", "ai_call_status", "status_channel"], p)) for p in PAYERS}
        self._seq = {}
        # deterministic per payer+cpt allowed factor
        self._allowed_factor = {}

    def _id(self, prefix: str, width: int = 6) -> str:
        n = self._seq.get(prefix, 0) + 1
        self._seq[prefix] = n
        return f"{prefix}{n:0{width}d}"

    # ------------------------------------------------------------------ reference
    def build_payers(self):
        self.ds.payers = list(self.payer.values())
        # contracted allowed rate per payer x CPT (share of billed charge)
        for pid in sorted(self.payer):
            lo, hi = PAYER_BEHAVIOR[self.payer[pid]["payer_family"]]["allowed_pct"]
            for cpt in sorted(CPT):
                pct = round(self.rng.uniform(lo, hi), 4)
                self._allowed_factor[(pid, cpt)] = pct
                self.ds.fee_schedule.append({"payer_id": pid, "cpt_code": cpt, "allowed_pct": pct})

    def build_providers(self):
        r = self.rng
        for state in STATES:
            for spec, meta in SPECIALTIES.items():
                if spec == "Radiology" and state in ("KS", "AZ"):
                    continue
                group_npi = luhn_npi(r)
                tax_id = f"{r.randint(10, 99)}-{r.randint(1000000, 9999999)}"
                self.ds.providers.append({
                    "provider_npi": group_npi, "provider_name": f"{state} {spec} Associates (synthetic)",
                    "provider_type": "Billing", "specialty": spec, "taxonomy_code": meta["taxonomy"],
                    "tax_id": tax_id, "state": state, "billing_npi": None})
                for _ in range(r.randint(2, 4)):
                    self.ds.providers.append({
                        "provider_npi": luhn_npi(r), "provider_name": f"Dr. {r.choice(FIRST)} {r.choice(LAST)} (synthetic)",
                        "provider_type": "Rendering", "specialty": spec, "taxonomy_code": meta["taxonomy"],
                        "tax_id": tax_id, "state": state, "billing_npi": group_npi})

    # ------------------------------------------------------------------ patient access
    def _pick_payer(self, age: int, state: str) -> str:
        """Line of business by age band, then payer family, then a payer available in the patient's state."""
        r = self.rng
        band = "senior" if age >= 65 else "under65"
        while True:
            lobs = LOB_MIX[band]
            lob = r.choices(list(lobs), weights=[v[0] for v in lobs.values()], k=1)[0]
            fams = lobs[lob][1]
            fam = r.choices(list(fams), weights=list(fams.values()), k=1)[0]
            candidates = [pid for pid, p in self.payer.items()
                          if p["payer_family"] == fam and p["line_of_business"] == lob
                          and (p["state"] in (None, state))
                          and (pid != "TFL" or band == "senior") and (pid != "TRIW" or band == "under65")]
            if candidates:
                return r.choice(candidates)

    def _unique_mrn(self) -> str:
        """Random-looking 8-digit MRN, redrawn on the rare collision (MRN is unique in the schema)."""
        used = self.__dict__.setdefault("_mrns", set())
        while True:
            mrn = f"MRN{self.rng.randint(10**7, 10**8 - 1)}"
            if mrn not in used:
                used.add(mrn)
                return mrn

    def build_patients(self):
        r, c = self.rng, self.cfg
        window_end = c.start + timedelta(days=30 * c.months)
        for _ in range(c.n_patients):
            age = int(min(95, max(1, r.gauss(52, 20))))
            state = r.choice(STATES)
            pid = self._id("PT")
            dob = date(c.start.year - age, r.randint(1, 12), r.randint(1, 28))
            self.ds.patients.append({
                "patient_id": pid, "mrn": self._unique_mrn(),
                "first_name": r.choice(FIRST), "last_name": r.choice(LAST), "dob": _d(dob),
                "sex": r.choice("MF"), "state": state, "zip": f"{r.randint(10000, 99999)}"})
            payer_id = self._pick_payer(age, state)
            term = None
            if r.random() < c.p_coverage_terminates:
                term = c.start + timedelta(days=r.randint(60, (window_end - c.start).days - 30))
            fam = self.payer[payer_id]["payer_family"]
            commercial = self.payer[payer_id]["line_of_business"] == "Commercial"
            self.ds.coverages.append({
                "coverage_id": self._id("COV"), "patient_id": pid, "payer_id": payer_id,
                "member_id": f"{payer_id[:3]}{r.randint(10**8, 10**9 - 1)}",
                "group_number": f"G{r.randint(10000, 99999)}" if commercial else None,
                "coverage_rank": 1, "effective_date": _d(date(c.start.year - r.randint(0, 5), 1, 1)),
                "termination_date": _d(term) if term else None,
                "copay_cents": _cents(r.choice([20, 25, 30, 40])) if commercial else 0,
                # plan deductible; much of it is usually met at other providers, so we only track the remainder here
                "deductible_cents": (_cents(r.choice([0, 0, 250, 500, 750, 1000])) if commercial
                                     else (_cents(283) if fam == "Medicare" else 0)),
                "coinsurance_pct": 0.2 if fam in ("Medicare", "BCBS", "Aetna", "UHC", "UPMC") else 0.0,
            })

    # ------------------------------------------------------------------ encounters + claims
    def _services(self, spec: str):
        """Return list of (cpt, units) for one visit of this specialty."""
        r = self.rng
        cpts = SPECIALTIES[spec]["cpts"]
        em = [x for x in cpts if x.startswith("99") or x == "G0439"]
        if spec == "Physical Therapy":
            if r.random() < 0.15:
                return [("97161", 1)]
            svc = [("97110", r.randint(2, 3))]
            if r.random() < 0.6:
                svc.append(("97530", r.randint(1, 2)))
            if r.random() < 0.55:
                svc.append(("97140", r.randint(1, 2)))
            return svc
        if spec == "Radiology":
            return [(r.choice(cpts), 1)]
        visit = [(r.choice(em), 1)]
        if spec == "Family Medicine":
            if r.random() < 0.35:
                visit += [("36415", 1), (r.choice(["80053", "85025", "83036"]), 1)]
            if r.random() < 0.08:
                visit.append(("93000", 1))
        elif spec == "Cardiology":
            roll = r.random()
            if roll < 0.45:
                visit.append(("93000", 1))
            elif roll < 0.60:
                visit = [("93306", 1)]
            elif roll < 0.68:
                visit = [("78452", 1)]
        elif spec == "Orthopedics":
            roll = r.random()
            if roll < 0.30:
                visit += [("20610", 1), ("J3301", r.choice([2, 4]))]
            elif roll < 0.42:
                visit = [(r.choice(["73721", "72148"]), 1)]
            elif roll < 0.48:
                visit = [("29881", 1)]
        return visit

    def _dx_for(self, cpts, strict: bool):
        r = self.rng
        prefixes = None
        for cpt in cpts:
            allowed = CPT[cpt][4]
            if allowed:
                prefixes = allowed
                break
        good = [code for code in ICD10 if prefixes is None or any(code.startswith(p) for p in prefixes)]
        bad = [code for code in ICD10 if prefixes is not None and not any(code.startswith(p) for p in prefixes)]
        p_bad = self.cfg.p_bad_dx * (1.6 if strict else 1.0)
        mismatch = bool(bad) and r.random() < p_bad
        primary = r.choice(bad if mismatch else good)
        extras = r.sample([x for x in ICD10 if x != primary], k=r.randint(0, 2))
        return [primary] + extras, mismatch

    def _allowed_pct(self, payer_id: str, cpt: str) -> float:
        key = (payer_id, cpt)
        return self._allowed_factor[key]

    def build_encounters_and_claims(self):
        r, c = self.rng, self.cfg
        cov_by_patient = {cv["patient_id"]: cv for cv in self.ds.coverages}
        rendering = {}
        for p in self.ds.providers:
            if p["provider_type"] == "Rendering":
                rendering.setdefault((p["state"], p["specialty"]), []).append(p)
        window_days = 30 * c.months

        plans = []
        for _ in range(c.n_encounters):
            pt = r.choice(self.ds.patients)
            age = c.start.year - int(pt["dob"][:4])
            specs = [s for s in SPECIALTIES if (pt["state"], s) in rendering]
            weights = {"Family Medicine": 0.40, "Cardiology": 0.15, "Orthopedics": 0.17, "Radiology": 0.10, "Physical Therapy": 0.18}
            if age < 40:
                weights["Cardiology"] = 0.05
            spec = r.choices(specs, weights=[weights[s] for s in specs], k=1)[0]
            dos = c.start + timedelta(days=r.randint(0, window_days - 1))
            plans.append((dos, pt, spec))
        plans.sort(key=lambda x: x[0])   # chronological so deductibles accrue in order

        deductible_used = {}
        remit_batches = {}

        for dos, pt, spec in plans:
            cov = cov_by_patient[pt["patient_id"]]
            payer = self.payer[cov["payer_id"]]
            beh = PAYER_BEHAVIOR[payer["payer_family"]]
            mult = beh["denial_mult"]
            prov = r.choice(rendering[(pt["state"], spec)])
            services = self._services(spec)
            # Medicare wellness code only for Medicare; preventive only for non-Medicare
            services = [(("99396" if payer["payer_family"] != "Medicare" and code == "G0439" else
                          "G0439" if payer["payer_family"] == "Medicare" and code == "99396" else code), u)
                        for code, u in services]
            pos = r.choice(SPECIALTIES[spec]["pos"])
            enc_id = self._id("ENC")
            charge_entry = dos + timedelta(days=r.randint(0, 4))
            self.ds.encounters.append({
                "encounter_id": enc_id, "patient_id": pt["patient_id"], "coverage_id": cov["coverage_id"],
                "rendering_npi": prov["provider_npi"], "billing_npi": prov["billing_npi"],
                "date_of_service": _d(dos), "place_of_service": pos, "charge_entry_date": _d(charge_entry)})

            cpts = [s[0] for s in services]
            dx, dx_mismatch = self._dx_for(cpts, beh["strict_dx"])

            # --- authorization
            auth_number, auth_missing = None, False
            auth_cpt = next((cp for cp in cpts if CPT[cp][2]), None)
            if auth_cpt:
                if r.random() < c.p_missing_auth * mult:
                    auth_missing = True
                else:
                    auth_number = f"AUTH{r.randint(10**7, 10**8 - 1)}"
                    self.ds.authorizations.append({
                        "auth_id": self._id("AU"), "patient_id": pt["patient_id"], "payer_id": payer["payer_id"],
                        "cpt_code": auth_cpt, "auth_number": auth_number, "status": "Approved",
                        "valid_from": _d(dos - timedelta(days=r.randint(3, 20))),
                        "valid_to": _d(dos + timedelta(days=r.randint(30, 90)))})

            # --- lines + modifiers
            lines = []
            for i, (cpt, units) in enumerate(services, start=1):
                charge = CPT[cpt][1] * units
                lines.append({"line_number": i, "cpt_code": cpt, "modifier1": None, "modifier2": None,
                              "units": units, "charge_cents": _cents(charge), "dx_pointer": "1" if len(dx) == 1 else "1,2"})
            bundled_denial = set()
            codes = {ln["cpt_code"]: ln for ln in lines}
            for prim, sec, mod, denied in BUNDLING_PAIRS:
                if prim in codes and sec in codes:
                    if r.random() < c.p_missing_modifier * mult:
                        bundled_denial.add(denied)
                    else:
                        codes[denied]["modifier1"] = mod
            if payer["payer_family"] == "Medicare" and "97110" in codes:
                for ln in lines:
                    if ln["cpt_code"].startswith("97"):
                        ln["modifier2"] = "GP"

            # --- submission timing
            sub_lag = r.randint(0, 6) if r.random() > c.p_late_submission * mult else r.randint(payer["timely_filing_days"] - 20, payer["timely_filing_days"] + 120)
            submission = charge_entry + timedelta(days=sub_lag)
            late = (submission - dos).days > payer["timely_filing_days"]

            claim_level = None
            if late:
                claim_level = ("29", None, "timely_filing", False)
            elif cov["termination_date"] and dos > date.fromisoformat(cov["termination_date"]):
                claim_level = ("27", "N30", "eligibility", True)
            elif r.random() < c.p_missing_info * mult:
                claim_level = ("16", "MA130", "missing_info", True)
                for ln in lines:            # the real defect: service lines not linked to a diagnosis
                    ln["dx_pointer"] = ""

            self._emit_claim(enc_id, pt, cov, payer, beh, prov, dx, lines, auth_number, auth_missing, auth_cpt,
                             dx_mismatch, bundled_denial, claim_level, dos, charge_entry, submission,
                             deductible_used, remit_batches, frequency="1")

            # --- accidental duplicate submission
            if r.random() < c.p_duplicate:
                dup_lines = [dict(ln) for ln in lines]
                self._emit_claim(enc_id, pt, cov, payer, beh, prov, dx, dup_lines, auth_number, auth_missing, auth_cpt,
                                 dx_mismatch, bundled_denial, ("18", None, "duplicate", False), dos, charge_entry,
                                 submission + timedelta(days=r.randint(10, 30)), deductible_used, remit_batches, frequency="1")

        self._flush_remits(remit_batches)

    def _emit_claim(self, enc_id, pt, cov, payer, beh, prov, dx, lines, auth_number, auth_missing, auth_cpt,
                    dx_mismatch, bundled_denial, claim_level, dos, charge_entry, submission,
                    deductible_used, remit_batches, frequency):
        r, c = self.rng, self.cfg
        claim_id = self._id("CLM", 7)
        events = [(charge_entry, "Created", "PM", None, None), (submission, "Submitted", "PM", None, None)]

        # clearinghouse front-end rejection, then corrected resubmission
        if r.random() < c.p_clearinghouse_reject:
            events.append((submission, "Rejected", "Clearinghouse", "A7", "Invalid subscriber ID format"))
            submission = submission + timedelta(days=r.randint(1, 5))
            events.append((submission, "Submitted", "PM", None, "Corrected and resubmitted"))
        events.append((submission, "Accepted", "Clearinghouse", "A1", None))
        received = submission + timedelta(days=r.randint(1, 2))
        events.append((received, "Accepted", "Payer-277", "A2", None))
        adjudication = received + timedelta(days=r.randint(*beh["adjud_days"]))
        # payers pay in a weekly check run on a fixed weekday; the 835 arrives that day
        run_day = sum(map(ord, payer["payer_id"])) % 5
        adjudication = adjudication + timedelta(days=(run_day - adjudication.weekday()) % 7)
        icn = f"{r.randint(10**12, 10**13 - 1)}"

        total_charge = sum(ln["charge_cents"] for ln in lines)
        claim = {
            "claim_id": claim_id, "encounter_id": enc_id, "patient_id": pt["patient_id"], "payer_id": payer["payer_id"],
            "coverage_id": cov["coverage_id"], "billing_npi": prov["billing_npi"], "claim_type": "837P",
            "frequency_code": frequency, "dx1": dx[0], "dx2": dx[1] if len(dx) > 1 else None,
            "dx3": dx[2] if len(dx) > 2 else None, "dx4": None, "prior_auth_number": auth_number,
            "total_charge_cents": total_charge, "submission_date": _d(submission), "payer_received_date": _d(received),
            "adjudication_date": None, "payer_claim_number": icn, "current_status": "Pending",
            "paid_cents": 0, "patient_resp_cents": 0}

        if adjudication > c.as_of:
            # still in process at snapshot
            p1 = received + timedelta(days=7)
            if p1 <= c.as_of:
                events.append((p1, "Pending", "Payer-277", "P1", "In process"))
            for ln in lines:
                ln.update({"claim_id": claim_id, "allowed_cents": 0, "paid_cents": 0, "line_status": "Pending"})
            claim["current_status"] = "Pending" if received <= c.as_of else "Submitted"
            if submission > c.as_of:
                claim["current_status"] = "Created"
                claim["submission_date"] = claim["payer_received_date"] = None
                events = events[:1]
            elif received > c.as_of:
                events = [e for e in events if e[0] <= c.as_of]
                claim["payer_received_date"] = None
            self._store(claim, lines, events)
            return

        claim["adjudication_date"] = _d(adjudication)
        year = dos.year
        ded_key = (cov["coverage_id"], year)
        copay_left = cov["copay_cents"]
        adjustments = []   # (line_number, group, carc, rarc, amount)
        claim_paid = claim_pr = 0
        any_paid = any_denied = False

        for ln in lines:
            ln["claim_id"] = claim_id
            cpt = ln["cpt_code"]
            denial = None
            if claim_level:
                denial = claim_level
            elif auth_missing and cpt == auth_cpt:
                denial = ("197", None, "no_auth", True)
            elif cpt in bundled_denial:
                denial = ("97", "M80", "bundling", True)
            elif dx_mismatch and CPT[cpt][4] and (beh["strict_dx"] or r.random() < 0.5):
                denial = ("50", "N115", "medical_necessity", True)
            elif r.random() < c.p_non_covered:
                denial = ("96", "N130", "non_covered", False)

            if denial:
                carc, rarc, driver, recoverable = denial
                group = "PR" if carc == "96" else "CO"
                adjustments.append((ln["line_number"], group, carc, rarc, ln["charge_cents"]))
                ln.update({"allowed_cents": 0, "paid_cents": 0, "line_status": "Denied"})
                if group == "PR":
                    claim_pr += ln["charge_cents"]
                self.ds.truth_denial_drivers.append({"claim_id": claim_id, "line_number": ln["line_number"],
                                                     "driver": driver, "is_recoverable": int(recoverable)})
                any_denied = True
                continue

            allowed = int(round(ln["charge_cents"] * self._allowed_pct(payer["payer_id"], cpt)))
            if r.random() < c.p_underpayment:
                expected = allowed
                allowed = int(round(expected * r.uniform(0.80, 0.93)))
                self.ds.truth_underpayments.append({"claim_id": claim_id, "line_number": ln["line_number"],
                                                    "expected_allowed_cents": expected, "actual_allowed_cents": allowed})
            contractual = ln["charge_cents"] - allowed
            remaining = allowed
            pr_parts = []
            preventive = cpt in PREVENTIVE
            if copay_left and cpt.startswith("99") and not preventive:
                amt = min(copay_left, remaining)
                pr_parts.append(("3", amt)); remaining -= amt; copay_left = 0
            if ded_key not in deductible_used:   # share already met elsewhere this year
                deductible_used[ded_key] = int(cov["deductible_cents"] * r.choice([0, 0.25, 0.5, 0.75, 1, 1]))
            ded_left = cov["deductible_cents"] - deductible_used[ded_key]
            if ded_left > 0 and remaining > 0 and not preventive:
                amt = min(ded_left, remaining)
                pr_parts.append(("1", amt)); remaining -= amt
                deductible_used[ded_key] += amt
            if cov["coinsurance_pct"] and remaining > 0 and not preventive:
                amt = int(round(remaining * cov["coinsurance_pct"]))
                pr_parts.append(("2", amt)); remaining -= amt
            paid = remaining
            if contractual:
                adjustments.append((ln["line_number"], "CO", "45", None, contractual))
            for carc, amt in pr_parts:
                if amt:
                    adjustments.append((ln["line_number"], "PR", carc, None, amt))
            pr_total = sum(a for _, a in pr_parts)
            ln.update({"allowed_cents": allowed, "paid_cents": paid,
                       "line_status": "Paid"})
            claim_paid += paid
            claim_pr += pr_total
            any_paid = True

        status = "Paid" if any_paid and not any_denied else ("Partially Paid" if any_paid else "Denied")
        claim.update({"current_status": status, "paid_cents": claim_paid, "patient_resp_cents": claim_pr})
        events.append((adjudication, status, "Payer-835", "F1" if any_paid else "F2", None))
        self._store(claim, lines, events)

        key = (payer["payer_id"], prov["billing_npi"], adjudication)
        remit_batches.setdefault(key, []).append((claim, adjustments, lines))

    def _store(self, claim, lines, events):
        self.ds.claims.append(claim)
        self.ds.claim_lines.extend(lines)
        for d, status, source, cat, note in sorted(events, key=lambda e: e[0]):
            self.ds.claim_events.append({"claim_id": claim["claim_id"], "event_date": _d(d), "status": status,
                                         "source": source, "status_category": cat, "note": note})

    def _flush_remits(self, batches):
        for (payer_id, payee, pay_date), items in sorted(batches.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
            remit_id = self._id("ERA", 7)
            total = sum(cl["paid_cents"] for cl, _, _ in items)
            self.ds.remits.append({"remit_id": remit_id, "payer_id": payer_id, "payee_npi": payee,
                                   "payment_method": "ACH" if total > 0 else "NON",
                                   "payment_date": _d(pay_date), "total_paid_cents": total})
            for cl, adjs, lns in items:
                for ln in lns:
                    self.ds.remit_lines.append({
                        "remit_id": remit_id, "claim_id": cl["claim_id"], "line_number": ln["line_number"],
                        "cpt_code": ln["cpt_code"], "modifier1": ln["modifier1"], "modifier2": ln["modifier2"],
                        "units": ln["units"], "charge_cents": ln["charge_cents"], "paid_cents": ln["paid_cents"],
                        "allowed_cents": ln["allowed_cents"] if ln["line_status"] == "Paid" else None})
                self.ds.remit_claims.append({
                    "remit_id": remit_id, "claim_id": cl["claim_id"],
                    "claim_status_code": "4" if cl["current_status"] == "Denied" else "1",
                    "charge_cents": cl["total_charge_cents"], "paid_cents": cl["paid_cents"],
                    "patient_resp_cents": cl["patient_resp_cents"], "payer_claim_number": cl["payer_claim_number"]})
                for line_no, group, carc, rarc, amt in adjs:
                    self.ds.remit_adjustments.append({
                        "remit_id": remit_id, "claim_id": cl["claim_id"], "line_number": line_no,
                        "group_code": group, "carc_code": carc, "rarc_code": rarc, "amount_cents": amt})

    # ------------------------------------------------------------------
    def run(self) -> Dataset:
        self.build_payers()
        self.build_providers()
        self.build_patients()
        self.build_encounters_and_claims()
        self.ds.data_sources = [{"source": "synthetic", "detail": f"seed {self.cfg.seed}, as of {self.cfg.as_of}"},
                                {"source": "authorizations", "detail": f"{len(self.ds.authorizations)} rows"},
                                {"source": "fee_schedule", "detail": f"{len(self.ds.fee_schedule)} rows"}]
        return self.ds


def generate(cfg: Config | None = None) -> Dataset:
    return Generator(cfg).run()

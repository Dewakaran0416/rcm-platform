"""Payer reference list.

ai_call_status mirrors the Payer AI-Call Policy Registry (Nov 2026).
status_channel is the claim-status route the platform should use:
  EDI_276  - send a 276, read the 277 (default for everyone)
  IVR      - payer requires its IVR for status (Medicare MACs)
Voice calls to a live rep are never the default channel.
"""

# (payer_id, name, family, line_of_business, state, timely_filing_days, appeal_limit_days, ai_call_status, status_channel)
PAYERS = [
    # BCBS — one row per plan; extend state by state
    ("BCBSKS", "BCBS Kansas", "BCBS", "Commercial", "KS", 180, 180, "Conditional", "EDI_276"),
    ("BCBSMI", "BCBS Michigan", "BCBS", "Commercial", "MI", 180, 180, "Silent", "EDI_276"),
    ("BCBSTX", "BCBS Texas", "BCBS", "Commercial", "TX", 95, 180, "Not yet checked", "EDI_276"),
    ("CAPBC",  "Capital Blue Cross", "BCBS", "Commercial", "PA", 180, 180, "Silent", "EDI_276"),
    ("ANTHCA", "Anthem Blue Cross (CA)", "BCBS", "Commercial", "CA", 90, 180, "Not yet checked", "EDI_276"),
    # Medicare FFS — MACs
    ("MCR-JL", "Medicare Part B – Novitas JL", "Medicare", "Medicare FFS", "PA", 365, 120, "IVR-required", "IVR"),
    ("MCR-JF", "Medicare Part B – Noridian JF", "Medicare", "Medicare FFS", "AZ", 365, 120, "IVR-required", "IVR"),
    ("MCR-JH", "Medicare Part B – Novitas JH", "Medicare", "Medicare FFS", "TX", 365, 120, "IVR-required", "IVR"),
    ("MCR-JK", "Medicare Part B – NGS JK", "Medicare", "Medicare FFS", "NY", 365, 120, "IVR-required", "IVR"),
    ("MCR-J8", "Medicare Part B – WPS J8", "Medicare", "Medicare FFS", "MI", 365, 120, "IVR-required", "IVR"),
    ("MCR-J5", "Medicare Part B – WPS J5", "Medicare", "Medicare FFS", "KS", 365, 120, "IVR-required", "IVR"),
    ("MCR-JE", "Medicare Part B – Noridian JE", "Medicare", "Medicare FFS", "CA", 365, 120, "IVR-required", "IVR"),
    # Medicaid
    ("MCD-NY", "New York Medicaid (FFS)", "Medicaid", "Medicaid FFS", "NY", 90, 60, "Not yet checked", "EDI_276"),
    ("MCD-TX", "Texas Medicaid (FFS)", "Medicaid", "Medicaid FFS", "TX", 95, 120, "Not yet checked", "EDI_276"),
    ("MCD-PA", "Pennsylvania Medicaid (FFS)", "Medicaid", "Medicaid FFS", "PA", 180, 60, "Not yet checked", "EDI_276"),
    ("MCD-MI", "Michigan Medicaid (FFS)", "Medicaid", "Medicaid FFS", "MI", 365, 60, "Not yet checked", "EDI_276"),
    ("MCD-KS", "Kansas Medicaid (FFS)", "Medicaid", "Medicaid FFS", "KS", 180, 60, "Not yet checked", "EDI_276"),
    ("MCD-AZ", "Arizona Medicaid (FFS)", "Medicaid", "Medicaid FFS", "AZ", 180, 60, "Not yet checked", "EDI_276"),
    ("MCD-CA", "California Medicaid (FFS)", "Medicaid", "Medicaid FFS", "CA", 180, 90, "Not yet checked", "EDI_276"),
    ("FIDNY",  "Fidelis Care", "Medicaid", "Medicaid MCO", "NY", 90, 60, "Prohibited", "EDI_276"),
    # Nationals
    ("AETNA",  "Aetna", "Aetna", "Commercial", None, 90, 180, "Silent", "EDI_276"),
    ("AETMA",  "Aetna Medicare Advantage", "Aetna", "Medicare Advantage", None, 365, 60, "Silent", "EDI_276"),
    ("HUMMA",  "Humana Medicare Advantage", "Humana", "Medicare Advantage", None, 365, 60, "Silent", "EDI_276"),
    ("UHC",    "UnitedHealthcare", "UHC", "Commercial", None, 90, 180, "Silent", "EDI_276"),
    ("UHCMA",  "UnitedHealthcare Medicare Advantage", "UHC", "Medicare Advantage", None, 365, 60, "Silent", "EDI_276"),
    ("UHCCP",  "UnitedHealthcare Community Plan", "UHC", "Medicaid MCO", None, 180, 60, "Not yet checked", "EDI_276"),
    ("UPMC",   "UPMC Health Plan", "UPMC", "Commercial", "PA", 90, 180, "Silent", "EDI_276"),
    # Military
    ("TRIW",   "TRICARE West (TriWest)", "TRICARE", "Military", None, 365, 90, "Restricted", "EDI_276"),
    ("TFL",    "TRICARE For Life (WPS)", "TRICARE", "Military", None, 365, 90, "Silent", "EDI_276"),
]

# Coverage mix by age band: line of business -> (share, {payer family -> weight within that LOB})
LOB_MIX = {
    "under65": {
        "Commercial":   (0.66, {"BCBS": 0.40, "UHC": 0.28, "Aetna": 0.20, "UPMC": 0.12}),
        "Medicaid FFS": (0.12, {"Medicaid": 1.0}),
        "Medicaid MCO": (0.17, {"Medicaid": 0.5, "UHC": 0.5}),
        "Military":     (0.05, {"TRICARE": 1.0}),
    },
    "senior": {
        "Medicare FFS":       (0.48, {"Medicare": 1.0}),
        "Medicare Advantage": (0.47, {"UHC": 0.40, "Humana": 0.35, "Aetna": 0.25}),
        "Military":           (0.05, {"TRICARE": 1.0}),
    },
}

# How each family behaves in the simulation.
#   allowed_pct  - allowed amount as a share of billed charge
#   denial_mult  - multiplier on the base denial probabilities
#   adjud_days   - (min, max) days from receipt to adjudication
#   strict_dx    - extra medical-necessity scrutiny
PAYER_BEHAVIOR = {
    "BCBS":     {"allowed_pct": (0.52, 0.66), "denial_mult": 1.00, "adjud_days": (12, 35), "strict_dx": False},
    "Medicare": {"allowed_pct": (0.36, 0.46), "denial_mult": 0.80, "adjud_days": (14, 20), "strict_dx": True},
    "Medicaid": {"allowed_pct": (0.28, 0.38), "denial_mult": 1.45, "adjud_days": (18, 50), "strict_dx": True},
    "Aetna":    {"allowed_pct": (0.55, 0.68), "denial_mult": 1.10, "adjud_days": (10, 30), "strict_dx": False},
    "Humana":   {"allowed_pct": (0.40, 0.50), "denial_mult": 1.25, "adjud_days": (12, 32), "strict_dx": True},
    "UHC":      {"allowed_pct": (0.54, 0.67), "denial_mult": 1.20, "adjud_days": (10, 35), "strict_dx": False},
    "UPMC":     {"allowed_pct": (0.50, 0.62), "denial_mult": 0.95, "adjud_days": (12, 30), "strict_dx": False},
    "TRICARE":  {"allowed_pct": (0.40, 0.50), "denial_mult": 0.90, "adjud_days": (15, 40), "strict_dx": False},
}

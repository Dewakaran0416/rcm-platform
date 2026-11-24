"""Procedure, diagnosis and adjustment code subsets used by the simulation.

Only code numbers are standard; every description here is a short
project-written summary.
"""

# Specialty -> taxonomy code (NUCC) and the CPTs it bills
SPECIALTIES = {
    "Family Medicine":  {"taxonomy": "207Q00000X", "pos": ["11"], "cpts": ["99202", "99203", "99204", "99212", "99213", "99214", "99215", "99396", "G0439", "36415", "80053", "85025", "83036", "93000"]},
    "Cardiology":       {"taxonomy": "207RC0000X", "pos": ["11", "22"], "cpts": ["99203", "99204", "99213", "99214", "99215", "93000", "93306", "78452"]},
    "Orthopedics":      {"taxonomy": "207X00000X", "pos": ["11", "22", "24"], "cpts": ["99203", "99204", "99213", "99214", "20610", "J3301", "73721", "72148", "29881"]},
    "Radiology":        {"taxonomy": "2085R0202X", "pos": ["22", "11"], "cpts": ["71046", "70553", "72148", "73721"]},
    "Physical Therapy": {"taxonomy": "225100000X", "pos": ["11"], "cpts": ["97161", "97110", "97140", "97530"]},
}

# code: (summary, base charge $, needs_auth, typical units, allowed dx prefixes for medical necessity)
CPT = {
    "99202": ("New patient office visit, straightforward", 135, False, 1, None),
    "99203": ("New patient office visit, low complexity", 190, False, 1, None),
    "99204": ("New patient office visit, moderate complexity", 285, False, 1, None),
    "99212": ("Established patient visit, straightforward", 95, False, 1, None),
    "99213": ("Established patient visit, low complexity", 145, False, 1, None),
    "99214": ("Established patient visit, moderate complexity", 210, False, 1, None),
    "99215": ("Established patient visit, high complexity", 295, False, 1, None),
    "99396": ("Preventive visit, established, age 40-64", 260, False, 1, ["Z00"]),
    "G0439": ("Medicare annual wellness visit, subsequent", 210, False, 1, ["Z00"]),
    "36415": ("Routine blood draw", 25, False, 1, None),
    "80053": ("Comprehensive metabolic panel", 65, False, 1, ["E11", "I10", "E78", "Z00", "R53"]),
    "85025": ("Complete blood count with differential", 45, False, 1, ["R53", "Z00", "D64", "E11"]),
    "83036": ("Hemoglobin A1c", 55, False, 1, ["E11", "R73"]),
    "93000": ("Electrocardiogram with interpretation", 85, False, 1, ["R07", "I10", "I25", "R00", "R06"]),
    "93306": ("Echocardiogram, complete, with Doppler", 1150, True, 1, ["I25", "I50", "R06", "R07", "I10"]),
    "78452": ("Myocardial perfusion imaging, multiple studies", 1650, True, 1, ["I25", "R07", "R06"]),
    "20610": ("Injection/aspiration, major joint", 220, False, 1, ["M17", "M25", "M19"]),
    "J3301": ("Triamcinolone injection, per 10 mg", 18, False, 4, ["M17", "M25", "M19"]),
    "73721": ("MRI lower-extremity joint, without contrast", 1450, True, 1, ["M17", "M25", "S83"]),
    "72148": ("MRI lumbar spine, without contrast", 1500, True, 1, ["M54", "M51"]),
    "70553": ("MRI brain, with and without contrast", 2600, True, 1, ["G43", "R51", "G40"]),
    "71046": ("Chest X-ray, 2 views", 120, False, 1, ["R06", "R07", "J18", "R05"]),
    "29881": ("Knee arthroscopy with meniscectomy", 4200, True, 1, ["S83", "M23"]),
    "97161": ("PT evaluation, low complexity", 165, False, 1, ["M54", "M25", "M17", "S83"]),
    "97110": ("Therapeutic exercise, each 15 min", 55, False, 3, ["M54", "M25", "M17", "S83"]),
    "97140": ("Manual therapy, each 15 min", 52, False, 2, ["M54", "M25", "M17", "S83"]),
    "97530": ("Therapeutic activities, each 15 min", 58, False, 2, ["M54", "M25", "M17", "S83"]),
}

def cpt_info(code: str):
    """CPT tuple for known codes; a neutral default for anything outside the built-in list
    (no auth requirement, no diagnosis rule), so real data with other codes never breaks a module."""
    return CPT.get(code, (code, 0, False, 1, None))


# Pairs billed together on one claim that need a modifier, or the column-2 code is bundled (CO-97)
#   (primary, secondary, modifier that unbundles it, which code gets denied)
BUNDLING_PAIRS = [
    ("20610", "99213", "25", "99213"),
    ("20610", "99214", "25", "99214"),
    ("20610", "99203", "25", "99203"),
    ("97530", "97140", "59", "97140"),
]

ICD10 = {
    "E11.9":   "Type 2 diabetes without complications",
    "I10":     "Essential hypertension",
    "E78.5":   "Hyperlipidemia, unspecified",
    "R53.83":  "Other fatigue",
    "R73.03":  "Prediabetes",
    "Z00.00":  "General adult exam without abnormal findings",
    "R07.9":   "Chest pain, unspecified",
    "R06.02":  "Shortness of breath",
    "R00.2":   "Palpitations",
    "I25.10":  "Coronary artery disease, native vessel",
    "I50.9":   "Heart failure, unspecified",
    "M17.11":  "Primary osteoarthritis, right knee",
    "M25.561": "Pain in right knee",
    "S83.241A":"Medial meniscus tear, right knee, initial",
    "M54.50":  "Low back pain, unspecified",
    "M54.16":  "Radiculopathy, lumbar region",
    "M51.26":  "Lumbar disc displacement",
    "G43.909": "Migraine, unspecified",
    "R51.9":   "Headache, unspecified",
    "J06.9":   "Acute upper respiratory infection",
    "R05.9":   "Cough, unspecified",
    "J18.9":   "Pneumonia, unspecified organism",
    "D64.9":   "Anemia, unspecified",
}

POS = {"11": "Office", "22": "Outpatient hospital", "24": "Ambulatory surgical center"}

# Claim Adjustment Reason Codes used by the simulation (summaries)
CARC = {
    "1":   "Deductible amount",
    "2":   "Coinsurance amount",
    "3":   "Copay amount",
    "16":  "Claim lacks information needed for adjudication",
    "18":  "Exact duplicate claim/service",
    "27":  "Expenses incurred after coverage terminated",
    "29":  "Time limit for filing has expired",
    "45":  "Charge exceeds fee schedule / contracted amount",
    "50":  "Not deemed medically necessary by the payer",
    "96":  "Non-covered charge",
    "97":  "Payment included in another service already adjudicated",
    "197": "Precertification/authorization absent",
    "4":   "Procedure code inconsistent with modifier, or modifier missing",
    "11":  "Diagnosis inconsistent with procedure",
    "15":  "Authorization number missing or invalid",
    "22":  "May be covered by another payer (coordination of benefits)",
    "26":  "Expenses incurred before coverage",
    "31":  "Patient cannot be identified as our insured",
    "109": "Not covered by this payer; send to the correct payer",
    "119": "Benefit maximum reached",
    "151": "Payment adjusted: services not supported by frequency/number",
    "167": "Diagnosis not covered",
    "204": "Service not covered under the patient's current benefit plan",
    "252": "Additional documentation needed",
    "253": "Sequestration reduction",
}

RARC = {
    "MA130": "Claim has missing/invalid information; resubmit corrected claim",
    "N130":  "Consult plan benefit documents for coverage",
    "M80":   "Not covered when performed with another service on the same day",
    "N115":  "Decision based on a local coverage determination",
    "N30":   "Patient ineligible for this service",
}

# X12 277 claim status category codes (summaries)
STATUS_CATEGORY = {
    "A1": "Acknowledged – received",
    "A2": "Accepted into adjudication",
    "A3": "Returned as unprocessable",
    "A7": "Rejected – invalid information",
    "P1": "Pending – in process",
    "F1": "Finalized – payment",
    "F2": "Finalized – denial",
}

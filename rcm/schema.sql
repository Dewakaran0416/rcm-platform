-- RCM Platform — shared data model (Foundation module)
-- Every later module reads and writes these tables.
-- Money is stored as INTEGER cents to avoid float rounding; exports convert to dollars.
-- All data produced by the generator is SYNTHETIC. No PHI.

PRAGMA foreign_keys = ON;

-- ---------- Reference ----------
CREATE TABLE payers (
    payer_id            TEXT PRIMARY KEY,
    payer_name          TEXT NOT NULL,
    payer_family        TEXT NOT NULL,      -- BCBS, Medicare, Medicaid, Aetna, Humana, UHC, UPMC, TRICARE
    line_of_business    TEXT NOT NULL,      -- Commercial, Medicare FFS, Medicare Advantage, Medicaid FFS, Medicaid MCO, Military
    state               TEXT,               -- NULL = national
    timely_filing_days  INTEGER NOT NULL,
    appeal_limit_days   INTEGER NOT NULL,
    ai_call_status      TEXT NOT NULL,      -- from the Payer AI-Call Policy Registry
    status_channel      TEXT NOT NULL       -- preferred claim-status route: EDI_276, PORTAL, IVR
);

CREATE TABLE providers (
    provider_npi        TEXT PRIMARY KEY,   -- synthetic, Luhn-valid NPI
    provider_name       TEXT NOT NULL,
    provider_type       TEXT NOT NULL,      -- Billing (group) / Rendering (individual)
    specialty           TEXT NOT NULL,
    taxonomy_code       TEXT NOT NULL,
    tax_id              TEXT NOT NULL,      -- synthetic
    state               TEXT NOT NULL,
    billing_npi         TEXT REFERENCES providers(provider_npi)  -- rendering -> group
);

-- ---------- Patient access ----------
CREATE TABLE patients (
    patient_id          TEXT PRIMARY KEY,
    mrn                 TEXT NOT NULL UNIQUE,
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    dob                 TEXT NOT NULL,
    sex                 TEXT NOT NULL,
    state               TEXT NOT NULL,
    zip                 TEXT NOT NULL
);

CREATE TABLE coverages (
    coverage_id         TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    payer_id            TEXT NOT NULL REFERENCES payers(payer_id),
    member_id           TEXT NOT NULL,
    group_number        TEXT,
    coverage_rank       INTEGER NOT NULL,   -- 1 primary, 2 secondary
    effective_date      TEXT NOT NULL,
    termination_date    TEXT,               -- NULL = active
    copay_cents         INTEGER NOT NULL,
    deductible_cents    INTEGER NOT NULL,
    coinsurance_pct     REAL NOT NULL
);

CREATE TABLE authorizations (
    auth_id             TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    payer_id            TEXT NOT NULL REFERENCES payers(payer_id),
    cpt_code            TEXT NOT NULL,
    auth_number         TEXT NOT NULL,
    status              TEXT NOT NULL,      -- Approved / Denied / Pending
    valid_from          TEXT NOT NULL,
    valid_to            TEXT NOT NULL
);

-- ---------- Charge capture ----------
CREATE TABLE encounters (
    encounter_id        TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    coverage_id         TEXT NOT NULL REFERENCES coverages(coverage_id),
    rendering_npi       TEXT NOT NULL REFERENCES providers(provider_npi),
    billing_npi         TEXT NOT NULL REFERENCES providers(provider_npi),
    date_of_service     TEXT NOT NULL,
    place_of_service    TEXT NOT NULL,
    charge_entry_date   TEXT NOT NULL
);

-- ---------- Claims (837) ----------
CREATE TABLE claims (
    claim_id            TEXT PRIMARY KEY,   -- patient control number (CLM01)
    encounter_id        TEXT NOT NULL REFERENCES encounters(encounter_id),
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    payer_id            TEXT NOT NULL REFERENCES payers(payer_id),
    coverage_id         TEXT NOT NULL REFERENCES coverages(coverage_id),
    billing_npi         TEXT NOT NULL REFERENCES providers(provider_npi),
    claim_type          TEXT NOT NULL,      -- 837P
    frequency_code      TEXT NOT NULL,      -- 1 original, 7 replacement, 8 void
    dx1 TEXT NOT NULL, dx2 TEXT, dx3 TEXT, dx4 TEXT,
    prior_auth_number   TEXT,
    total_charge_cents  INTEGER NOT NULL,
    submission_date     TEXT,
    payer_received_date TEXT,
    adjudication_date   TEXT,
    payer_claim_number  TEXT,               -- ICN / DCN
    current_status      TEXT NOT NULL,      -- see claim_events.status
    paid_cents          INTEGER NOT NULL DEFAULT 0,
    patient_resp_cents  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE claim_lines (
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    line_number         INTEGER NOT NULL,
    cpt_code            TEXT NOT NULL,
    modifier1           TEXT,
    modifier2           TEXT,
    units               INTEGER NOT NULL,
    charge_cents        INTEGER NOT NULL,
    dx_pointer          TEXT NOT NULL,      -- e.g. "1,2"
    allowed_cents       INTEGER NOT NULL DEFAULT 0,
    paid_cents          INTEGER NOT NULL DEFAULT 0,
    line_status         TEXT NOT NULL,      -- Paid / Denied / Pending / Partial
    PRIMARY KEY (claim_id, line_number)
);

-- Status history: one row per lifecycle step (feeds claim-status + aging)
CREATE TABLE claim_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    event_date          TEXT NOT NULL,
    status              TEXT NOT NULL,      -- Created, Submitted, Rejected (clearinghouse), Accepted, Pending, Paid, Partially Paid, Denied
    source              TEXT NOT NULL,      -- PM, Clearinghouse, Payer-277, Payer-835
    status_category     TEXT,               -- X12 277 category code (A1, A2, F1, F2, P1, A7 ...)
    note                TEXT
);

-- ---------- Remittance (835) ----------
CREATE TABLE remits (
    remit_id            TEXT PRIMARY KEY,   -- one ERA / payment (TRN02)
    payer_id            TEXT NOT NULL REFERENCES payers(payer_id),
    payee_npi           TEXT NOT NULL REFERENCES providers(provider_npi),
    payment_method      TEXT NOT NULL,      -- ACH / CHK / NON (zero-pay)
    payment_date        TEXT NOT NULL,
    total_paid_cents    INTEGER NOT NULL
);

CREATE TABLE remit_claims (
    remit_id            TEXT NOT NULL REFERENCES remits(remit_id),
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    claim_status_code   TEXT NOT NULL,      -- CLP02: 1 primary paid, 2 secondary, 4 denied, 22 reversal
    charge_cents        INTEGER NOT NULL,
    paid_cents          INTEGER NOT NULL,
    patient_resp_cents  INTEGER NOT NULL,
    payer_claim_number  TEXT NOT NULL,
    PRIMARY KEY (remit_id, claim_id)
);

-- SVC: each payment's own result per line (a claim can be paid more than once: denied, then paid on appeal)
CREATE TABLE remit_lines (
    remit_id            TEXT NOT NULL REFERENCES remits(remit_id),
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    line_number         INTEGER NOT NULL,
    cpt_code            TEXT NOT NULL,
    modifier1           TEXT,
    modifier2           TEXT,
    units               INTEGER NOT NULL,
    charge_cents        INTEGER NOT NULL,
    paid_cents          INTEGER NOT NULL,
    allowed_cents       INTEGER,            -- AMT*B6 when the payer sends it
    PRIMARY KEY (remit_id, claim_id, line_number)
);

-- CAS segments at line level. charge = allowed + CO/OA/PI adj; allowed = paid + PR adj
CREATE TABLE remit_adjustments (
    adjustment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    remit_id            TEXT NOT NULL REFERENCES remits(remit_id),
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    line_number         INTEGER NOT NULL,
    group_code          TEXT NOT NULL,      -- CO, PR, OA, PI
    carc_code           TEXT NOT NULL,
    rarc_code           TEXT,
    amount_cents        INTEGER NOT NULL
);

-- ---------- Ground truth (generator only) ----------
-- Why a line was denied. Kept OUT of the operational tables so the
-- denial-prediction module can't leak the answer into its features.
CREATE TABLE truth_denial_drivers (
    claim_id            TEXT NOT NULL REFERENCES claims(claim_id),
    line_number         INTEGER NOT NULL,
    driver              TEXT NOT NULL,      -- eligibility, no_auth, timely_filing, bundling, medical_necessity, duplicate, missing_info, non_covered
    is_recoverable      INTEGER NOT NULL,   -- 1 if a correction/appeal would realistically recover it
    PRIMARY KEY (claim_id, line_number)
);

-- Contracted rates: a fixed allowed amount per unit (allowed_cents), or a share of billed charge (allowed_pct)
CREATE TABLE fee_schedule (
    payer_id            TEXT NOT NULL REFERENCES payers(payer_id),
    cpt_code            TEXT NOT NULL,
    allowed_pct         REAL,
    allowed_cents       INTEGER,
    PRIMARY KEY (payer_id, cpt_code)
);

-- Lines the payer priced below contract (generator only; posting must find these on its own)
CREATE TABLE truth_underpayments (
    claim_id               TEXT NOT NULL REFERENCES claims(claim_id),
    line_number            INTEGER NOT NULL,
    expected_allowed_cents INTEGER NOT NULL,
    actual_allowed_cents   INTEGER NOT NULL,
    PRIMARY KEY (claim_id, line_number)
);

-- Where the data came from and which optional tables were supplied
CREATE TABLE data_sources (
    source  TEXT PRIMARY KEY,   -- synthetic / csv / 837 / 835 / authorizations / fee_schedule / coverages / deidentified
    detail  TEXT
);

CREATE INDEX ix_claims_payer   ON claims(payer_id);
CREATE INDEX ix_claims_status  ON claims(current_status);
CREATE INDEX ix_events_claim   ON claim_events(claim_id);
CREATE INDEX ix_adj_claim      ON remit_adjustments(claim_id, line_number);
CREATE INDEX ix_remit_claims_claim ON remit_claims(claim_id);

-- The adjustments from each claim's most recent payment (what the claim's current state rests on)
CREATE VIEW latest_remit_adjustments AS
SELECT a.* FROM remit_adjustments a
JOIN remits r ON r.remit_id = a.remit_id
WHERE r.payment_date || '|' || r.remit_id = (
    SELECT MAX(r2.payment_date || '|' || r2.remit_id) FROM remit_claims rc2 JOIN remits r2 ON r2.remit_id = rc2.remit_id
    WHERE rc2.claim_id = a.claim_id);

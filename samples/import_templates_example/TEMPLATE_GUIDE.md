# Import templates

Fill one CSV (or .xlsx with the same name) per table. Required: `patients`, `claims`, `claim_lines`. The rest are optional.

Dates: YYYY-MM-DD, MM/DD/YYYY, M/D/YY or YYYYMMDD. Money: 1234.5, $1,234.50.
Column names are matched loosely (case, spaces, common export names like DOS, CPT, Billed Amount).

Then: `python -m rcm ingest --csv <this folder> --x835 <folder of 835s> --out data --deidentify`

## patients.csv  (required)

| Column | Required | Type | Notes |
|---|---|---|---|
| patient_id | yes | text | Your patient/account identifier (any unique value) |
| first_name |  | text | Removed if you de-identify |
| last_name |  | text | Removed if you de-identify |
| dob | yes | date | Date of birth |
| sex |  | text | M / F / U |
| state |  | text | 2-letter state |
| zip |  | text | 5-digit ZIP |
| mrn |  | text | Medical record number |

## claims.csv  (required)

| Column | Required | Type | Notes |
|---|---|---|---|
| claim_id | yes | text | Patient control number / claim number you sent (CLM01) |
| patient_id | yes | text | Must exist in patients |
| payer_id | yes | text | Payer ID (as in payers, or your clearinghouse payer ID) |
| payer_name |  | text | Used to create the payer if payers.csv is not supplied |
| member_id |  | text | Subscriber/member ID |
| billing_npi | yes | npi | Billing provider NPI |
| rendering_npi |  | npi | Rendering provider NPI (defaults to billing NPI) |
| date_of_service | yes | date | Claim date of service (first DOS if a range) |
| place_of_service |  | text | 2-digit POS code (default 11) |
| frequency_code |  | text | 1 original, 7 replacement, 8 void (default 1) |
| dx1 | yes | text | Primary ICD-10 (with or without the dot) |
| dx2 |  | text |  |
| dx3 |  | text |  |
| dx4 |  | text |  |
| prior_auth_number |  | text | Auth number sent on the claim (REF*G1) |
| submission_date |  | date | Date sent to the payer; blank = not yet billed |
| payer_claim_number |  | text | Payer ICN/DCN if known |

## claim_lines.csv  (required)

| Column | Required | Type | Notes |
|---|---|---|---|
| claim_id | yes | text | Must exist in claims |
| line_number | yes | int | 1, 2, 3 ... |
| cpt_code | yes | text | CPT/HCPCS |
| modifier1 |  | text |  |
| modifier2 |  | text |  |
| units |  | int | Default 1 |
| charge | yes | money | Billed amount for the line |
| dx_pointer |  | text | Which dx support the line, e.g. 1 or 1,2 (default 1) |

## payers.csv  (optional)

| Column | Required | Type | Notes |
|---|---|---|---|
| payer_id | yes | text |  |
| payer_name | yes | text |  |
| line_of_business |  | text | Commercial / Medicare FFS / Medicare Advantage / Medicaid FFS / Medicaid MCO / Military |
| state |  | text | Blank = national |
| timely_filing_days |  | int | Filing limit in days (default 90, or from the built-in payer list) |
| appeal_limit_days |  | int | Appeal limit in days (default 90) |
| ai_call_status |  | text | Prohibited / Restricted / Conditional / IVR-required / Silent / Allowed / Not yet checked |

## providers.csv  (optional)

| Column | Required | Type | Notes |
|---|---|---|---|
| npi | yes | npi |  |
| name | yes | text |  |
| provider_type |  | text | Billing / Rendering |
| specialty |  | text |  |
| taxonomy_code |  | text |  |
| tax_id |  | text |  |
| state |  | text |  |
| billing_npi |  | npi | For rendering providers: their group NPI |

## coverages.csv  (optional)

| Column | Required | Type | Notes |
|---|---|---|---|
| patient_id | yes | text |  |
| payer_id | yes | text |  |
| member_id | yes | text |  |
| group_number |  | text |  |
| coverage_rank |  | int | 1 primary, 2 secondary (default 1) |
| effective_date |  | date |  |
| termination_date |  | date | Blank = active; drives the eligibility edit |
| copay |  | money |  |
| deductible |  | money |  |
| coinsurance_pct |  | pct | 0.2 or 20% |

## authorizations.csv  (optional)

| Column | Required | Type | Notes |
|---|---|---|---|
| patient_id | yes | text |  |
| payer_id | yes | text |  |
| cpt_code | yes | text |  |
| auth_number | yes | text |  |
| status |  | text | Approved / Denied / Pending (default Approved) |
| valid_from | yes | date |  |
| valid_to | yes | date |  |

## fee_schedule.csv  (optional)

| Column | Required | Type | Notes |
|---|---|---|---|
| payer_id | yes | text |  |
| cpt_code | yes | text |  |
| allowed_amount |  | money | Contracted allowed per unit (use this OR allowed_pct) |
| allowed_pct |  | pct | Allowed as a share of billed charge |

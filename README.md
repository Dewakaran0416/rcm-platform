# RCM Platform

![tests](https://github.com/YOUR-USERNAME/rcm-platform/actions/workflows/tests.yml/badge.svg)

An end-to-end revenue cycle management platform, built module by module on one shared data model: claim creation, scrubbing, 837 submission, 835 posting, denial prediction, denial recovery, claim-status follow-up, and analytics. It runs on generated data out of the box, and on a company's own data through the ingestion module (templates or 837s, plus 835s, with built-in de-identification).

**Out of the box everything runs on synthetic data.** No PHI, no client data, no payer contract terms. Payer names are real so the reference data reads true. Filing and appeal limits are simulation defaults; verify them against the payer TFL tracker before real use.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt        # scikit-learn (Module 3), openpyxl (Excel import), pytest; the rest is standard library

python -m rcm pipeline --out data      # every module, ~20 seconds
start data\dashboard.html              # Windows   (Mac: open data/dashboard.html)
python -m pytest -q                    # 47 tests
```

Run each command on its own line. Python 3.10–3.13 is tested on Windows and Linux. `pip install -e .` also gives you a plain `rcm` command (`rcm pipeline`, `rcm ingest` ...).

| Option | Meaning |
|---|---|
| `--seed`, `--patients`, `--encounters` | dataset size and randomness |
| `--as-of YYYY-MM-DD` | snapshot date ("today" for A/R, deadlines, follow-up) |
| `--no-model` | skip Module 3 if scikit-learn isn't installed |
| `--ai-vendor-registered BCBSKS ...` | payers where your AI vendor and BAA are on file (unlocks AI voice for "Conditional" payers) |
| `--no-csv` | skip CSV exports |

`python -m rcm generate` builds the data only. `python -m rcm validate data/rcm.sqlite` re-runs the integrity checks.

## What comes out

```
data/
  dashboard.html          RCM Command Center: offline, one file, no scripts, no network calls
  report.json             every KPI and module metric from the run
  rcm.sqlite              the full data model plus every module's output tables
  csv/                    every table as Excel-ready CSV, including the work queues
  x12/837/                837P files, one per payer (claims with HARD edits held back; on loaded data: unbilled claims only, named _REVIEW)
  x12/835/                835 files, one per payer
  x12/status/             276 requests and 277 responses
  appeals/                drafted appeal letters for the top recovery items
```

`samples/` holds a dashboard, report and X12 snippets from the default run, so you can see the output without running anything.

## Load a company's data

```bash
python -m rcm templates --out templates              # blank templates + TEMPLATE_GUIDE.md  (--example for a filled sample)
# fill them from the billing system export (CSV or .xlsx), then:
python -m rcm ingest --csv templates --x835 era_files --out client --deidentify --run
# or, straight from claim files:
python -m rcm ingest --x837 claim_files --x835 era_files --out client --deidentify --run
```

`--run` runs Modules 1–6 straight after the load. Later you can re-run them with `python -m rcm pipeline --db client/rcm.sqlite` (add `--x277 folder` once payer 277 responses come back).

**What you can load**

| Source | Required | Notes |
|---|---|---|
| Templates: `patients`, `claims`, `claim_lines` | yes (or 837s) | CSV or .xlsx. Headers are matched loosely: `DOS`, `CPT`, `Billed Amount`, `Account Number`, `Payor ID` ... |
| Templates: `payers`, `providers`, `coverages`, `authorizations`, `fee_schedule` | no | Missing payers/providers/coverage are derived from the claims. Payer filing limits and AI-call policy come from the built-in payer list when the name matches; otherwise defaults are used and flagged |
| 837P files | instead of templates | Any vendor's separators; dependent-patient loops; corrected claims keep their latest version |
| 835 files | no, but needed for anything after submission | Applied in date order: line matching by REF*6R or CPT, claim-level-only denials spread across lines, payment history kept (denied, then paid on appeal), reversals flagged |

**What happens to bad data:** nothing stops the load. Each problem goes to `ingest_issues.csv` with file, row, column and reason. Any claim with an error is left out whole, so what loads is internally consistent. Patient values (names, DOB, member IDs) are never echoed into the report.

**What switches off when an input is missing:** with no `authorizations` file, the authorization edit and the model's auth feature turn off rather than flag every MRI. With no `fee_schedule`, underpayment detection is skipped and net collection rate falls back to charges less contractual adjustments. On real data, claim status writes 276s and waits for the payer's 277s instead of simulating them.

**De-identification (`--deidentify`):** patient, claim, encounter, coverage and auth IDs, MRNs, member/group IDs and payer claim numbers become keyed hashes (HMAC-SHA256). Names are removed, DOB becomes the birth year (90+ grouped), ZIP becomes 3 digits, and the file is compacted so no old values linger. The same key gives the same hashes, so later loads link up. Keep the key separate from the data. Service and payment dates, codes and amounts stay, because the revenue cycle math needs them. **That means this is not HIPAA Safe Harbor on its own.** Run it under a BAA, or have the output reviewed under Expert Determination, before data leaves the client. With `--deidentify`, the issue report (which names original claim numbers so staff can fix them) is written to a separate file marked INTERNAL and kept out of the dataset.

**837s from loaded data are for review, not submission.** The data model keeps state and ZIP but not street address or city, so the 837 writer fills those with a visible placeholder and names the files `_REVIEW`. Send production claims from the practice management system, after fixing what the scrubber flags.

**Scale:** about 50,000 claims (85,000 lines) load, de-identify and run through all six modules in under two minutes, using under 600 MB of memory, on an ordinary laptop.

**How the load is tested:** synthetic data → templates → load → the claims, lines and every payment adjustment match the source exactly. The same holds through 837 files. Modules 1–6 give identical results on the loaded, de-identified copy. The only expected differences: claims show "Submitted" rather than "Pending" (no 277 history), and clean claim rate reads higher (billing exports carry no clearinghouse rejections).

## Results on the default run (seed 42, 12,000 encounters)

| Module | What it does | Result |
|---|---|---|
| 0 · Foundation | Synthetic claim lifecycle with denials that have real causes | 12,143 claims, 11.4% initial denial rate, 15 integrity checks pass |
| 1 · Scrubber + 837P | 7 pre-submission edits; writes 837P 5010 | Catches **91.6%** of denied lines before submission at 81% precision; **$676K** fixable before sending |
| 2 · 835 + posting | Writes 835s, parses them back, posts, checks balance, finds underpayments | 0 exceptions; posted totals match source to the cent; underpayments found at **99% recall / 100% precision** |
| 3 · Denial prediction | Gradient boosting on submission-time features; trained on the earlier 75% of service dates, tested on the latest 25% | **AUC 0.94**; the top 10% riskiest claims are **98% precise**, against 79% for the rules alone |
| 4 · Denial recovery | Root cause, action, owner team, deadline, priority; drafts appeals | 846 open items, **$188K expected recovery**; top 20% of the queue holds 79% of recoverable dollars |
| 5 · Claim status | 276/277 first; escalates only through channels the payer allows | **0** AI calls to payers that prohibit or restrict them |
| 6 · Analytics | KPIs + dashboard | NCR 86.9%, clean claim rate 85.2%, 49.8 days in A/R |

Across seeds and sizes (1K–50K encounters), every X12 file passes the envelope checks, posting always reconciles, and no AI call is ever routed to a payer that forbids it.

### Read these numbers honestly

The generator and the scrubber encode the same payer rules, so on this data the scrubber catches nearly everything it was designed to catch. That proves the edits are wired correctly. It does not tell you what share of real-world denials they'd prevent. The same applies to the recovery module's recoverability calls. The test that matters is running Modules 1–4 on a client's real (de-identified) 837/835 history, and the parsers are built to read other systems' files for exactly that reason.

## The modules

### 0 · Foundation (`generator.py`, `schema.sql`, `validate.py`)
Payers, providers (Luhn-valid synthetic NPIs), patients, coverage, authorizations, encounters, claims, lines, status events, remits, CAS adjustments and a contract fee schedule. Money is stored as integer cents. Denials are caused, not sprinkled:

| Driver | CARC | Cause |
|---|---|---|
| Eligibility | 27 | DOS after coverage end |
| No authorization | 197 | MRI / echo / nuclear / arthroscopy with no auth on file |
| Timely filing | 29 | Sent after the payer's limit |
| Bundling | 97 | 20610 + E/M without -25, 97530 + 97140 without -59 |
| Medical necessity | 50 | Pointed dx doesn't support the CPT (stricter at Medicare, Medicaid, Humana) |
| Duplicate | 18 | Same encounter sent twice |
| Missing info | 16 | Lines with no diagnosis pointer |
| Non-covered | PR-96 | Benefit exclusion (not knowable before submission) |

Ground truth sits in `truth_*` tables that no module reads; only the `evaluate()` functions read them.

### 1 · Scrubber + 837P (`scrubber.py`, `x12/x837.py`)
Edits: `ELIG`, `AUTH`, `BUNDLE`, `DXNEC` (warn), `TFL`, `DUP`, `INFO`. Each edit has a message and a fix. Claims with HARD edits are held out of the 837. The 837P covers the billing provider, subscriber, payer, CLM, REF*G1, HI, the rendering provider, and SV1/DTP per line.

### 2 · Payment posting (`x12/x835.py`, `posting.py`)
The parser takes its separators from the ISA header, so it reads other systems' 835s too. Posting matches each CLP to its claim, checks that every line balances, splits each line into paid, contractual, patient responsibility and denied, and compares allowed to contract. Variances of $1 or more *and* 2% or more go to `underpayments`, and denied lines go to `denial_queue`.

### 3 · Denial prediction (`predict.py`)
Features use only what's known before sending: payer, LOB, specialty, POS, CPTs, charge, age, coverage dates, auth on file, bundling modifiers, dx–CPT support, filing lag, duplicate check, and payer×CPT denial history from earlier months only. Each claim gets a risk score, a band, and plain-language reasons. Permutation importance shows which signals matter. The most common 150 payers and 300 CPTs get their own features and the rest are pooled, so memory stays bounded on real data. The model skips itself, with a reason, when there's too little history to train on (under 500 claims or 25 denials).

### 4 · Denial recovery (`recovery.py`)
CARC → root cause → action (corrected claim, appeal, retro-auth, rebill, bill patient, close) → owner team. The deadline is the appeal limit from the 835 date, or the filing limit for corrected claims. Priority = denied $ × chance of recovery × urgency. Expired items are marked expired, not hidden. Appeal drafts have placeholders where clinical detail must be added.

### 5 · Claim status (`claim_status.py`, `x12/x276.py`)
Follows up on claims with no 835 after 30 days, and on unbilled claims near or past the filing limit. Sends 276s and reads the 277s back. Here a payer simulator answers; in production a clearinghouse would. When EDI can't settle a claim (no record, or still pending after 45 days), routing follows the Payer AI-Call Policy Registry:

| Payer policy | Channel |
|---|---|
| Prohibited | Portal / staff call |
| Restricted | Payer IVR self-service only |
| IVR-required (Medicare MACs) | IVR bot (NPI / PTAN / TIN) |
| Conditional | Staff call, or AI voice once the vendor + BAA are registered |
| Silent / Not yet checked | IVR self-service only until the payer confirms |
| Allowed | AI voice agent |

AI voice scripts open with a required AI disclosure and an offer to transfer to a person. The telephony itself is not built; this module produces the routed worklist and the scripts.

### 6 · Analytics (`analytics.py`, `dashboard.py`)
Net collection rate (collected ÷ contracted allowed), gross collection rate, initial denial rate, clean claim rate, days in A/R, A/R aging, days to pay, denials by payer and reason, plus each module's results. The dashboard is a single offline HTML file that reads in light and dark mode and on a phone.

## Layout

```
rcm/
  schema.sql  generator.py  store.py  validate.py  db.py      Foundation
  scrubber.py                                                 Module 1
  posting.py                                                  Module 2
  predict.py                                                  Module 3
  recovery.py                                                 Module 4
  claim_status.py                                             Module 5
  analytics.py  dashboard.py                                  Module 6
  pipeline.py  cli.py                                         python -m rcm ...
  x12/  __init__.py (envelopes, reader, checks)  x837.py (writer + reader)  x835.py  x276.py
  ingest/  templates.py  loader.py  payers.py  remits.py  deidentify.py  export.py
  reference/  payers.py  codes.py  carc_map.py
tests/  test_foundation.py  test_modules.py  test_ingest.py
```

## Not built yet

- **Real-time service:** a FastAPI layer so a PM system can score and scrub one claim at a time
- **Voice agent:** telephony and speech for IVR/AI calls (the routing and scripts are ready)
- **Secondary claims / COB, institutional 837I, patient statements**
- **Expanding the rule set from real data:** the built-in CPT/diagnosis rules cover a demo subset; codes outside it load fine but get no auth/medical-necessity edits until rules are added

## License

Source-available under the **[PolyForm Noncommercial License 1.0.0](LICENSE)**. You may read, run, modify and share it for any noncommercial purpose: personal study, research, evaluation, teaching, and use by charities, schools, public research and government bodies. **Commercial use** (using it in a business, offering it as a service, selling it, or building it into a product) **needs a separate commercial license from the author**; open an issue or message on LinkedIn. This is not an OSI "open source" license.

## Disclaimer

- All bundled data is synthetic. Do not commit real patient or claim data to this repository; `.gitignore` blocks the common data file types as a safety net.
- Payer names are used only to make the reference data recognizable. They are trademarks of their owners, and this project is not affiliated with or endorsed by any payer. Payer policies, filing limits and AI-call statuses change; verify them before relying on them.
- CPT® is a registered trademark of the American Medical Association. The project includes a small set of code numbers with short descriptions written for this project, not AMA descriptors. CARC/RARC descriptions are likewise project-written summaries.
- This is software, not legal, compliance or billing advice. The de-identification step is not by itself a HIPAA de-identification determination.

## About

Built by Dewakaran: 10+ years in US healthcare revenue cycle operations (denials, AR follow-up, payer analytics), now building data and ML tools for it.

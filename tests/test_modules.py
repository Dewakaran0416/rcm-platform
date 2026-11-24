"""Modules 1-6 tests, on one small end-to-end pipeline run. Run: python -m pytest -q"""
import json

import pytest

from rcm import Config
from rcm.db import connect
from rcm.pipeline import run
from rcm.x12 import read_segments, structure_errors, x835, x276

CFG = Config(seed=5, n_patients=900, n_encounters=4500)


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    folder = tmp_path_factory.mktemp("pipe")
    report = run(folder, CFG, csv=True, model=True, log=lambda *_: None)
    return folder, report, connect(folder / "rcm.sqlite")


# ---------- Module 1: scrubber + 837
def test_scrubber_catches_most_preventable_denials(out):
    s = out[1]["modules"]["scrubber"]
    assert s["recall"] > 0.8 and s["precision"] > 0.6
    assert s["by_driver"]["non_covered"]["caught"] == 0      # not knowable before submission


def test_scrubber_never_reads_ground_truth():
    import inspect
    from rcm import scrubber
    src = inspect.getsource(scrubber.scrub)          # the edits themselves; scoring helpers may read the 835
    assert "truth_" not in src and "remit" not in src and "line_status" not in src


def test_837_files_are_well_formed(out):
    files = list((out[0] / "x12" / "837").glob("*.x12"))
    assert files
    for f in files:
        text = f.read_text(encoding="ascii")
        assert structure_errors(text) == []
        segs = read_segments(text)
        # CLM total equals the sum of its SV1 lines
        total, running = None, 0
        for s in segs + [["CLM", "", "0"]]:
            if s[0] in ("CLM", "SE"):
                if total is not None:
                    assert abs(total - running) < 0.005
                total, running = (float(s[2]), 0) if s[0] == "CLM" and s[1] else (None, 0)
            elif s[0] == "SV1":
                running += float(s[2])


def test_claims_with_hard_edits_are_held_back(out):
    con = out[2]
    held = {r[0] for r in con.execute("SELECT DISTINCT claim_id FROM scrub_edits WHERE severity='HARD'")}
    sent = set()
    for f in (out[0] / "x12" / "837").glob("*.x12"):
        sent |= {s[1] for s in read_segments(f.read_text(encoding="ascii")) if s[0] == "CLM"}
    assert held and not (held & sent)


# ---------- Module 2: 835 + posting
def test_835_round_trip_and_posting_balance(out):
    p = out[1]["modules"]["posting"]
    assert p["exceptions"] == 0
    assert p["paid_matches_source"] and p["adjustments_match_source"] and p["denials_queued_match_source"]
    for f in (out[0] / "x12" / "835").glob("*.x12"):
        assert structure_errors(f.read_text(encoding="ascii")) == []


def test_underpayments_found(out):
    p = out[1]["modules"]["posting"]
    assert p["underpayment_recall"] > 0.9 and p["underpayment_precision"] > 0.9


def test_835_parser_reads_foreign_separators():
    text = ("ISA|00|          |00|          |ZZ|PAYER          |ZZ|PROV           |261116|0900|^|00501|000000001|0|T|>!"
            "GS|HP|PAYER|PROV|20261116|0900|1|X|005010X221A1!ST|835|0001!BPR|I|80|C|ACH||||||||||||20261116!"
            "TRN|1|T1|1999999999!CLP|C1|1|100|80|0|12|ICN1!SVC|HC>99213|100|80||1!CAS|CO|45|20!REF|6R|1!"
            "AMT|B6|80!SE|9|0001!GE|1|1!IEA|1|000000001!")
    pay = x835.parse_835(text)
    line = pay[0]["claims"][0]["lines"][0]
    assert line["cpt_code"] == "99213" and line["adjustments"][0]["amount_cents"] == 2000 and line["allowed_cents"] == 8000


def test_posting_flags_unknown_claim(out):
    from rcm import posting
    con = out[2]
    fake = [{"trace_number": "X", "total_paid_cents": 100, "payment_date": "2026-11-01",
             "claims": [{"claim_id": "NOPE", "lines": [], "paid_cents": 100}]}]
    res = posting.post(con, fake)
    assert res["exceptions"] == 1
    posting.post(con, x835.parse_folder(out[0] / "x12" / "835"))   # restore


# ---------- Module 3: prediction
def test_model_beats_chance_and_rules(out):
    m = out[1]["modules"]["prediction"]
    assert m["auc"] > 0.85
    assert m["precision_top_10pct"] > m["rules_only_baseline"]["precision"]


def test_model_features_exclude_answer():
    import inspect
    from rcm import predict
    src = inspect.getsource(predict)
    assert "truth_" not in src and "remit_adjustments" not in src and "denial_queue" not in src


def test_every_claim_scored(out):
    con = out[2]
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == con.execute("SELECT COUNT(*) FROM denial_risk").fetchone()[0]


# ---------- Module 4: recovery
def test_recovery_queue_is_ranked_and_dated(out):
    con = out[2]
    pr = [r[0] for r in con.execute("SELECT priority FROM recovery_queue WHERE status='Open' ORDER BY rowid")]
    assert pr == sorted(pr, reverse=True)
    assert con.execute("SELECT COUNT(*) FROM recovery_queue WHERE status='Open' AND days_left < 0").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM recovery_queue WHERE status='Expired' AND priority > 0").fetchone()[0] == 0


def test_appeal_letters_written(out):
    letters = list((out[0] / "appeals").glob("*.txt"))
    assert letters and "DRAFT" in letters[0].read_text(encoding="utf-8")


# ---------- Module 5: claim status
def test_no_ai_voice_to_payers_that_forbid_it(out):
    assert out[1]["modules"]["status"]["ai_voice_calls_to_prohibited_payers"] == 0


def test_conditional_payer_unlocks_only_when_vendor_registered():
    from rcm.claim_status import _route
    assert _route("Conditional", set(), "BCBSKS")[0] == "HUMAN_CALL"
    assert _route("Conditional", {"BCBSKS"}, "BCBSKS")[0] == "AI_VOICE"
    assert _route("Prohibited", {"FIDNY"}, "FIDNY")[0] == "PORTAL"
    assert _route("IVR-required", set(), "MCR-JL")[0] == "IVR_BOT"


def test_276_277_round_trip():
    payer = {"payer_id": "UHC", "payer_name": "UnitedHealthcare"}
    req = x276.build_276([{"claim_id": "C1", "payer_claim_number": "I1", "member_id": "M1", "last_name": "A",
                           "first_name": "B", "dob": "1970-01-01", "billing_npi": "1234567893",
                           "total_charge_cents": 14500, "dos": "2026-10-01"}], payer)
    resp = x276.build_277([{"claim_id": "C1", "category": "P1", "status_code": "20", "status_date": "2026-10-20",
                            "total_charge_cents": 14500}], payer)
    assert structure_errors(req) == [] and structure_errors(resp) == []
    assert x276.parse_277(resp) == [{"claim_id": "C1", "category": "P1", "status_code": "20", "status_date": "2026-10-20"}]


def test_ai_voice_script_discloses_ai():
    from rcm.claim_status import call_script
    c = {"provider_name": "X Clinic (synthetic)", "billing_npi": "1", "member_id": "M", "date_of_service": "2026-01-01",
         "total_charge_cents": 100}
    script = call_script(c, "AI_VOICE")
    assert "automated AI assistant" in script and "transfer to a staff member" in script


# ---------- Module 6: analytics + outputs
def test_kpis_in_sane_ranges(out):
    k = out[1]["kpis"]
    assert 0.7 < k["net_collection_rate"] <= 1.0
    assert 0.05 < k["initial_denial_rate"] < 0.2
    assert 0.7 < k["clean_claim_rate"] < 1.0
    assert 10 < k["days_in_ar"] < 120
    assert abs(sum(k["ar_aging"].values()) - k["open_ar"]) < 0.01


def test_outputs_exist(out):
    folder = out[0]
    html = (folder / "dashboard.html").read_text(encoding="utf-8")
    assert "RCM Command Center" in html and "<script" not in html and "http" not in html.split("<body")[1]
    for name in ["recovery_queue.csv", "status_worklist.csv", "denial_risk.csv", "underpayments.csv"]:
        assert (folder / "csv" / name).exists()
    json.loads((folder / "report.json").read_text(encoding="utf-8"))

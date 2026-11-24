"""Ingestion tests: templates, CSV/Excel loading, 837 reading, 835 application, de-identification. Run: python -m pytest -q"""
import csv
import sqlite3
from pathlib import Path

import pytest

from rcm import Config
from rcm.db import connect
from rcm.ingest import ingest, write_templates
from rcm.ingest.export import export_templates
from rcm.ingest.loader import norm_dx, norm_pointer, parse_date, parse_money
from rcm.pipeline import run, run_modules
from rcm.x12 import structure_errors, x837

CFG = Config(seed=9, n_patients=700, n_encounters=3500)
QUIET = dict(log=lambda *_: None)

CLAIM_SQL = """SELECT claim_id, payer_id, total_charge_cents, paid_cents, adjudication_date, submission_date,
                      dx1, dx2, dx3, prior_auth_number, frequency_code FROM claims WHERE submission_date IS NOT NULL ORDER BY 1"""
LINE_SQL = """SELECT l.claim_id, line_number, cpt_code, modifier1, modifier2, units, charge_cents, dx_pointer, allowed_cents,
                     l.paid_cents, line_status FROM claim_lines l JOIN claims c USING (claim_id)
              WHERE c.submission_date IS NOT NULL ORDER BY 1, 2"""
ADJ_SQL = "SELECT claim_id, line_number, group_code, carc_code, amount_cents FROM remit_adjustments ORDER BY 1, 2, 3, 4, 5"


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    folder = tmp_path_factory.mktemp("src")
    run(folder, CFG, csv=False, model=False, **QUIET)
    return folder


def same(db_a, db_b, sql):
    a = sqlite3.connect(db_a).execute(sql).fetchall()
    b = sqlite3.connect(db_b).execute(sql).fetchall()
    return a == b, len(a)


# ---------- templates
def test_templates_written(tmp_path):
    write_templates(tmp_path)
    assert (tmp_path / "TEMPLATE_GUIDE.md").exists()
    header = (tmp_path / "claims.csv").read_text(encoding="utf-8-sig").strip().split(",")
    assert header[:3] == ["claim_id", "patient_id", "payer_id"]


# ---------- CSV round trip: synthetic -> templates -> load -> identical claims, lines and payments
def test_csv_round_trip_is_exact(src, tmp_path):
    export_templates(src / "rcm.sqlite", tmp_path / "tpl")
    rep = ingest(tmp_path / "load", csv_dir=tmp_path / "tpl", x835_dir=src / "x12" / "835", **QUIET)
    assert rep["loaded"]["errors"] == 0 and rep["835"]["unmatched_claims"] == 0
    assert rep["data_checks"] == {}
    for sql in (CLAIM_SQL, LINE_SQL, ADJ_SQL):
        ok, n = same(src / "rcm.sqlite", tmp_path / "load" / "rcm.sqlite", sql)
        assert ok and n > 0


# ---------- 837 round trip
def test_837_round_trip_is_exact(src, tmp_path):
    con = connect(src / "rcm.sqlite")
    folder = tmp_path / "837"
    folder.mkdir()
    for i, (pid,) in enumerate(con.execute("SELECT payer_id FROM payers").fetchall()):
        text = x837.build_837(con, pid, control=i + 1)
        if text:
            assert structure_errors(text) == []
            (folder / f"claims_{pid}.edi").write_text(text, encoding="ascii")
    rep = ingest(tmp_path / "load", x837_dir=folder, x835_dir=src / "x12" / "835", **QUIET)
    assert rep["loaded"]["errors"] == 0 and rep["data_checks"] == {}
    for sql in (CLAIM_SQL, LINE_SQL, ADJ_SQL):
        ok, n = same(src / "rcm.sqlite", tmp_path / "load" / "rcm.sqlite", sql)
        assert ok and n > 0


def test_837_replacement_claim_keeps_latest_version(src, tmp_path):
    con = connect(src / "rcm.sqlite")
    cid = con.execute("SELECT claim_id FROM claims WHERE payer_id = 'AETNA' AND submission_date IS NOT NULL LIMIT 1").fetchone()[0]
    text = x837.build_837(con, "AETNA", [cid])
    corrected = text.replace("CLM*" + cid + "*", "CLM*" + cid + "*").replace(":B:1*", ":B:7*")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1_original.edi").write_text(text, encoding="ascii")
    (tmp_path / "a" / "2_corrected.edi").write_text(corrected, encoding="ascii")
    rec = x837.parse_837_folder(tmp_path / "a")
    assert [c["frequency_code"] for c in rec["claims"] if c["claim_id"] == cid] == ["7"]
    assert len({(l["claim_id"], l["line_number"]) for l in rec["claim_lines"]}) == len(rec["claim_lines"])


# ---------- messy client files
def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def messy_folder(folder: Path):
    folder.mkdir()
    write_csv(folder / "Patients.csv", ["Account Number", "First Name", "Last Name", "Date of Birth", "Gender"],
              [["A1", "Ann", "Lee", "03/15/1960", "F"], ["A2", "Bob", "Ray", "not a date", "M"]])
    write_csv(folder / "claims.csv", ["Claim Number", "Account Number", "Payor ID", "Payer", "Subscriber ID", "Billing NPI",
                                      "DOS", "ICD1", "Bill Date"],
              [["C1", "A1", "60054", "Aetna", "W123", "1234567893", "2026-02-01", "E119", "2/3/2026"],
               ["C2", "A2", "60054", "Aetna", "W456", "1234567893", "2026-02-02", "I10", "2026-02-04"],
               ["C3", "A1", "MCRB", "Medicare Part B", "1EG4", "1234567893", "2026-02-05", "M17.11", ""]])
    write_csv(folder / "claim_lines.csv", ["Claim Number", "Line", "CPT", "Units", "Billed Amount", "Pointer"],
              [["C1", "1", "99213", "1", "$145.00", "A"], ["C1", "2", "36415", "1", "25", "1"],
               ["C2", "1", "99214", "1", "210", "1"], ["C3", "1", "20610", "1", "1,220.50", ""],
               ["C9", "1", "99213", "1", "100", "1"]])


def test_messy_csv_loads_what_it_can_and_reports_the_rest(tmp_path):
    messy_folder(tmp_path / "in")
    rep = ingest(tmp_path / "out", csv_dir=tmp_path / "in", **QUIET)
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert sorted(r[0] for r in con.execute("SELECT claim_id FROM claims")) == ["C1", "C3"]      # C2's patient has a bad DOB
    assert con.execute("SELECT total_charge_cents FROM claims WHERE claim_id='C3'").fetchone()[0] == 122050
    assert con.execute("SELECT dx1, submission_date FROM claims WHERE claim_id='C1'").fetchone() == ("E11.9", "2026-02-03")
    assert con.execute("SELECT dx_pointer FROM claim_lines WHERE claim_id='C3'").fetchone()[0] == ""   # blank stays a defect
    assert con.execute("SELECT current_status FROM claims WHERE claim_id='C3'").fetchone()[0] == "Created"
    lob = dict(con.execute("SELECT payer_id, line_of_business FROM payers"))
    assert lob == {"60054": "Commercial", "MCRB": "Medicare FFS"}
    issues = (tmp_path / "out" / "ingest_issues.csv").read_text(encoding="utf-8-sig")
    assert "could not read as date" in issues and "C9" in issues and "timely filing limit not supplied" in issues
    assert "not a date" not in issues                          # patient values are never echoed
    assert rep["loaded"]["claims_excluded"] >= 1


def test_value_parsers():
    assert parse_date("03/15/2026") == parse_date("20260315") == parse_date("2026-03-15") == "2026-03-15"
    assert parse_date("46096") == "2026-03-15"                  # Excel serial
    assert parse_money("$1,234.50") == 123450 and parse_money("(10.00)") == -1000
    assert norm_dx("e119") == "E11.9" and norm_dx("I10") == "I10"
    assert norm_pointer("A:B") == "1,2" and norm_pointer("") == "" and norm_pointer("", "1") == "1"


def test_excel_file_reads(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    messy_folder(tmp_path / "in")
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in csv.reader(open(tmp_path / "in" / "claim_lines.csv", newline="", encoding="utf-8")):
        ws.append(r)
    (tmp_path / "in" / "claim_lines.csv").unlink()
    wb.save(tmp_path / "in" / "claim_lines.xlsx")
    ingest(tmp_path / "out", csv_dir=tmp_path / "in", **QUIET)
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert con.execute("SELECT COUNT(*) FROM claim_lines").fetchone()[0] == 3


# ---------- 835 behaviours seen in real files
ISA = "ISA*00*          *00*          *ZZ*PAYER          *ZZ*PROV           *260301*0900*^*00501*000000001*0*P*:~"


def era(trace, date, clp_body):
    segs = [ISA, "GS*HP*PAYER*PROV*20260301*0900*1*X*005010X221A1~", "ST*835*0001~",
            f"BPR*I*{clp_body[0]}*C*ACH************{date}~", f"TRN*1*{trace}*1999999999~", "N1*PR*AETNA*PI*60054~", "LX*1~"]
    segs += clp_body[1]
    segs += [f"SE*{len(segs) - 1}*0001~", "GE*1*1~", "IEA*1*000000001~"]
    return "\n".join(segs)


def test_claim_level_denial_then_paid_on_appeal(tmp_path):
    messy_folder(tmp_path / "in")
    (tmp_path / "era").mkdir()
    # 1) whole-claim denial with only claim-level CAS (no SVC loops)
    (tmp_path / "era" / "a.835").write_text(era("T1", "20260301", (0, ["CLP*C1*4*170*0*0*12*ICN1~", "CAS*CO*16*170~"])), encoding="ascii")
    # 2) appeal paid later, line level
    (tmp_path / "era" / "b.835").write_text(era("T2", "20260410", (120, [
        "CLP*C1*1*170*120*0*12*ICN1~", "SVC*HC:99213*145*100**1~", "CAS*CO*45*45~", "REF*6R*1~",
        "SVC*HC:36415*25*20**1~", "CAS*CO*45*5~", "REF*6R*2~"])))
    rep = ingest(tmp_path / "out", csv_dir=tmp_path / "in", x835_dir=tmp_path / "era", **QUIET)
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert con.execute("SELECT current_status, paid_cents, adjudication_date FROM claims WHERE claim_id='C1'").fetchone() == \
        ("Paid", 12000, "2026-04-10")
    assert con.execute("SELECT COUNT(*) FROM remit_claims WHERE claim_id='C1'").fetchone()[0] == 2     # history kept
    assert rep["data_checks"] == {}
    mrep = run_modules(tmp_path / "out" / "rcm.sqlite", model=False, csv=False, **QUIET)
    assert mrep["modules"]["posting"]["denied_lines"] == 0          # paid on appeal: no longer in the denial queue
    assert mrep["modules"]["posting"]["exceptions"] == 0


def test_claim_level_denial_spreads_across_lines(tmp_path):
    messy_folder(tmp_path / "in")
    (tmp_path / "era").mkdir()
    (tmp_path / "era" / "a.835").write_text(era("T1", "20260301", (0, ["CLP*C1*4*170*0*0*12*ICN1~", "CAS*CO*197*170~"])), encoding="ascii")
    ingest(tmp_path / "out", csv_dir=tmp_path / "in", x835_dir=tmp_path / "era", **QUIET)
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert con.execute("SELECT line_status, COUNT(*) FROM claim_lines WHERE claim_id='C1' GROUP BY 1").fetchall() == [("Denied", 2)]
    assert con.execute("SELECT SUM(amount_cents) FROM remit_adjustments WHERE claim_id='C1'").fetchone()[0] == 17000


# ---------- de-identification
def test_deidentified_load_has_no_identifiers_and_same_results(src, tmp_path):
    export_templates(src / "rcm.sqlite", tmp_path / "tpl")
    ingest(tmp_path / "plain", csv_dir=tmp_path / "tpl", x835_dir=src / "x12" / "835", **QUIET)
    ingest(tmp_path / "deid", csv_dir=tmp_path / "tpl", x835_dir=src / "x12" / "835", deidentify_key="k1", **QUIET)
    raw = (tmp_path / "deid" / "rcm.sqlite").read_bytes()
    s = sqlite3.connect(src / "rcm.sqlite")
    provider_text = " ".join(r[0] for r in s.execute("SELECT provider_name FROM providers"))   # kept on purpose
    for sql in ("SELECT member_id FROM coverages", "SELECT claim_id FROM claims", "SELECT mrn FROM patients",
                "SELECT payer_claim_number FROM claims", "SELECT patient_id FROM patients"):
        for (v,) in s.execute(sql):
            assert v.encode() not in raw, f"identifier left in file: {sql}"
    for (v,) in s.execute("SELECT DISTINCT last_name FROM patients"):
        if v not in provider_text:
            assert v.encode() not in raw, "patient surname left in file"
    d = sqlite3.connect(tmp_path / "deid" / "rcm.sqlite")
    assert d.execute("SELECT COUNT(*) FROM patients WHERE first_name <> ''").fetchone()[0] == 0
    src_names = {r[0] for r in s.execute("SELECT last_name FROM patients")}
    assert not src_names & {r[0] for r in d.execute("SELECT last_name FROM patients")}
    assert all(dob.endswith("-01-01") for (dob,) in d.execute("SELECT dob FROM patients"))
    assert all(z == "" or z.endswith("00") for (z,) in d.execute("SELECT zip FROM patients"))
    assert d.execute("PRAGMA foreign_key_check").fetchall() == []
    a = run_modules(tmp_path / "plain" / "rcm.sqlite", model=False, csv=False, **QUIET)
    b = run_modules(tmp_path / "deid" / "rcm.sqlite", model=False, csv=False, **QUIET)
    for k in ("recall", "precision", "preventable_dollars"):
        assert a["modules"]["scrubber"][k] == b["modules"]["scrubber"][k]
    assert a["modules"]["posting"]["underpaid_dollars"] == b["modules"]["posting"]["underpaid_dollars"]
    assert b["modules"]["posting"]["exceptions"] == 0
    assert a["kpis"]["net_collection_rate"] == b["kpis"]["net_collection_rate"]


def test_same_key_same_ids_other_key_different(src, tmp_path):
    export_templates(src / "rcm.sqlite", tmp_path / "tpl")
    ids = []
    for name, key in (("a", "k1"), ("b", "k1"), ("c", "k2")):
        ingest(tmp_path / name, csv_dir=tmp_path / "tpl", deidentify_key=key, **QUIET)
        ids.append(sorted(r[0] for r in sqlite3.connect(tmp_path / name / "rcm.sqlite").execute("SELECT patient_id FROM patients")))
    assert ids[0] == ids[1] and ids[0] != ids[2]


# ---------- modules on loaded data
def test_auth_edit_off_without_auth_file(tmp_path):
    messy_folder(tmp_path / "in")
    ingest(tmp_path / "out", csv_dir=tmp_path / "in", **QUIET)
    run_modules(tmp_path / "out" / "rcm.sqlite", model=False, csv=False, **QUIET)
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert con.execute("SELECT COUNT(*) FROM scrub_edits WHERE edit_code='AUTH'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM scrub_edits WHERE edit_code='INFO'").fetchone()[0] == 1   # blank pointer on C3


def test_unknown_cpt_codes_do_not_break_modules(tmp_path):
    messy_folder(tmp_path / "in")
    p = tmp_path / "in" / "claim_lines.csv"
    p.write_text(p.read_text(encoding="utf-8").replace("36415", "G2211"), encoding="utf-8")
    ingest(tmp_path / "out", csv_dir=tmp_path / "in", **QUIET)
    assert "outside the built-in rule set" in (tmp_path / "out" / "ingest_issues.csv").read_text(encoding="utf-8-sig")
    run_modules(tmp_path / "out" / "rcm.sqlite", model=True, csv=True, **QUIET)
    assert (tmp_path / "out" / "dashboard.html").exists()


def test_claim_status_on_real_data_waits_for_277(src, tmp_path):
    export_templates(src / "rcm.sqlite", tmp_path / "tpl")
    ingest(tmp_path / "load", csv_dir=tmp_path / "tpl", x835_dir=src / "x12" / "835", **QUIET)
    rep = run_modules(tmp_path / "load" / "rcm.sqlite", model=False, csv=False, **QUIET)
    channels = rep["modules"]["status"]["by_channel"]
    assert set(channels) <= {"EDI_276", "BILLING_TEAM"}          # no simulated payer answers, no escalations
    con = sqlite3.connect(tmp_path / "load" / "rcm.sqlite")
    assert con.execute("SELECT COUNT(*) FROM status_worklist WHERE reason='AWAITING_277'").fetchone()[0] > 0
    html = (tmp_path / "load" / "dashboard.html").read_text(encoding="utf-8")
    assert "loaded data" in html and "synthetic data" not in html


def test_deidentified_output_keeps_issue_report_separate(tmp_path):
    messy_folder(tmp_path / "in")
    rep = ingest(tmp_path / "out", csv_dir=tmp_path / "in", deidentify_key="k", **QUIET)
    assert "INTERNAL" in rep["issues"]["file"]
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert "ingest_issues" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    ids = set()
    for t in ("claims", "claim_lines", "claim_events", "encounters"):
        ids |= {r[0] for r in con.execute(f"SELECT {'encounter_id' if t == 'encounters' else 'claim_id'} FROM {t}")}
    assert not ids & {"C1", "C3", "E-C1", "E-C3"}
    assert '"C1"' not in (tmp_path / "out" / "ingest_report.json").read_text(encoding="utf-8")


def test_foreign_837_with_dependent_patient(tmp_path):
    """Another vendor's 837: | separators, > composites, child covered under a parent's plan (HL 23 / NM1*QC)."""
    segs = ["ISA|00|          |00|          |ZZ|OTHERPM        |ZZ|CLEARINGHOUSE  |260210|1200|^|00501|000000007|0|P|>",
            "GS|HC|OTHERPM|CH|20260210|1200|7|X|005010X222A1", "ST|837|0001|005010X222A1",
            "BHT|0019|00|B1|20260210|1200|CH", "NM1|41|2|OTHER PM|||||46|S1", "NM1|40|2|CH|||||46|R1",
            "HL|1||20|1", "PRV|BI|PXC|208000000X", "NM1|85|2|KIDS CLINIC|||||XX|1234567893", "N4|AUSTIN|TX|73301",
            "REF|EI|741234567",
            "HL|2|1|22|1", "SBR|P||GRP9||||||CI", "NM1|IL|1|PARENT|PAT||||MI|XYZ123", "DMG|D8|19800101|F",
            "NM1|PR|2|BLUE CROSS BLUE SHIELD OF TEXAS|||||PI|84980",
            "HL|3|2|23|0", "PAT|19", "NM1|QC|1|CHILD|KID", "N4|AUSTIN|TX|73301", "DMG|D8|20180505|M",
            "CLM|K-100|180|||11>B>1|Y|A|Y|Y", "HI|ABK>J069|ABF>R509",
            "NM1|82|1|DOC|ANNA||||XX|1245319599", "PRV|PE|PXC|208000000X",
            "LX|1", "SV1|HC>99213>25|150|UN|1|||1>2", "DTP|472|D8|20260208",
            "LX|2", "SV1|HC>87880|30|UN|1|||1", "DTP|472|D8|20260208",
            "SE|29|0001", "GE|1|7", "IEA|1|000000007"]
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "batch.txt").write_text("!".join(segs) + "!", encoding="ascii")
    rep = ingest(tmp_path / "out", x837_dir=tmp_path / "in", **QUIET)
    assert rep["loaded"]["claims_loaded"] == 1 and rep["loaded"]["errors"] == 0
    con = sqlite3.connect(tmp_path / "out" / "rcm.sqlite")
    assert con.execute("SELECT first_name, last_name, dob, sex FROM patients").fetchone() == ("KID", "CHILD", "2018-05-05", "M")
    assert con.execute("SELECT member_id FROM coverages").fetchone()[0] == "XYZ123"
    assert con.execute("SELECT dx1, dx2, submission_date, total_charge_cents FROM claims").fetchone() == \
        ("J06.9", "R50.9", "2026-02-10", 18000)
    assert con.execute("SELECT cpt_code, modifier1, dx_pointer FROM claim_lines ORDER BY line_number").fetchall() == \
        [("99213", "25", "1,2"), ("87880", None, "1")]
    assert con.execute("SELECT rendering_npi, date_of_service FROM encounters").fetchone() == ("1245319599", "2026-02-08")
    assert con.execute("SELECT payer_family, line_of_business FROM payers").fetchone() == ("BCBS", "Commercial")

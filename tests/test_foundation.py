"""Foundation module tests. Run: python -m pytest -q"""
import sqlite3

import pytest

from rcm import Config, generate
from rcm.generator import luhn_npi
from rcm.store import to_csv, to_sqlite
from rcm.validate import summary, validate

SMALL = dict(n_patients=600, n_encounters=3000)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    ds = generate(Config(seed=7, **SMALL))
    path = tmp_path_factory.mktemp("rcm") / "rcm.sqlite"
    return to_sqlite(ds, path)


def test_all_integrity_checks_pass(db):
    failures = {k: n for k, n in validate(db).items() if n}
    assert failures == {}


def test_same_seed_same_data():
    a = generate(Config(seed=11, **SMALL))
    b = generate(Config(seed=11, **SMALL))
    assert a.claims == b.claims and a.remit_adjustments == b.remit_adjustments


def test_different_seed_different_data():
    a = generate(Config(seed=1, **SMALL))
    b = generate(Config(seed=2, **SMALL))
    assert a.claims != b.claims


def test_denial_rate_is_realistic(db):
    con = sqlite3.connect(db)
    rate = con.execute("""SELECT AVG(CASE WHEN current_status IN ('Denied','Partially Paid') THEN 1.0 ELSE 0 END)
                          FROM claims WHERE adjudication_date IS NOT NULL""").fetchone()[0]
    assert 0.06 < rate < 0.18


def test_every_denial_driver_appears(db):
    con = sqlite3.connect(db)
    drivers = {r[0] for r in con.execute("SELECT DISTINCT driver FROM truth_denial_drivers")}
    assert {"eligibility", "no_auth", "timely_filing", "bundling", "medical_necessity",
            "duplicate", "missing_info", "non_covered"} <= drivers


def test_driver_matches_carc(db):
    """Ground truth and the 835 must tell the same story."""
    expected = {"eligibility": "27", "no_auth": "197", "timely_filing": "29", "bundling": "97",
                "medical_necessity": "50", "duplicate": "18", "missing_info": "16", "non_covered": "96"}
    con = sqlite3.connect(db)
    rows = con.execute("""SELECT t.driver, a.carc_code FROM truth_denial_drivers t
                          JOIN remit_adjustments a USING (claim_id, line_number)""").fetchall()
    assert rows and all(expected[d] == carc for d, carc in rows)


def test_medicare_status_goes_to_ivr(db):
    con = sqlite3.connect(db)
    assert con.execute("""SELECT COUNT(*) FROM payers WHERE line_of_business='Medicare FFS'
                          AND status_channel <> 'IVR'""").fetchone()[0] == 0


def test_prohibited_payers_never_routed_to_voice(db):
    con = sqlite3.connect(db)
    assert con.execute("""SELECT COUNT(*) FROM payers WHERE ai_call_status='Prohibited'
                          AND status_channel NOT IN ('EDI_276','PORTAL')""").fetchone()[0] == 0


def test_nothing_adjudicated_after_snapshot(db):
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM claims WHERE adjudication_date > '2026-11-15'").fetchone()[0] == 0


def test_npis_are_luhn_valid():
    import random
    rng = random.Random(0)
    for _ in range(200):
        npi = luhn_npi(rng)
        digits = [int(d) for d in "80840" + npi]
        total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2)
                    for i, d in enumerate(reversed(digits)))
        assert len(npi) == 10 and total % 10 == 0


def test_csv_export_in_dollars(tmp_path):
    ds = generate(Config(seed=3, n_patients=100, n_encounters=300))
    folder = to_csv(ds, tmp_path)
    header = (folder / "claims.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "total_charge" in header and "_cents" not in header


def test_summary_shape(db):
    s = summary(db)
    assert s["counts"]["claims"] > 0 and s["dollars"]["paid"] > 0

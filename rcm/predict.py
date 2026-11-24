"""Module 3 — Denial prediction (ClaimBridge on the shared schema).

Scores every claim BEFORE it is sent. Features use only what is known at
submission time: the claim, coverage on file, auths on file, claims already
sent, and denial history from earlier months. The payer's answer and the
ground-truth tables are never features.

Split is by date of service (train on earlier months, test on later ones),
the way the model would actually be used.

Needs scikit-learn (pip install scikit-learn). Everything else in the
platform runs on the standard library.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date

from .db import has_source, replace_table, rows
from .reference import BUNDLING_PAIRS, cpt_info

DDL = """
CREATE TABLE denial_risk (
    claim_id     TEXT PRIMARY KEY REFERENCES claims(claim_id),
    risk_score   REAL NOT NULL,       -- probability the claim comes back with a denied line
    risk_band    TEXT NOT NULL,       -- High / Medium / Low
    reasons      TEXT NOT NULL,       -- JSON list of plain-language reasons
    in_test_set  INTEGER NOT NULL,
    model_version TEXT NOT NULL
);
"""
MODEL_VERSION = "denial-gbm-0.2"
TEST_SHARE = 0.25          # the most recent quarter of adjudicated claims (by date of service) is the test set
MIN_TRAIN, MIN_POSITIVES = 500, 25
MAX_CATEGORIES = {"payer": 150, "cpt": 300}   # rarer values are pooled as "other" so memory stays bounded
IMPORTANCE_SAMPLE = 5000


def split_date(con) -> str | None:
    """Date of service where the test set starts: the latest TEST_SHARE of adjudicated claims."""
    dos = [r[0] for r in con.execute("""SELECT e.date_of_service FROM claims c JOIN encounters e USING (encounter_id)
                                        WHERE c.adjudication_date IS NOT NULL ORDER BY 1""")]
    return dos[int(len(dos) * (1 - TEST_SHARE))] if dos else None


def _base_rows(con):
    claims = rows(con, """
        SELECT c.claim_id, c.patient_id, c.payer_id, c.dx1, c.dx2, c.dx3, c.dx4, c.prior_auth_number,
               c.total_charge_cents, c.submission_date, c.current_status, c.adjudication_date,
               e.date_of_service, e.place_of_service, pr.specialty,
               p.payer_family, p.line_of_business, p.timely_filing_days,
               cv.termination_date, pt.dob
        FROM claims c JOIN encounters e USING (encounter_id) JOIN payers p USING (payer_id)
        JOIN providers pr ON pr.provider_npi = e.rendering_npi
        JOIN coverages cv ON cv.coverage_id = c.coverage_id JOIN patients pt ON pt.patient_id = c.patient_id
        ORDER BY c.submission_date IS NULL, c.submission_date, c.claim_id""")
    lines = defaultdict(list)
    for ln in rows(con, "SELECT * FROM claim_lines ORDER BY line_number"):
        lines[ln["claim_id"]].append(ln)
    auths = defaultdict(list)
    for a in rows(con, "SELECT * FROM authorizations WHERE status='Approved'"):
        auths[(a["patient_id"], a["payer_id"], a["cpt_code"])].append(a)
    return claims, lines, auths


def _frequent_values(claims, lines) -> dict[str, set]:
    from collections import Counter
    payers = Counter(c["payer_id"] for c in claims)
    cpts = Counter(ln["cpt_code"] for ls in lines.values() for ln in ls)
    return {"payer": {k for k, _ in payers.most_common(MAX_CATEGORIES["payer"])},
            "cpt": {k for k, _ in cpts.most_common(MAX_CATEGORIES["cpt"])}}


def build_features(con, test_from: str | None = None):
    test_from = test_from or split_date(con) or "9999-12-31"
    claims, lines, auths = _base_rows(con)
    auths_known = has_source(con, "authorizations")
    keep = _frequent_values(claims, lines)
    sent = set()
    # payer x CPT denial history, built only from claims with service before the test window
    hist = defaultdict(lambda: [0, 0])
    for c in claims:
        if c["adjudication_date"] and c["date_of_service"] < test_from:
            for ln in lines[c["claim_id"]]:
                h = hist[(c["payer_id"], ln["cpt_code"])]
                h[0] += ln["line_status"] == "Denied"
                h[1] += 1
    out = []
    for c in claims:
        ls = lines[c["claim_id"]]
        codes = {ln["cpt_code"]: ln for ln in ls}
        dos = c["date_of_service"]
        dx = [c["dx1"], c["dx2"], c["dx3"], c["dx4"]]
        payer_key = c["payer_id"] if c["payer_id"] in keep["payer"] else "other"
        f: dict = {f"payer={payer_key}": 1, f"family={c['payer_family']}": 1, f"lob={c['line_of_business']}": 1,
                   f"specialty={c['specialty']}": 1, f"pos={c['place_of_service']}": 1}
        for code in codes:
            f[f"cpt={code if code in keep['cpt'] else 'other'}"] = 1
        f["n_lines"] = len(ls)
        f["log_charge"] = math.log1p(c["total_charge_cents"] / 100)
        f["age"] = int(dos[:4]) - int(c["dob"][:4])
        # what a scrubber/eligibility check would know
        f["coverage_ended_before_dos"] = int(bool(c["termination_date"] and dos > c["termination_date"]))
        need_auth = [cp for cp in codes if cpt_info(cp)[2]]
        f["needs_auth"] = int(bool(need_auth))
        f["auth_missing"] = int(auths_known and any(not any(a["valid_from"] <= dos <= a["valid_to"]
                                            for a in auths.get((c["patient_id"], c["payer_id"], cp), []))
                                    for cp in need_auth))
        f["bundle_pair"] = 0
        f["bundle_modifier_missing"] = 0
        for prim, sec, mod, denied in BUNDLING_PAIRS:
            if prim in codes and sec in codes:
                f["bundle_pair"] = 1
                if mod not in (codes[denied]["modifier1"], codes[denied]["modifier2"]):
                    f["bundle_modifier_missing"] = 1
        f["blank_dx_pointer"] = int(any(not ln["dx_pointer"] for ln in ls))
        mismatch = 0
        for ln in ls:
            allowed = cpt_info(ln["cpt_code"])[4]
            if allowed and ln["dx_pointer"]:
                primary = dx[int(ln["dx_pointer"].split(",")[0]) - 1]
                if primary and not any(primary.startswith(p) for p in allowed):
                    mismatch = 1
        f["dx_not_supporting_cpt"] = mismatch
        lag = (date.fromisoformat(c["submission_date"]) - date.fromisoformat(dos)).days if c["submission_date"] else 0
        f["submission_lag_days"] = lag
        f["days_past_filing_limit"] = max(0, lag - c["timely_filing_days"])
        key = (c["patient_id"], dos, tuple(sorted(codes)))
        f["possible_duplicate"] = int(key in sent)
        if c["submission_date"]:
            sent.add(key)
        rates = [hist[(c["payer_id"], cp)][0] / hist[(c["payer_id"], cp)][1]
                 for cp in codes if hist[(c["payer_id"], cp)][1] >= 5]
        f["payer_cpt_hist_denial_rate"] = max(rates) if rates else 0.0

        label = None
        if c["adjudication_date"]:
            label = int(c["current_status"] in ("Denied", "Partially Paid"))
        out.append({"claim_id": c["claim_id"], "dos": dos, "features": f, "label": label,
                    "payer_id": c["payer_id"], "codes": list(codes)})
    return out


REASON_TEXT = {
    "coverage_ended_before_dos": "Coverage on file ended before the date of service",
    "auth_missing": "Service needs prior authorization and none is on file",
    "bundle_modifier_missing": "Bundled pair billed without modifier 25/59",
    "blank_dx_pointer": "Service line not linked to a diagnosis",
    "dx_not_supporting_cpt": "Primary diagnosis may not support the procedure",
    "days_past_filing_limit": "Past the payer's timely filing limit",
    "possible_duplicate": "Looks like a duplicate of a claim already sent",
}


def _reasons(f: dict) -> list[str]:
    out = [txt for k, txt in REASON_TEXT.items() if f.get(k)]
    if f.get("payer_cpt_hist_denial_rate", 0) >= 0.15:
        out.append(f"This payer has denied this service {f['payer_cpt_hist_denial_rate']:.0%} of the time")
    return out


def train_and_score(con) -> dict:
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.metrics import average_precision_score, roc_auc_score
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Module 3 needs scikit-learn: pip install scikit-learn") from e

    test_from = split_date(con)
    data = build_features(con, test_from)
    labelled = [d for d in data if d["label"] is not None]
    train = [d for d in labelled if d["dos"] < test_from] if test_from else []
    test = [d for d in labelled if d["dos"] >= test_from] if test_from else []
    pos_tr, pos_te = sum(d["label"] for d in train), sum(d["label"] for d in test)
    if len(train) < MIN_TRAIN or pos_tr < MIN_POSITIVES or pos_te < 5 or pos_te == len(test):
        return {"skipped": f"not enough history to train ({len(train)} training claims, {pos_tr} denied; "
                           f"need {MIN_TRAIN}+ claims and {MIN_POSITIVES}+ denials)"}
    import numpy as np
    vec = DictVectorizer(sparse=False, dtype=np.float32)
    Xtr = vec.fit_transform([d["features"] for d in train])
    ytr = [d["label"] for d in train]
    model = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.06, max_leaf_nodes=31,
                                           l2_regularization=1.0, random_state=0)
    model.fit(Xtr, ytr)

    Xall = vec.transform([d["features"] for d in data])
    scores = model.predict_proba(Xall)[:, 1]
    by_id = {d["claim_id"]: s for d, s in zip(data, scores)}

    yte = [d["label"] for d in test]
    ste = [by_id[d["claim_id"]] for d in test]
    order = sorted(range(len(test)), key=lambda i: -ste[i])
    top = order[: max(1, len(order) // 10)]
    base_rate = sum(yte) / len(yte)
    # baseline: the scrubber's rules alone (any flag = predicted denial)
    rule_keys = list(REASON_TEXT)
    rule_pred = [int(any(d["features"].get(k) for k in rule_keys)) for d in test]
    tp = sum(p and y for p, y in zip(rule_pred, yte))

    test_ids = {d["claim_id"] for d in test}
    records = []
    for d, s in zip(data, scores):
        band = "High" if s >= 0.5 else "Medium" if s >= 0.15 else "Low"
        records.append({"claim_id": d["claim_id"], "risk_score": round(float(s), 4), "risk_band": band,
                        "reasons": json.dumps(_reasons(d["features"])), "in_test_set": int(d["claim_id"] in test_ids),
                        "model_version": MODEL_VERSION})
    replace_table(con, DDL, "denial_risk", records)

    # global importance by permutation on the test set (small and fast)
    from sklearn.inspection import permutation_importance
    import random as _random
    sample = _random.Random(0).sample(range(len(test)), min(IMPORTANCE_SAMPLE, len(test)))
    Xte = vec.transform([test[i]["features"] for i in sample])
    yte_s = [yte[i] for i in sample]
    pi = (permutation_importance(model, Xte, yte_s, n_repeats=3, random_state=0, scoring="roc_auc")
          if len(set(yte_s)) > 1 else None)
    names = vec.get_feature_names_out()
    top_feats = sorted(zip(names, pi.importances_mean), key=lambda x: -x[1])[:10] if pi is not None else []

    return {
        "model": MODEL_VERSION, "test_from_dos": test_from, "train_claims": len(train), "test_claims": len(test),
        "test_denial_rate": round(base_rate, 3),
        "auc": round(roc_auc_score(yte, ste), 3),
        "average_precision": round(average_precision_score(yte, ste), 3),
        "precision_top_10pct": round(sum(yte[i] for i in top) / len(top), 3),
        "recall_top_10pct": round(sum(yte[i] for i in top) / max(1, sum(yte)), 3),
        "rules_only_baseline": {"precision": round(tp / max(1, sum(rule_pred)), 3),
                                "recall": round(tp / max(1, sum(yte)), 3)},
        "top_features": [(n, round(float(v), 4)) for n, v in top_feats],
        "scored_unsent_or_pending": sum(1 for d in data if d["label"] is None),
    }

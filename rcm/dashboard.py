"""Offline HTML dashboard: one self-contained file, inline SVG charts, no scripts or network calls."""
from __future__ import annotations

import html
from pathlib import Path

from .db import rows
from .reference import CARC

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--a:#2563eb;--b:#94a3b8;--bad:#c2410c;--good:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--a:#60a5fa;--b:#64748b;--bad:#fb923c;--good:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}.grid{display:grid;gap:12px}.k{grid-template-columns:repeat(4,1fr)}.k3{grid-template-columns:repeat(3,1fr)}@media (max-width:760px){.k,.k3{grid-template-columns:repeat(2,1fr)}}.grid>*{min-width:0}.sx{overflow-x:auto}.sx svg{min-width:440px}.sx.wide svg{min-width:720px}
.two{grid-template-columns:1fr 1fr}@media (max-width:860px){.two{grid-template-columns:1fr}}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kv{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}.kl{color:var(--muted);font-size:12px}.kn{color:var(--muted);font-size:11px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}td.n,th.n{text-align:right}.tw{overflow-x:auto}svg text{fill:var(--ink);font-size:11px}svg .m{fill:var(--muted)}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;border:1px solid var(--line)}.note{color:var(--muted);font-size:12px;margin-top:8px}
section{margin-top:18px}
"""


CARC_SHORT = {"197": "No authorization", "16": "Missing information", "50": "Medical necessity", "27": "Coverage ended",
              "18": "Duplicate", "96": "Non-covered", "97": "Bundled", "29": "Timely filing"}
CHANNEL_LABEL = {"EDI_276": "EDI 276/277", "BILLING_TEAM": "Billing team (unbilled)", "IVR_BOT": "Payer IVR (bot)",
                 "PORTAL": "Portal / staff", "AI_VOICE": "AI voice agent", "HUMAN_CALL": "Staff call"}


def _usd(x: float, short=True) -> str:
    if short and abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if short and abs(x) >= 10_000:
        return f"${x / 1000:.0f}K"
    return f"${x:,.0f}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def hbar(items: list[tuple[str, float, str]], width=520, color="var(--a)", lab=150, room=120) -> str:
    """Horizontal bars: (label, value, value_text). lab = label column px, room = px kept for the value text."""
    if not items:
        return "<p class='note'>No data.</p>"
    row = 24
    vmax = max(v for _, v, _ in items) or 1
    h = row * len(items) + 4
    out = [f'<div class="sx"><svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    for i, (label, v, txt) in enumerate(items):
        y = i * row + 4
        w = (width - lab - room) * v / vmax
        out.append(f'<text x="0" y="{y + 13}">{html.escape(label)}</text>'
                   f'<rect x="{lab}" y="{y + 2}" width="{max(w, 1):.1f}" height="14" rx="3" fill="{color}"/>'
                   f'<text x="{lab + w + 6:.1f}" y="{y + 13}" class="m">{html.escape(txt)}</text>')
    out.append("</svg></div>")
    return "".join(out)


def monthly_chart(monthly: list[dict], width=1080, height=220) -> str:
    if not monthly:
        return ""
    pad_l, pad_b = 52, 26
    vmax = max(max(m["charges"], m["paid"]) for m in monthly) or 1
    n = len(monthly)
    slot = (width - pad_l) / n
    bw = slot * 0.34
    plot_h = height - pad_b - 10
    out = [f'<div class="sx wide"><svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for t in range(0, 5):
        v = vmax * t / 4
        y = 10 + plot_h - plot_h * t / 4
        out.append(f'<line x1="{pad_l}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>'
                   f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" class="m">{_usd(v)}</text>')
    for i, m in enumerate(monthly):
        x = pad_l + i * slot + slot * 0.14
        for j, (key, col) in enumerate((("charges", "var(--b)"), ("paid", "var(--a)"))):
            hgt = plot_h * m[key] / vmax
            out.append(f'<rect x="{x + j * bw:.1f}" y="{10 + plot_h - hgt:.1f}" width="{bw - 2:.1f}" height="{hgt:.1f}" rx="2" fill="{col}">'
                       f'<title>{m["month"]} {key}: {_usd(m[key], False)}</title></rect>')
        out.append(f'<text x="{x + bw:.1f}" y="{height - 8}" text-anchor="middle" class="m">{m["month"][5:]}/{m["month"][2:4]}</text>')
    out.append("</svg></div>")
    return "".join(out)


def _table(headers: list[str], data: list[list], numeric: set[int] = frozenset()) -> str:
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{html.escape(h)}</th>' for i, h in enumerate(headers))
    trs = "".join("<tr>" + "".join(f'<td class="{"n" if i in numeric else ""}">{html.escape(str(c))}</td>'
                                   for i, c in enumerate(r)) + "</tr>" for r in data)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def render(con, kpis: dict, results: dict) -> str:
    from .db import has_source
    k = kpis
    if has_source(con, "synthetic"):
        data_label, footer = "synthetic data", "All patients, providers and amounts are synthetic."
    else:
        deid = has_source(con, "deidentified")
        data_label = "loaded data" + (" (de-identified)" if deid else "")
        footer = ("Patient identifiers are de-identified." if deid else
                  "Contains identifiable patient data: keep this file inside the organization.")
    cards = [
        ("Net collection rate", _pct(k["net_collection_rate"]), "collected ÷ contracted allowed"),
        ("Initial denial rate", _pct(k["initial_denial_rate"]), f"{k['claims_adjudicated']:,} adjudicated claims"),
        ("Clean claim rate", _pct(k["clean_claim_rate"]), "paid in full, first pass"),
        ("Days in A/R", f"{k['days_in_ar']:.1f}", "open A/R ÷ avg daily charges (90d)"),
        ("Open A/R", _usd(k["open_ar"]), "pending claims + workable denials"),
        ("Payer paid", _usd(k["payer_paid"]), f"of {_usd(k['charges'])} billed"),
        ("Avg days to pay", f"{k['avg_days_dos_to_payment']:.0f}", "date of service → 835"),
        ("Denied dollars", _usd(k["denied"]), "all denied lines"),
    ]
    card_html = "".join(f'<div class="card"><div class="kl">{a}</div><div class="kv">{b}</div><div class="kn">{c}</div></div>'
                        for a, b, c in cards)

    fam = hbar([(f["family"], f["denial_rate"], _pct(f["denial_rate"])) for f in k["denial_by_family"]], color="var(--bad)", lab=90, room=60)
    carc = hbar([(f"{d['carc']} · {CARC_SHORT.get(d['carc']) or CARC.get(d['carc'], 'Other')[:22]}", d["dollars"], _usd(d["dollars"]))
                 for d in k["denial_by_carc"]], color="var(--bad)", lab=170, room=60)
    aging = hbar([(f"{b} days", v, _usd(v)) for b, v in k["ar_aging"].items()], lab=90, room=60)

    s, po, pm, rc, st = (results.get(x, {}) for x in ("scrubber", "posting", "prediction", "recovery", "status"))
    rq = rows(con, """SELECT r.claim_id, p.payer_name, r.cpt_code, r.root_cause, r.action, r.owner_team,
                             r.denied_cents, r.days_left, r.priority
                      FROM recovery_queue r JOIN payers p USING (payer_id) WHERE r.status='Open'
                      ORDER BY r.priority DESC LIMIT 12""")
    rq_table = _table(["Claim", "Payer", "CPT", "Root cause", "Action", "Team", "Denied", "Days left"],
                      [[r["claim_id"], r["payer_name"], r["cpt_code"], r["root_cause"], r["action"], r["owner_team"],
                        _usd(r["denied_cents"] / 100, False), r["days_left"]] for r in rq], {6, 7})
    team = hbar([(t, n, f"{n}") for t, n in (rc.get("by_team") or {}).items()], lab=180, room=50)
    sw = rows(con, """SELECT w.claim_id, p.payer_name, p.ai_call_status, w.reason, w.days_open, w.channel, w.channel_reason, w.charge_cents
                      FROM status_worklist w JOIN payers p USING (payer_id) WHERE w.channel <> 'EDI_276'
                      ORDER BY w.priority DESC LIMIT 10""")
    sw_table = _table(["Claim", "Payer", "AI-call policy", "Reason", "Days", "Channel", "Why"],
                      [[r["claim_id"], r["payer_name"], r["ai_call_status"], r["reason"], r["days_open"], r["channel"],
                        r["channel_reason"]] for r in sw], {4})
    chan = hbar([(CHANNEL_LABEL.get(c, c), n, str(n)) for c, n in (st.get("by_channel") or {}).items()], lab=170, room=50)
    up = rows(con, """SELECT p.payer_name, COUNT(*) AS n, SUM(u.variance_cents) AS v FROM underpayments u
                      JOIN payers p USING (payer_id) GROUP BY 1 ORDER BY v DESC LIMIT 8""")
    up_bar = hbar([(r["payer_name"][:26], r["v"] / 100, f"{_usd(r['v'] / 100, False)} · {r['n']} lines") for r in up],
                  lab=190, room=130)
    drv = s.get("by_driver") or {}
    scrub_table = _table(["Denial driver", "Denied lines", "Caught pre-submission", "Recall"],
                         [[d, v["denied_lines"], v["caught"], _pct(v["recall"])] for d, v in drv.items()], {1, 2, 3})
    feats = pm.get("top_features") or []

    def mcard(title, value, note):
        return f'<div class="card"><div class="kl">{title}</div><div class="kv">{value}</div><div class="kn">{note}</div></div>'

    module_cards = "".join([
        mcard("Scrubber: denials caught", _pct(s.get("recall", 0)), f"{s.get('precision', 0) * 100:.0f}% precision · {_usd(s.get('preventable_dollars', 0))} preventable"),
        (mcard("Denial model AUC", f"{pm['auc']:.3f}", f"top-10% precision {_pct(pm['precision_top_10pct'])}") if pm.get("auc") is not None
         else mcard("Denial model", "—", pm.get("skipped") or "not run (scikit-learn not installed or --no-model)")),
        mcard("Expected recovery", _usd(rc.get("expected_recovery_dollars", 0)), f"{rc.get('open', 0)} open items · {rc.get('due_in_14_days', 0)} due ≤14 days"),
        mcard("Underpayments found", _usd(po.get("underpaid_dollars", 0)), f"{po.get('underpayments', 0)} lines below contract"),
        mcard("835 posting exceptions", str(po.get("exceptions", 0)), f"{po.get('claims_posted', 0):,} claims posted from 835 files"),
        mcard("AI calls to restricted payers", str(st.get("ai_voice_calls_to_prohibited_payers", 0)), "must stay 0"),
    ])

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RCM Command Center</title><style>{CSS}</style></head><body><div class="wrap">
<h1>RCM Command Center</h1>
<p class="sub">Snapshot {k['as_of']} · {data_label} · end-to-end pipeline output</p>
<div class="grid k">{card_html}</div>

<section class="card"><h2>Charges vs payer payments by month of service</h2>{monthly_chart(k['monthly'])}
<p class="note"><span class="pill" style="background:var(--b)">&nbsp;</span> charges &nbsp; <span class="pill" style="background:var(--a)">&nbsp;</span> payer paid. Recent months are still being adjudicated.</p></section>

<section><h2>What each module did</h2><div class="grid k3">{module_cards}</div></section>

<section class="grid two">
<div class="card"><h2>Initial denial rate by payer family</h2>{fam}</div>
<div class="card"><h2>Denied dollars by reason</h2>{carc}</div>
<div class="card"><h2>A/R aging (by date of service)</h2>{aging}</div>
<div class="card"><h2>Scrubber: denials it would have stopped</h2>{scrub_table}
<p class="note">Non-covered services can't be caught before submission; they need a benefits check at scheduling.</p></div>
</section>

<section class="card"><h2>Denial recovery: top of the work queue</h2>{rq_table}
<p class="note">Priority = denied $ × chance of recovery × urgency (deadline from payer appeal or filing limit).</p></section>

<section class="grid two">
<div class="card"><h2>Open recovery items by team</h2>{team}</div>
<div class="card"><h2>Underpayments vs contract, by payer</h2>{up_bar}</div>
</section>

<section class="grid two">
<div class="card"><h2>Claim-status follow-up by channel</h2>{chan}
<p class="note">EDI 276/277 first. Escalations go only to channels the payer's AI-call policy allows.</p></div>
<div class="card"><h2>Denial model: strongest signals</h2>{_table(["Feature", "Importance"], [[f, f"{v:.4f}"] for f, v in feats[:8]], {1}) if feats else "<p class='note'>Model not run.</p>"}</div>
</section>

<section class="card"><h2>Claim-status escalations</h2>{sw_table}</section>
<p class="note">{footer} Generated by rcm-platform.</p>
</div></body></html>"""


def write(con, kpis: dict, results: dict, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(render(con, kpis, results), encoding="utf-8")
    return path

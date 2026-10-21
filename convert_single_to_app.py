#!/usr/bin/env python3
"""Convert design-B single-agent cards (data/cards_single/<APP>.json, fields inline
as {text,citations}) into the app's native shape so app.py renders them with the
existing machinery:

  - data/cards_new/<APP>.json         value card (+ _meta.expedited, _meta.indication,
                                      and the full cited approval_program kept intact)
  - data/cards_recited_new.jsonl      {app_key, card} cited overlay ({text,citations})

Deterministic metadata (approval date, priority) is already in each B card's _meta.
"""
import json, glob, re, datetime
from pathlib import Path

def _parse_date(s):
    s = (s or "").strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try: return datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError: pass
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", s)
    if m:
        try: return datetime.datetime.strptime(m.group(1), "%m/%d/%Y")
        except ValueError: pass
    for fmt in ("%B %d, %Y", "%d %B %Y", "%d %b %Y"):
        try: return datetime.datetime.strptime(s, fmt)
        except ValueError: pass
    return None

def tta_months(approval_iso, receipt_text):
    """Time-to-approval in months = approval_date - FDA receipt date. Returns None
    if either date is unparseable (never guessed)."""
    a, r = _parse_date(approval_iso), _parse_date(receipt_text)
    if a and r and a > r:
        return round((a - r).days / 30.4375, 1)
    return None

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
OUT_CARDS = HERE / "data" / "cards_new"
OUT_CARDS.mkdir(parents=True, exist_ok=True)
OUT_CITED = HERE / "data" / "cards_recited_new.jsonl"

def t(f):
    """plain text/value of a {text,citations} leaf (or passthrough)."""
    if isinstance(f, dict):
        return f.get("text", f.get("value"))
    return f

cited_lines = []
n = 0
for fp in sorted(glob.glob(str(SRC / "*.json"))):
    d = json.load(open(fp))
    app = d["app_key"]
    c = d.get("card", {})
    m = d.get("_meta", {})
    if not isinstance(c, dict):     # malformed card (e.g. saved as a string) — skip, don't abort the whole run
        print(f"  SKIP {app}: card is {type(c).__name__}, not a dict", flush=True)
        continue
    ap = c.get("approval_program", {}) or {}

    def present(k):
        v = ap.get(k, {})
        return bool(v.get("present")) if isinstance(v, dict) else False

    exped = {
        "btd": present("breakthrough_therapy"), "ft": present("fast_track"),
        "pr": present("priority_review"), "orph": present("orphan_drug"),
        "accelerated": present("accelerated_approval"), "qidp": present("qidp"),
    }
    # value pivotal_trials (extract text)
    vtrials = []
    for tr in (c.get("pivotal_trials") or []):
        vtrials.append({
            "trial_name": t(tr.get("trial_name")), "nct": t(tr.get("nct")),
            "phase": t(tr.get("phase")), "design": t(tr.get("design")),
            "population": t(tr.get("population")),
            "endpoint": t(tr.get("endpoint")), "n": t(tr.get("n")),
            "result": t(tr.get("result")),
            # NCT looked up on ClinicalTrials.gov by sponsor study id / acronym when the FDA
            # docs don't print one (backfill_nct_ctgov.py); shown as "via ClinicalTrials.gov"
            "nct_ctgov": tr.get("nct_ctgov"),
        })
    bw = c.get("boxed_warning", {}) or {}
    rems = c.get("rems", {}) or {}
    cdx = c.get("companion_dx", {}) or {}
    adc = c.get("advisory_committee", {}) or {}
    fca = c.get("first_cycle_approval", {}) or {}

    value_card = {
        "_meta": {
            "drug": m.get("drug", ""), "appno": m.get("appno", ""), "type": m.get("type", ""),
            "active": m.get("active", ""), "sponsor": m.get("sponsor", ""),
            "indication": t(c.get("indication")) or "",
            "approval_date": m.get("approval_date", ""),
            "review_designation": "Priority" if m.get("review_priority") == "PRIORITY" else "Standard",
            "expedited": exped,
            "receipt_date": t(c.get("receipt_date")),
            "tta_months": tta_months(m.get("approval_date", ""), t(c.get("receipt_date"))),
        },
        "pivotal_trials": vtrials,
        "key_safety": t(c.get("key_safety")) or "",
        "target_moa": t(c.get("target_moa")) or "",
        "modality": t(c.get("modality")) or "",
        "boxed_warning": {"present": bw.get("present"), "content": t(bw.get("detail"))},
        "rems": {"present": rems.get("present"), "type": t(rems.get("detail"))},
        "companion_dx": cdx.get("present"),
        "companion_dx_detail": t(cdx.get("detail")),
        "advisory_committee": {"convened": adc.get("convened"), "vote": t(adc.get("vote"))},
        "first_cycle_approval": fca.get("value"),
        "therapeutic_area": c.get("therapeutic_area"),
        "indication_details": {},
        # None = efficacy-supplement extraction never ran; [] = deterministically none;
        # list = the efficacy supplements with cited descriptions.
        "supplements": (c["supplements"] if "supplements" in c else None),
        "approval_program": ap,        # NEW: kept with {text,citations} for the detail renderer
        # None = extraction never ran for this card (do NOT imply "none exist");
        # [] = letter WAS read and states none; list = the PMRs/PMCs.
        "post_marketing": (c["post_marketing"] if "post_marketing" in c else None),
        "crl_history": c.get("crl_history") or {},          # prior CRL history (cited)
        "_verify": d.get("_verify"),
        "_source": "single_agent_opus",
    }
    (OUT_CARDS / f"{app}.json").write_text(json.dumps(value_card, indent=2))

    # cited overlay: B's card fields are already {text,citations}; expose the paths app.py walks
    cited = {
        "indication": c.get("indication"), "key_safety": c.get("key_safety"),
        "target_moa": c.get("target_moa"), "modality": c.get("modality"),
        "pivotal_trials": [
            {k: tr.get(k) for k in ("trial_name", "phase", "design", "population", "endpoint", "result", "n")}
            for tr in (c.get("pivotal_trials") or [])
        ],
    }
    cited_lines.append(json.dumps({"app_key": app, "card": cited}))
    n += 1

OUT_CITED.write_text("\n".join(cited_lines) + "\n")
print(f"converted {n} cards -> {OUT_CARDS}/ and {OUT_CITED.name}", flush=True)

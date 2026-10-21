#!/usr/bin/env python3
"""Surgically fix the 4 unverified citations, grounding each to verbatim local text.
Backs up pre-fix cards; verifies each fix against data/docs/ before reporting.
Then run convert_single_to_app.py to propagate to the rendered cards.
"""
import json, re, shutil
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
BK = HERE / "data" / "cards_single_fix_backup"; BK.mkdir(exist_ok=True)

FIXES = {
    "BLA761166": [{"match": "38/51",
                   "quote": "There were 65 serious adverse events reported for 28/51 (54.9%) patients in the PEGINVERA study.",
                   "doc_id": "1d6baeba-13b9-5018-9cdd-32debc061249", "line": 162}],
    "NDA217026": [{"match": "Fast Track designation on June 4, 2013",
                   "quote": None, "doc_id": "4a9f06d6-042b-5bbc-8eaf-dba301abbdda", "line": 72}],
    "NDA213176": [{"match": "the UTX-TGR-205 trial",
                   "quote": "UTX-TGR-205, an open-label, multi-center, multi-cohort trial (NCT02793583) that included 69 patients with relapsed or refractory MZL.",
                   "doc_id": None, "line": 474}],
    "BLA761328": [{"match": "In Trial 04, in term and late preterm infants",
                   "quote": "the incidence of MA RSV LRTI was 12/994 (1.2%) in those who received nirsevimab and 25/496 (5.0%) in those who received placebo",
                   "doc_id": None, "line": 51}],
}

def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()
_oc = {}
def local(did):
    if did not in _oc:
        p = HERE / "data" / "docs" / f"{did}.txt"
        _oc[did] = norm(p.read_text()) if p.exists() else None
    return _oc[did]

for app, fixes in FIXES.items():
    fp = SRC / f"{app}.json"
    d = json.load(open(fp))
    shutil.copy(fp, BK / f"{app}.json")
    updated = []
    def apply(o):
        if isinstance(o, dict):
            for c in (o.get("citations") or []):
                for f in fixes:
                    if f["match"] in (c.get("quote") or ""):
                        if f["quote"] is not None: c["quote"] = f["quote"]
                        if f["doc_id"] is not None: c["doc_id"] = f["doc_id"]
                        if f["line"] is not None: c["line"] = f["line"]
                        updated.append(c)
            for v in o.values(): apply(v)
        elif isinstance(o, list):
            for v in o: apply(v)
    apply(d.get("card", {}))
    (fp).write_text(json.dumps(d, indent=2))
    p = fl = 0; fails = []
    for c in updated:
        doc = local(c.get("doc_id", "")); nq = norm(c.get("quote") or "")
        probe = nq[:60] if len(nq) >= 60 else nq
        if doc is not None and probe in doc: p += 1
        else: fl += 1; fails.append((c.get("doc_id", "")[:8], c.get("line"), (c.get("quote") or "")[:50]))
    print(f"{app}: updated {len(updated)} citation(s) | verify PASS={p} FAIL={fl}", flush=True)
    for x in fails:
        print(f"    FAIL doc={x[0]} L{x[1]}: {x[2]}", flush=True)
print("\ndone. backups in data/cards_single_fix_backup/", flush=True)

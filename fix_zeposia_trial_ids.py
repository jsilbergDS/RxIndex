#!/usr/bin/env python3
"""One-off fix (Oct 21 2026): Zeposia (NDA209899) trial names had the protocol ids swapped.

The card's trial data follow the LABEL's numbering -- Study 1: N=895, ARR 0.181 vs 0.350
(= SUNBEAM); Study 2: N=874, 24-month (= RADIANCE) -- and the label prints
"Study 1 (NCT02294058) and Study 2 (NCT02047734)". The review only says "RPC01-201B and
RPC01-301" (no order), and the build paired them backwards. ClinicalTrials.gov:
RPC01-301 = NCT02294058 (SUNBEAM); RPC01-201B = NCT02047734 (RADIANCE).
So only the protocol ids in trial_name.text change; citations, NCTs and data are untouched.
"""
import json
from pathlib import Path

p = Path(__file__).parent / "data" / "cards_single" / "NDA209899.json"
d = json.load(open(p))
tr = d["card"]["pivotal_trials"]
fix = {"Study 1 (RPC01-201B)": "Study 1 (RPC01-301, SUNBEAM)",
       "Study 2 (RPC01-301)": "Study 2 (RPC01-201B, RADIANCE)"}
for t in tr:
    old = t["trial_name"]["text"]
    if old in fix:
        t["trial_name"]["text"] = fix[old]
        t["trial_name"]["fix_note"] = ("Protocol id corrected Oct 2026: the review names RPC01-201B and RPC01-301 "
                                       "without order; ClinicalTrials.gov registers RPC01-301 as NCT02294058 (SUNBEAM) "
                                       "and RPC01-201B as NCT02047734 (RADIANCE), matching the label's Study 1/Study 2.")
        print(f"{old!r} -> {t['trial_name']['text']!r}")
p.write_text(json.dumps(d, indent=2))

#!/usr/bin/env python3
"""Scale the post-approval 'Changes since approval' data to ALL cards.

Phase 1: build_label_changes.build(app)  -> data/label_changes/<app>.json  (RMC timeline, paperclip)
Phase 2: build_changes.build(app,...)    -> data/changes/<app>.json        (Submissions.txt spine
         + RMC content + FDA label-PDF URL resolution for non-corpus supplements)

Network-bound (paperclip + fda.gov), light CPU. Run in background.
"""
import json, sys, time
from pathlib import Path
from tqdm import tqdm
import build_label_changes as BLC
import build_changes as BC

HERE = Path(__file__).parent
CARDS = HERE / "data" / "cards_new"
apps = sorted(p.stem for p in CARDS.glob("*.json"))
print(f"[{time.strftime('%H:%M:%S')}] {len(apps)} cards", flush=True)

# ---- Phase 1: label_changes (RMC timeline per drug) ----
LC_OUT = HERE / "data" / "label_changes"; LC_OUT.mkdir(parents=True, exist_ok=True)
lc_fail = []
for a in tqdm(apps, desc="label_changes", unit="drug"):
    try:
        r = BLC.build(a)
        (LC_OUT / f"{a}.json").write_text(json.dumps(r, indent=2))
    except Exception as e:
        lc_fail.append((a, repr(e)[:100]))
print(f"[{time.strftime('%H:%M:%S')}] label_changes done; {len(lc_fail)} failed", flush=True)
for a, e in lc_fail[:25]:
    print(f"  FAIL {a}: {e}", flush=True)

# ---- Phase 2: changes (Submissions.txt spine + URL resolution) ----
appnos = {str(json.load(open(CARDS / f"{a}.json"))["_meta"]["appno"]).lstrip("0") for a in apps}
subs, alldate = BC.load_subs(appnos)
CH_OUT = HERE / "data" / "changes"; CH_OUT.mkdir(parents=True, exist_ok=True)
ch_fail = []
n_blank = n_entries = 0
for a in tqdm(apps, desc="changes", unit="drug"):
    try:
        r = BC.build(a, subs, alldate)
        (CH_OUT / f"{a}.json").write_text(json.dumps(r, indent=2))
        n_entries += len(r["entries"])
    except Exception as e:
        ch_fail.append((a, repr(e)[:100]))
print(f"[{time.strftime('%H:%M:%S')}] changes done; {len(ch_fail)} failed; {n_entries} total entries", flush=True)
for a, e in ch_fail[:25]:
    print(f"  FAIL {a}: {e}", flush=True)
print(f"[{time.strftime('%H:%M:%S')}] ALL DONE", flush=True)

#!/usr/bin/env python3
"""Conditional AdComm stage. Runs the VERIFIED AdComm extraction (run_adcomm.py)
ONLY for cards whose advisory_committee.convened is True — i.e. drugs that actually
had an advisory committee meeting. For every other card it does nothing (the
single-agent build already carries the cited convened=False).

This is the reproducible way to handle AdComm: build sets convened (grounded);
this stage fills the rich verified detail (committee/date/vote + concern->change
linkages from the transcript) exactly when there is one to fill.

Output: adcomm_out/<APP>.json (loaded by app.py as ADCOMM_VERIFIED, preferred in
the card's Advisory-Committee row).

Usage: PYTHONUNBUFFERED=1 python3 run_adcomm_conditional.py [budget]
"""
import json, glob, subprocess, sys
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
BUDGET = sys.argv[1] if len(sys.argv) > 1 else "30"

convened = []
for f in sorted(glob.glob(str(SRC / "*.json"))):
    d = json.load(open(f))
    ac = d["card"].get("advisory_committee", {}) or {}
    if ac.get("convened") is True:
        m = d["_meta"]
        convened.append((f"{m['type']}{m['appno']}", m.get("drug", "")))

if not convened:
    print("No new cards convened an advisory committee — verified AdComm stage is a "
          "no-op (nothing to extract). Each card keeps its cited convened=False.", flush=True)
    sys.exit(0)

apps = [a for a, _ in convened]
print(f"{len(apps)} card(s) convened an AdComm: {[d for _, d in convened]}", flush=True)
print(f"Running verified AdComm extraction (run_adcomm.py) -> adcomm_out/ ...", flush=True)
subprocess.run(["python", "run_adcomm.py", "adcomm_out", BUDGET, *apps], cwd=str(HERE), check=False)
print("Done. app.py will render the verified AdComm detail for those cards.", flush=True)

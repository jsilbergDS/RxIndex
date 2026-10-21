#!/usr/bin/env python3
"""Definitive: which cards cite a doc the CRITIC cannot fetch from paperclip.
Uses the critic's EXACT code path (run_card_critic.pc + norm) — no total_lines proxy.
"""
import json, glob, re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import run_card_critic as C   # reuse the exact paperclip client + norm the critic uses

# cited docs per card
bycard = {}
for fp in glob.glob("data/cards_single/*.json"):
    app = fp.split("/")[-1][:-5]; docs = set()
    def w(o):
        if isinstance(o, dict):
            for c in (o.get("citations") or []):
                if c.get("doc_id"): docs.add(c["doc_id"])
            for v in o.values(): w(v)
        elif isinstance(o, list):
            for v in o: w(v)
    w(json.load(open(fp)).get("card", {})); bycard[app] = docs

alldocs = sorted({d for s in bycard.values() for d in s})

def fetch(d):
    try:
        return d, len(C.norm(C.pc.execute("cat", ["--full", f"/fda/{d}/content.lines"]).output))
    except Exception:
        return d, 0

with ThreadPoolExecutor(max_workers=10) as ex:
    lens = dict(ex.map(fetch, alldocs))

missing = {d for d, n in lens.items() if n < 20}   # critic gets empty/near-empty -> false flags
print(f"cited docs: {len(alldocs)} | UNFETCHABLE by critic (paperclip): {len(missing)}", flush=True)
for d in sorted(missing):
    print(f"    {d}", flush=True)

affected = {a: sorted(s & missing) for a, s in bycard.items() if s & missing}
print(f"\nCARDS CITING A MISSING DOC: {len(affected)}", flush=True)
for a in sorted(affected):
    print(f"  {a}: {len(affected[a])} missing doc(s) -> {affected[a]}", flush=True)

json.dump({"missing_docs": sorted(missing),
           "cards": {a: affected[a] for a in affected}},
          open("data/residuals/cards_with_missing_docs.json", "w"), indent=2)
print("\nsaved -> data/residuals/cards_with_missing_docs.json", flush=True)

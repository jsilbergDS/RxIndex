#!/usr/bin/env python3
"""Re-run only Phase 2 (data/changes/) after the doc_urls combined-filename fix."""
import json, time
from pathlib import Path
from tqdm import tqdm
import build_changes as BC

HERE = Path(__file__).parent
CARDS = HERE / "data" / "cards_new"
apps = sorted(p.stem for p in CARDS.glob("*.json"))
appnos = {str(json.load(open(CARDS / f"{a}.json"))["_meta"]["appno"]).lstrip("0") for a in apps}
subs, alldate = BC.load_subs(appnos)
OUT = HERE / "data" / "changes"; OUT.mkdir(parents=True, exist_ok=True)

resolved = fell_back = in_corpus = 0
fb_rows = []
for a in tqdm(apps, desc="changes", unit="drug"):
    r = BC.build(a, subs, alldate)
    (OUT / f"{a}.json").write_text(json.dumps(r, indent=2))
    for en in r["entries"]:
        for sd in en.get("suppl_docs", []):
            if sd["in_corpus"]:
                in_corpus += 1
            elif sd["resolves"]:
                resolved += 1
            else:
                fell_back += 1
                fb_rows.append(f"{a} S-{sd['num']} {en['date']} {sd['url'].split('/')[-1]}")
print(f"[{time.strftime('%H:%M:%S')}] DONE  in_corpus={in_corpus}  constructed_resolved={resolved}  fell_back={fell_back}", flush=True)
for row in fb_rows:
    print("  FB " + row, flush=True)

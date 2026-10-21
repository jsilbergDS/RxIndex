#!/usr/bin/env python3
"""Bundle the label docs referenced by data/changes/*.json safety_src/eff_src links
(the deployed app reads OCR from data/docs/, not paperclip). Same 'L<n>: text' format
as bundle_cited_docs.py. Idempotent.
"""
import json, glob, re, subprocess, time
from pathlib import Path
from tqdm import tqdm

HERE = Path(__file__).parent
DOCS = HERE / "data" / "docs"; DOCS.mkdir(parents=True, exist_ok=True)

refd = set()
for f in glob.glob(str(HERE / "data" / "changes" / "*.json")):
    d = json.load(open(f))
    for e in d["entries"]:
        for key in ("safety_src", "eff_src"):
            s = e.get(key)
            if s and s.get("doc_id"):
                refd.add(s["doc_id"])

missing = [d for d in sorted(refd) if not (DOCS / f"{d}.txt").exists()]
print(f"[{time.strftime('%H:%M:%S')}] referenced={len(refd)}  missing={len(missing)}", flush=True)

ok, fail = 0, []
for d in tqdm(missing, desc="bundle", unit="doc"):
    out = subprocess.run(["paperclip", "grep", "-nE", "^", f"/fda/{d}/content.lines"],
                         capture_output=True, text=True, timeout=120).stdout
    lines = out.split("\n")
    if not any(re.match(r"^L\d+:", ln) for ln in lines):
        fail.append(d); continue
    txt = "\n".join(re.sub(r"^(L\d+):", r"\1: ", ln) if re.match(r"^L\d+:", ln) else ln
                    for ln in lines)
    (DOCS / f"{d}.txt").write_text(txt)
    ok += 1

print(f"[{time.strftime('%H:%M:%S')}] bundled {ok}/{len(missing)}  failed={len(fail)}", flush=True)
for d in fail:
    print("  FAIL " + d, flush=True)

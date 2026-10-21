#!/usr/bin/env python3
"""Bundle every cited doc that isn't yet in data/docs/ by fetching its OCR from
paperclip (grep -nE '^' returns the full L<n>-prefixed text; cat truncates, don't use it).
Writes data/docs/<doc_id>.txt in the same 'L<n>: text' format the app parses.
Idempotent; safe to re-run. Belongs as a pipeline stage after convert.
Usage: python3 bundle_cited_docs.py
"""
import json, glob, re, subprocess
from pathlib import Path

HERE = Path(__file__).parent
DOCS = HERE / "data" / "docs"

cited = set()
for fp in glob.glob(str(HERE / "data" / "cards_single" / "*.json")):
    def w(o):
        if isinstance(o, dict):
            for c in (o.get("citations") or []):
                if c.get("doc_id"):
                    cited.add(c["doc_id"])
            for v in o.values():
                w(v)
        elif isinstance(o, list):
            for v in o:
                w(v)
    w(json.load(open(fp)).get("card", {}))

missing = [d for d in sorted(cited) if not (DOCS / f"{d}.txt").exists()]
print(f"cited docs: {len(cited)} | not yet bundled: {len(missing)}", flush=True)

ok, fail = 0, []
for d in missing:
    out = subprocess.run(["paperclip", "grep", "-nE", "^", f"/fda/{d}/content.lines"],
                         capture_output=True, text=True, timeout=120).stdout
    lines = out.split("\n")
    if not any(re.match(r"^L\d+:", ln) for ln in lines):     # error / not found -> skip
        fail.append(d); continue
    txt = "\n".join(re.sub(r"^(L\d+):", r"\1: ", ln) if re.match(r"^L\d+:", ln) else ln
                    for ln in lines)
    (DOCS / f"{d}.txt").write_text(txt)
    ok += 1
    print(f"  bundled {d}  ({sum(1 for ln in lines if re.match(r'^L\d+:', ln))} lines)", flush=True)

print(f"\nbundled {ok}/{len(missing)} | failed {len(fail)}: {fail}", flush=True)

#!/usr/bin/env python3
"""Bundle the OCR text of every doc cited by the verified AdComm extraction into
data/docs/<canonical>.txt, so the app's /doc OCR viewer can show them offline.
Canonical doc_id = strip a leading 'fda_' (matches the cards_recited full-UUID form)."""
import json, glob, re, subprocess
from pathlib import Path

FH = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project/flask_hf")
DOCS = FH / "data" / "docs"
PAPERCLIP = "/home/users/jsilberg/.local/bin/paperclip"

def canon(d):
    return d[4:] if d and d.startswith("fda_") else d

def variants(d):
    out = [d]
    if d.startswith("fda_"):
        body = d[4:].replace("-", "")
        out += [d[4:], f"fda_{body[:12]}", body]
    else:
        body = d.replace("-", "")
        out += [f"fda_{d}", f"fda_{body[:12]}", body]
    return list(dict.fromkeys(out))

# collect cited doc_ids
docids = set()
def walk(o):
    if isinstance(o, dict):
        if o.get("doc_id"): docids.add(o["doc_id"])
        for v in o.values(): walk(v)
    elif isinstance(o, list):
        for v in o: walk(v)
for f in glob.glob(str(FH / "adcomm_out" / "*.json")):
    walk(json.load(open(f)))

fetched = skipped = failed = 0
for d in sorted(docids):
    c = canon(d)
    dest = DOCS / f"{c}.txt"
    if dest.exists():
        skipped += 1; continue
    got = None
    for v in variants(d):
        try:
            r = subprocess.run([PAPERCLIP, "head", "-n", "6000", f"/fda/{v}/content.lines"],
                               capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        if re.search(r"^L\d+:", r.stdout, re.MULTILINE):
            got = r.stdout; break
    if got:
        dest.write_text(got); fetched += 1
        print(f"  fetched {c[:12]}  ({len(got)} bytes)", flush=True)
    else:
        failed += 1
        print(f"  FAILED  {d}", flush=True)

print(f"\nfetched {fetched}, already had {skipped}, failed {failed}")

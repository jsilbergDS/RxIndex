#!/usr/bin/env python3
"""Export the static artifacts the serving app needs so it runs with NO paperclip:
  - data/doc_labels.json            {canon_doc_id: friendly label}
  - data/ocr/<canon_doc_id>.lines   the doc's content.lines (L<n>: text) for cited docs

Run locally (paperclip available). The exported files ship in the repo and are read
by app.py's bundled-first OCR/label path.
"""
import os, sys, json, glob, subprocess, re
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", open(
    "/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip())
HERE = Path(__file__).parent
DATA = HERE / "data"
OCR_DIR = DATA / "ocr"
OCR_DIR.mkdir(parents=True, exist_ok=True)
PAPERCLIP = os.environ.get("PAPERCLIP_BIN", "/home/users/jsilberg/.local/bin/paperclip")

import app  # noqa: E402  (imports with API key set; brings canon_docid, precompute, cache)

# 1) collect every cited doc_id across value cards + recited overlay
doc_ids = set()
def walk(o):
    if isinstance(o, dict):
        for c in o.get("citations", []) or []:
            if c.get("doc_id"):
                doc_ids.add(app.canon_docid(c["doc_id"]))
        for v in o.values():
            walk(v)
    elif isinstance(o, list):
        for v in o:
            walk(v)
for f in glob.glob(str(DATA / "cards_new" / "*.json")):
    walk(json.load(open(f)))
for ln in open(DATA / "cards_recited_new.jsonl"):
    walk(json.loads(ln))
doc_ids = sorted(doc_ids)
print(f"cited docs to export: {len(doc_ids)}", flush=True)

# 2) doc labels — reuse the app's precompute, then dump the cache
app._precompute_doc_labels()
labels = dict(app._DOC_LABEL_CACHE)
# also make sure every cited doc has a label key (fallback to short id)
for did in doc_ids:
    labels.setdefault(did, did[:8] + "…")
(DATA / "doc_labels.json").write_text(json.dumps(labels, indent=0))
print(f"wrote doc_labels.json ({len(labels)} labels)", flush=True)

# 3) OCR content.lines for each cited doc
def fetch_ocr(did):
    body = did.replace("-", "")
    for v in (did, "fda_" + body[:12], body):
        try:
            r = subprocess.run([PAPERCLIP, "cat", "--full", f"/fda/{v}/content.lines"],
                               capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        if re.search(r"^L\d+:", r.stdout, re.MULTILINE):
            return r.stdout
    return None

done = miss = 0
for i, did in enumerate(doc_ids, 1):
    out = OCR_DIR / f"{did}.lines"
    if out.exists() and out.stat().st_size > 0:
        done += 1
        continue
    txt = fetch_ocr(did)
    if txt:
        out.write_text(txt)
        done += 1
    else:
        miss += 1
    if i % 50 == 0:
        print(f"  {i}/{len(doc_ids)} (ok={done}, miss={miss})", flush=True)
print(f"OCR export done: {done} written, {miss} missing", flush=True)
total_mb = sum(f.stat().st_size for f in OCR_DIR.glob('*.lines')) / 1e6
print(f"data/ocr size: {total_mb:.1f} MB", flush=True)

#!/usr/bin/env python3
"""Bake data/doc_labels.json: doc_id -> friendly source name (e.g. "Multidiscipline
Review · 2019", "Label · 2023") for EVERY doc cited by a rendered card, so citation
chips never fall back to a short id ("393a228f…"). Uses data/doc_meta.json (run
build_doc_meta.py first) and the app's own naming (_doc_display). No paperclip/API.

Usage: python3 build_doc_labels.py      (run after convert + bundling + build_doc_meta)
"""
import json, glob
from pathlib import Path

HERE = Path(__file__).parent
import app as A   # reuse _doc_display so chip names match the app exactly

meta = json.load(open(HERE / "data" / "doc_meta.json"))
old_p = HERE / "data" / "doc_labels.json"
old = json.load(open(old_p)) if old_p.exists() else {}

ids = set()
def grab(o):
    if isinstance(o, dict):
        for c in o.get("citations", []) or []:
            if isinstance(c, dict) and c.get("doc_id"):
                ids.add(c["doc_id"])
        for v in o.values(): grab(v)
    elif isinstance(o, list):
        for v in o: grab(v)
for f in glob.glob(str(HERE / "data" / "cards_new" / "*.json")):
    grab(json.load(open(f)))
# clinical-field citations live in the recited jsonl, not cards_new/
for p in ("cards_recited_new.jsonl", "cards_recited.jsonl"):
    fp = HERE / "data" / p
    if fp.exists():
        for ln in open(fp):
            if ln.strip(): grab(json.loads(ln))

labels, unnamed = {}, []
for d in sorted(ids):
    m = meta.get(d)
    if m and m.get("fname"):
        path = (f"/{m['year']}/" if m.get("year") else "/") + m["fname"]
        labels[d] = A._doc_display(path)
    elif d in old and not old[d].endswith("…"):
        labels[d] = old[d]          # keep a previously-good label
    else:
        unnamed.append(d)

old_p.write_text(json.dumps(labels, indent=0))
print(f"cited docs: {len(ids)} | named: {len(labels)} | still unnamed: {len(unnamed)} "
      f"{unnamed[:10]}", flush=True)

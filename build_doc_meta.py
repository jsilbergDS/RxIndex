#!/usr/bin/env python3
"""Bake data/doc_meta.json: for every bundled OCR doc (data/docs/<id>.txt), map
doc_id -> {fname, year, drug} from paperclip all_documents. fname carries the doc-type
suffix (e.g. ...MultidisciplineR.pdf) for type inference; year is the drugsatfda-path
year. Pulled via SQL substring so long URLs are NOT truncated by the table view.
The app uses this to label the OCR panel, with NO runtime paperclip dependency.
Usage: python3 build_doc_meta.py
"""
import json, re, subprocess
from pathlib import Path

HERE = Path(__file__).parent
ids = sorted(p.stem for p in (HERE / "data" / "docs").glob("*.txt"))
print(f"bundled docs: {len(ids)}", flush=True)

meta = {}
CHUNK = 120
for i in range(0, len(ids), CHUNK):
    chunk = ids[i:i + CHUNK]
    inlist = ",".join("'" + d + "'" for d in chunk)
    q = ("SELECT document_id, tradename, substring(public_url from '[^/]+\\.pdf') fn, "
         "substring(public_url from '/([0-9]{4})/') yr "
         f"FROM all_documents WHERE document_id IN ({inlist})")
    out = subprocess.run(["paperclip", "sql", "--source", "fda", q],
                         capture_output=True, text=True, timeout=120).stdout
    for ln in out.splitlines():
        p = [x.strip() for x in ln.split("|")]
        if len(p) == 4 and re.match(r"^[0-9a-f-]{36}$", p[0]) and p[2].lower().endswith(".pdf"):
            meta[p[0]] = {"fname": p[2], "year": (p[3] if p[3].isdigit() else ""),
                          "drug": (p[1] if p[1] and p[1] != "NULL" else "")}
    print(f"  {min(i+CHUNK,len(ids))}/{len(ids)} -> {len(meta)} mapped", flush=True)

(HERE / "data" / "doc_meta.json").write_text(json.dumps(meta, indent=0))
print(f"wrote data/doc_meta.json: {len(meta)}/{len(ids)} docs "
      f"({len(ids)-len(meta)} not found in paperclip)", flush=True)

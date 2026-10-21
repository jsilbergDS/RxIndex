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
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
all_ids = sorted(p.stem for p in (HERE / "data" / "docs").glob("*.txt"))
# Only full UUIDs: a single short-id filename (e.g. "01cd620e") makes the whole IN(...)
# batch fail with "invalid input syntax for type uuid" -- that silently unlabeled ~2/3
# of docs before (Oct 2026).
ids = [d for d in all_ids if _UUID.match(d)]
print(f"bundled docs: {len(all_ids)} ({len(all_ids) - len(ids)} non-UUID filenames skipped)", flush=True)

meta = {}
CHUNK = 120
failed_batches = 0
# filename from public_url, else from source_file (some docs have no URL; extensions can
# be upper-case ".PDF") -- missing either left e.g. Addyi's Summary Review unnamed.
SELECT = ("SELECT document_id, tradename, "
          "coalesce(substring(public_url from '[^/]+\\.[pP][dD][fF]'), "
          "substring(source_file from '[^/]+\\.[pP][dD][fF]')) fn, "
          "substring(coalesce(public_url, '') from '/([0-9]{4})/') yr FROM all_documents ")

def run(where):
    r = subprocess.run(["paperclip", "sql", "--source", "fda", SELECT + where],
                       capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0 and "SQL error" not in r.stdout
    rows = []
    for ln in r.stdout.splitlines():
        p = [x.strip() for x in ln.split("|")]
        if len(p) == 4 and re.match(r"^[0-9a-f-]{36}$", p[0]) and p[2].lower().endswith(".pdf"):
            rows.append((p[0], {"fname": p[2], "year": (p[3] if p[3].isdigit() else ""),
                                "drug": (p[1] if p[1] and p[1] != "NULL" else "")}))
    return ok, rows, (r.stdout + r.stderr).strip()[:200]

for i in range(0, len(ids), CHUNK):
    chunk = ids[i:i + CHUNK]
    ok, rows, err = run("WHERE document_id IN (" + ",".join("'" + d + "'" for d in chunk) + ")")
    if not ok:
        failed_batches += 1
        print(f"  BATCH FAILED at {i}: {err}", flush=True)
    meta.update(rows)
    print(f"  {min(i+CHUNK,len(ids))}/{len(ids)} -> {len(meta)} mapped", flush=True)

# Short-id files (8 hex chars, e.g. AdComm bundles cite "5a120c39"): resolve by prefix,
# store under the short id so citations that use it get a name too.
short = [d for d in all_ids if re.fullmatch(r"[0-9a-f]{8}", d)]
n_short = 0
for s in short:
    ok, rows, _ = run(f"WHERE document_id::text LIKE '{s}%'")
    if ok and len(rows) == 1:
        meta[s] = rows[0][1]; n_short += 1
print(f"short-id files resolved: {n_short}/{len(short)}", flush=True)

(HERE / "data" / "doc_meta.json").write_text(json.dumps(meta, indent=0))
print(f"wrote data/doc_meta.json: {len(meta)} entries ({sum(1 for d in ids if d in meta)}/{len(ids)} UUID docs, {n_short} short ids) "
      f"({sum(1 for d in ids if d not in meta)} UUID docs not found, {failed_batches} failed batches)", flush=True)

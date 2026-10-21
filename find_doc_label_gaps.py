#!/usr/bin/env python3
"""Enumerate the OCR-panel label gaps from the actually-rendered page:
  - RAW: citation doc_id with no metadata anywhere -> panel shows a bare doc-id.
  - GENERIC: doc mapped, but filename suffix didn't map to a specific type
    -> shows generic 'Review Document'.
Saves data/doc_label_gaps.json + DOC_LABEL_GAPS.md. Reads the live local page.
Usage: python3 find_doc_label_gaps.py [URL]   (default http://127.0.0.1:8700/)
"""
import json, re, sys, html, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8700/"
DOC_META = json.load(open(HERE / "data" / "doc_meta.json"))

page = urllib.request.urlopen(URL, timeout=15).read().decode("utf-8", "replace")
seen = {}
for m in re.finditer(r'data-pdf="([^"]*)"', page):
    try:
        d = json.loads(html.unescape(m.group(1)))
    except Exception:
        continue
    did, lab = d.get("doc_id"), d.get("label")
    if did and did not in seen:
        seen[did] = lab

raw, generic = [], []
for did, lab in sorted(seen.items()):
    if lab and re.match(r"^[0-9a-f]{8}", lab):                    # bare doc-id fallback
        raw.append({"doc_id": did, "label": lab,
                    "in_doc_meta": did in DOC_META,
                    "bundled_txt": (HERE / "data" / "docs" / f"{did}.txt").exists()})
    elif lab and "Review Document" in lab:                        # unmapped doc type
        dm = DOC_META.get(did, {})
        generic.append({"doc_id": did, "label": lab, "fname": dm.get("fname"),
                        "drug": dm.get("drug"), "year": dm.get("year")})

out = {"generic_review_document": generic, "raw_docid_fallback": raw,
       "counts": {"generic": len(generic), "raw": len(raw), "total_labels": len(seen)}}
(HERE / "data" / "doc_label_gaps.json").write_text(json.dumps(out, indent=2))

md = ["# OCR-panel label gaps",
      "",
      f"From the rendered RxIndex Cards page: **{len(seen)}** distinct citation labels.",
      "Two residual gaps in the `_doc_panel_label` enrichment (see how it's built in the",
      "chat / `build_doc_meta.py`). Regenerate with `find_doc_label_gaps.py`.",
      "",
      f"## Generic 'Review Document' ({len(generic)}) — mapped, but filename suffix didn't map to a specific type",
      "| doc_id | drug | year | filename | shown as |",
      "|---|---|---|---|---|"]
for g in generic:
    md.append(f"| `{g['doc_id']}` | {g['drug'] or '—'} | {g['year'] or '—'} | `{g['fname'] or '—'}` | {g['label']} |")
md += ["",
       f"## Raw doc-id fallback ({len(raw)}) — no metadata (not in paperclip / no bundled OCR match)",
       "| doc_id | in doc_meta | bundled .txt |",
       "|---|---|---|"]
for r in raw:
    md.append(f"| `{r['doc_id']}` | {r['in_doc_meta']} | {r['bundled_txt']} |")
(HERE / "DOC_LABEL_GAPS.md").write_text("\n".join(md) + "\n")

print(f"total labels: {len(seen)} | generic: {len(generic)} | raw: {len(raw)}", flush=True)
print("saved: data/doc_label_gaps.json  and  DOC_LABEL_GAPS.md", flush=True)

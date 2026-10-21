#!/usr/bin/env python3
"""Regenerate and durably save the PRE-FIX unverified-quote list from the pre-fix card
backups (data/cards_single_fix_backup/), so the history isn't lost after the fixes.
Usage: python3 save_prefix_residuals.py <STAMP>
"""
import json, glob, re, os, sys
from pathlib import Path

HERE = Path(__file__).parent
BK = HERE / "data" / "cards_single_fix_backup"
OUT = HERE / "data" / "residuals"; OUT.mkdir(exist_ok=True)
STAMP = sys.argv[1] if len(sys.argv) > 1 else "prefix"

def norm(s): return re.sub(r"\s+", " ", (s or "")).strip().lower()
_oc = {}
def ocr(did):
    if did not in _oc:
        p = HERE / "data" / "docs" / f"{did}.txt"
        _oc[did] = norm(p.read_text()) if p.exists() else None
    return _oc[did]

FIX_NOTE = {
    "BLA761166": "BESREMI: quote said 38/51, doc says 28/51 (54.9%) at L162 -> corrected number + line.",
    "NDA217026": "DAYBUE: quote verbatim but cited wrong doc (77a23e88); real source is 4a9f06d6 L72 -> corrected doc_id.",
    "NDA213176": "UKONIQ: benign paraphrase ('the ... trial' vs 'study ...') -> quote set to verbatim (L474).",
    "BLA761328": "BEYFORTUS: numbers correct (12/994, 25/496) at L51; failed only on unicode >= vs LaTeX \\ge -> quote set to verbatim substring.",
}

rows = []
for fp in sorted(glob.glob(str(BK / "*.json"))):
    app = Path(fp).stem
    d = json.load(open(fp))
    drug = d.get("_meta", {}).get("drug", "")
    def w(o):
        if isinstance(o, dict):
            for c in (o.get("citations") or []):
                q = c.get("quote"); did = c.get("doc_id", "")
                if not q: continue
                doc = ocr(did); nq = norm(q); probe = nq[:60] if len(nq) >= 60 else nq
                if doc is None or probe not in doc:
                    rows.append({"app": app, "drug": drug, "doc_id": did, "line": c.get("line"),
                                 "reason": "no_local_file" if doc is None else "quote_not_found",
                                 "quote": q, "fix_applied": FIX_NOTE.get(app, "")})
            for v in o.values(): w(v)
        elif isinstance(o, list):
            for v in o: w(v)
    w(d.get("card", {}))

(OUT / f"prefix_unverified_quotes_{STAMP}.json").write_text(json.dumps(rows, indent=2))

md = [f"# Pre-fix unverified quotes ({STAMP})", "",
      f"Regenerated from `data/cards_single_fix_backup/` (the pre-fix originals of the 4 edited cards).",
      f"**{len(rows)} unverified citation(s)** — all FIXED (see fix_applied). Backups keep the originals.", ""]
for r in rows:
    md.append(f"## {r['drug']} ({r['app']}) — doc `{r['doc_id'][:8]}` L{r['line']} [{r['reason']}]")
    md.append(f"- PRE-FIX QUOTE: \"{r['quote']}\"")
    md.append(f"- FIX: {r['fix_applied']}")
    md.append("")
(OUT / f"prefix_unverified_quotes_{STAMP}.md").write_text("\n".join(md))

print(f"saved {len(rows)} pre-fix unverified quotes:", flush=True)
print(f"  {OUT}/prefix_unverified_quotes_{STAMP}.json", flush=True)
print(f"  {OUT}/prefix_unverified_quotes_{STAMP}.md", flush=True)
for r in rows:
    print(f"  {r['drug']} ({r['app']}) L{r['line']}: \"{r['quote'][:60]}\"", flush=True)

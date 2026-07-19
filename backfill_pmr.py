#!/usr/bin/env python3
"""Targeted PMR/PMC (+ CRL) backfill for cards built BEFORE the post_marketing field
existed. Reads ONLY the approval letter (which enumerates numbered PMRs/PMCs), extracts
with the proven schema, verifies quotes against the letter OCR, and merges
post_marketing (+ crl_history) into the EXISTING card JSON. No card regeneration.

Usage: PYTHONUNBUFFERED=1 python3 backfill_pmr.py [APP ...]   (default: all not-extracted)
"""
import os, sys, json, re, time
from pathlib import Path
import run_card_single as B          # reuse discover_docs, doc_lines, client, MODEL, pc

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
PRICE_IN, PRICE_OUT = 15.0, 75.0

PROMPT = """You are an FDA-approval-letter extractor. Below is the FULL text of ONE FDA
Approval Letter (lines prefixed L<n>:). Extract ONLY post-marketing requirements/
commitments and any prior-CRL history, each anchored to a verbatim quote from a line.

Return STRICT JSON only:
{
  "post_marketing": [
    {"type":"PMR"|"PMC","requirement":{"text":"...","citations":[{"quote":"<verbatim line text>","doc_id":"__LETTER_DOCID__","line":<L number>}]}}
  ],
  "crl_history": {"prior_crl": true|false|null,
                  "date": {"text":"...","citations":[{"quote":"...","doc_id":"__LETTER_DOCID__","line":<n>}]} or null,
                  "basis": {"text":"...","citations":[{"quote":"...","doc_id":"__LETTER_DOCID__","line":<n>}]} or null}
}
RULES:
- PMR = post-marketing REQUIREMENT (required study, e.g. 505(o) / Subpart H/E confirmatory).
  PMC = post-marketing COMMITMENT (agreed study). These are NOT REMS — exclude REMS.
- One entry per numbered requirement; keep FDA's own numbering/wording in `text`.
- Every `quote` copied EXACTLY from one L<n> line (grep-verified; non-matching quotes are dropped).
- If the letter lists NO PMRs/PMCs, return "post_marketing": [] (definitive 'none stated').
- crl_history.prior_crl true ONLY if the letter references a prior Complete Response Letter
  for THIS application; otherwise false or null.
- doc_id of EVERY citation MUST be exactly: __LETTER_DOCID__
"""

def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()

def pick_letter(app):
    """Return (doc_id, ocr_lines_text) for the ORIGINAL approval letter, or (None,None)."""
    docs = [d for d in B.discover_docs(app) if d["type"] == "AppLetter"]
    if not docs:
        return None, None
    # prefer the original submission (…Orig1s000…ltr / …s000…ltr)
    def rank(d):
        u = d["url"].lower()
        return (0 if ("orig1s000" in u or "s000ltr" in u) else
                1 if re.search(r"s0?0?1ltr", u) else 2)
    docs.sort(key=rank)
    did = docs[0]["doc_id"]
    lines = B.doc_lines(did)
    text = "\n".join(f"L{ln}: {t}" for ln, t in lines)
    return did, text

def verify(obj, letter_did, ocr_norm):
    """Drop citations whose quote isn't in the letter OCR; force correct doc_id."""
    def fix_leaf(leaf):
        if not isinstance(leaf, dict):
            return leaf
        good = []
        for c in leaf.get("citations", []) or []:
            c["doc_id"] = letter_did
            q = norm(c.get("quote", ""))
            if q and q[:50] in ocr_norm:
                good.append(c)
        leaf["citations"] = good
        return leaf
    for pm in obj.get("post_marketing", []) or []:
        pm["requirement"] = fix_leaf(pm.get("requirement"))
    crl = obj.get("crl_history") or {}
    for k in ("date", "basis"):
        if isinstance(crl.get(k), dict):
            crl[k] = fix_leaf(crl[k])
    return obj

def backfill_one(app):
    fp = SRC / f"{app}.json"
    d = json.load(open(fp))
    card = d["card"]
    if "post_marketing" in card:
        return "skip(already has key)", 0.0, d["_meta"].get("drug", "")
    letter_did, letter_txt = pick_letter(app)
    if not letter_did:
        return "NO-LETTER (left un-extracted)", 0.0, d["_meta"].get("drug", "")
    prompt = PROMPT.replace("__LETTER_DOCID__", letter_did)
    msg = prompt + "\n\n=== APPROVAL LETTER (doc_id " + letter_did + ") ===\n" + letter_txt[:120000]
    resp = B.client.messages.create(model=B.MODEL, max_tokens=8000,
                                    messages=[{"role": "user", "content": msg}])
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    cost = resp.usage.input_tokens/1e6*PRICE_IN + resp.usage.output_tokens/1e6*PRICE_OUT
    if not m:
        return f"PARSE-FAIL (left un-extracted)", cost, d["_meta"].get("drug", "")
    try:
        obj = json.loads(m.group(0), strict=False)   # tolerate control chars in quotes
    except Exception as e:
        return f"JSON-ERR {e} (left un-extracted)", cost, d["_meta"].get("drug", "")
    obj = verify(obj, letter_did, norm(letter_txt))
    pm = obj.get("post_marketing", [])
    # merge into the existing card (only these two fields)
    card["post_marketing"] = pm
    if "crl_history" not in card:
        card["crl_history"] = obj.get("crl_history") or {}
    d["card"] = card
    fp.write_text(json.dumps(d, indent=2))
    n_pmr = sum(1 for x in pm if (x.get("type") or "").upper() == "PMR")
    n_pmc = sum(1 for x in pm if (x.get("type") or "").upper() == "PMC")
    return f"OK: {n_pmr} PMR, {n_pmc} PMC", cost, d["_meta"].get("drug", "")

def main():
    if len(sys.argv) > 1:
        apps = sys.argv[1:]
    else:
        apps = [Path(f).stem for f in sorted(SRC.glob("*.json"))
                if "post_marketing" not in json.load(open(f)).get("card", {})]
    total = 0.0
    for i, app in enumerate(apps, 1):
        t0 = time.time()
        try:
            status, cost, drug = backfill_one(app)
        except Exception as e:
            status, cost, drug = f"ERROR {type(e).__name__}: {e}", 0.0, app
        total += cost
        print(f"[{i}/{len(apps)}] {drug[:16]:<16} {app:<12} {status:<28} ${cost:.3f} cum=${total:.2f} {time.time()-t0:.0f}s", flush=True)
    print(f"\nbackfill done: {len(apps)} cards, total ${total:.2f}", flush=True)

if __name__ == "__main__":
    main()

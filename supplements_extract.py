#!/usr/bin/env python3
"""Populate the INDICATION EXPANSIONS / EFFICACY SUPPLEMENTS section.

For each drug, enumerate approved EFFICACY (class-2) supplements from Drugs@FDA
Submissions.txt (deterministic: number + date), locate that supplement's docs in
paperclip (review / letter / label matched by supplement number), read them, and
extract a ONE-LINE CITED description of the new indication/use. Merge
card['supplements'] into the existing card JSON. No card regeneration.

Usage: PYTHONUNBUFFERED=1 python3 supplements_extract.py [APP ...]   (default: all with efficacy suppls)
"""
import os, sys, json, re, csv, time
from pathlib import Path
import run_card_single as B

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
SUBM = "/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda/fresh_20260714/Submissions.txt"

PROMPT = """You are an FDA supplement extractor. Below are the FDA documents for ONE approved
EFFICACY SUPPLEMENT (a post-approval application that changed/expanded the drug's approved use).
Lines are prefixed L<n>:. Extract ONE concise, factual description of WHAT USE/INDICATION this
supplement added or changed — anchored to a verbatim quote.

Return STRICT JSON only:
{"description": {"text": "<one line: e.g. 'Expanded to include pediatric patients 6-17 years' or 'Added combination with pembrolizumab for first-line NSCLC' or 'Converted accelerated approval to full approval'>",
                 "citations": [{"quote":"<verbatim line text>","doc_id":"__DOCID__","line":<L number>}]}}
RULES:
- Describe the CHANGE this supplement made (new population / new line of therapy / new combination /
  new indication / accelerated->full conversion), NOT the drug's original indication.
- `text` is ONE line, factual, no marketing.
- The quote MUST be copied EXACTLY from an L<n> line (grep-verified; non-matching quotes dropped).
- doc_id of the citation MUST be one of the doc_ids provided below.
- If the documents do not state what changed, return {"description": null}.
"""

def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()

def efficacy_supps(apps_numeric):
    """appno(numeric) -> [ {num, date} ] approved efficacy (class-2) supplements."""
    out = {}
    for r in csv.DictReader(open(SUBM, encoding="latin-1"), delimiter="\t"):
        if r["SubmissionType"] == "SUPPL" and r["SubmissionStatus"] == "AP" \
                and r.get("SubmissionClassCodeID") == "2":
            an = r["ApplNo"].lstrip("0")
            if an in apps_numeric:
                out.setdefault(an, []).append(
                    {"num": int(r["SubmissionNo"]), "date": (r.get("SubmissionStatusDate") or "")[:10]})
    for an in out:
        out[an].sort(key=lambda x: x["num"])
    return out

def docs_for_supp(app, num):
    """Docs whose filename carries this supplement number, grouped by kind."""
    kinds = {"review": None, "letter": None, "label": None}
    for d in B.discover_docs(app):
        fn = d["url"].rsplit("/", 1)[-1].lower()
        m = re.search(r"s0*(\d+)", fn)               # supplement number in filename
        if not m or int(m.group(1)) != num:
            continue
        t = d["type"]
        if t in ("MultidisciplineR", "IntegratedR", "MedR", "SummaryR") and not kinds["review"]:
            kinds["review"] = d
        elif t == "AppLetter" and not kinds["letter"]:
            kinds["letter"] = d
        elif t == "Label" and not kinds["label"]:
            kinds["label"] = d
    return kinds

def read_doc(d, cap=60000):
    if not d:
        return "", None
    lines = B.doc_lines(d["doc_id"])
    return "\n".join(f"L{ln}: {t}" for ln, t in lines)[:cap], d["doc_id"]

def extract_desc(app, num):
    """Read the supplement's review/letter, extract cited 'what changed'."""
    kinds = docs_for_supp(app, num)
    # prefer review, then letter, then label as the description source
    src = kinds["review"] or kinds["letter"] or kinds["label"]
    if not src:
        return None, kinds, 0.0
    txt, did = read_doc(src)
    if not txt.strip():
        return None, kinds, 0.0
    prompt = PROMPT.replace("__DOCID__", did)
    msg = prompt + f"\n\n=== SUPPLEMENT s{num:03d} DOCS (doc_id {did}) ===\n" + txt
    resp = B.client.messages.create(model=B.MODEL, max_tokens=1500,
                                    messages=[{"role": "user", "content": msg}])
    cost = resp.usage.input_tokens/1e6*15.0 + resp.usage.output_tokens/1e6*75.0
    t = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        return None, kinds, cost
    try:
        desc = json.loads(m.group(0), strict=False).get("description")
    except Exception:
        return None, kinds, cost
    # verify quote(s)
    if isinstance(desc, dict):
        ocr = norm(txt)
        good = [c for c in (desc.get("citations") or [])
                if norm(c.get("quote", ""))[:50] in ocr]
        for c in good:
            c["doc_id"] = did
        desc["citations"] = good
        if not good:
            desc = None
    return desc, kinds, cost

def url_of(d): return d["url"] if d else None

def process_app(app, supps):
    fp = SRC / f"{app}.json"
    d = json.load(open(fp))
    card = d["card"]
    out = []
    total = 0.0
    for s in supps:
        desc, kinds, cost = extract_desc(app, s["num"])
        total += cost
        out.append({
            "submission_number": s["num"], "class_code": "EFFICACY",
            "approval_date": s["date"], "description": desc,
            "review_url": url_of(kinds["review"]), "letter_url": url_of(kinds["letter"]),
            "label_url": url_of(kinds["label"]),
            "review_doc_id": (kinds["review"] or {}).get("doc_id") if kinds["review"] else None,
        })
    card["supplements"] = out
    d["card"] = card
    fp.write_text(json.dumps(d, indent=2))
    n_desc = sum(1 for x in out if x["description"])
    return f"{len(out)} suppl ({n_desc} described)", total, d["_meta"].get("drug", "")

def main():
    apps_arg = sys.argv[1:]
    all_num = {os.path.basename(f)[:-5][3:].lstrip("0"): os.path.basename(f)[:-5]
               for f in SRC.glob("*.json")}       # numeric -> APPKEY(with prefix)
    eff = efficacy_supps(set(all_num))
    if apps_arg:
        targets = [(a, eff.get(a[3:].lstrip("0"), [])) for a in apps_arg]
    else:
        targets = [(all_num[an], eff[an]) for an in eff]     # only drugs WITH efficacy supps
    total = 0.0
    for i, (app, supps) in enumerate(targets, 1):
        if not supps:
            # deterministic truth: no efficacy supplements
            fp = SRC / f"{app}.json"; d = json.load(open(fp)); d["card"]["supplements"] = []
            fp.write_text(json.dumps(d, indent=2))
            print(f"[{i}/{len(targets)}] {app:<12} 0 efficacy supplements", flush=True)
            continue
        t0 = time.time()
        try:
            status, cost, drug = process_app(app, supps)
        except Exception as e:
            status, cost, drug = f"ERROR {type(e).__name__}: {e}", 0.0, app
        total += cost
        print(f"[{i}/{len(targets)}] {drug[:16]:<16} {app:<12} {status:<24} ${cost:.3f} cum=${total:.2f} {time.time()-t0:.0f}s", flush=True)
    print(f"\nsupplements done: {len(targets)} drugs, total ${total:.2f}", flush=True)

if __name__ == "__main__":
    main()

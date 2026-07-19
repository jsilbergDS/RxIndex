#!/usr/bin/env python3
"""Design A (base pass): the ORIGINAL pipeline's approach — feed the WHOLE
Integrated/Multidiscipline Review to Opus 4.8 in ONE call and extract the full
card schema as VALUES (no citations; citations were a separate pass in the
original pipeline). Same schema/fields as design B so we can compare values.

Usage: PYTHONUNBUFFERED=1 python3 run_card_wholereview.py <APP> [<APP> ...]
Output: data/cards_wholereview/<APP>.json  (+ per-card cost)
"""
import os, sys, json, re, time, csv
from pathlib import Path
sys.path.insert(0, '/home/users/jsilberg/.paperclip/lib')
from gxl_paperclip.client import PaperclipClient
import anthropic

HERE = Path(__file__).parent
OUT_DIR = HERE / "data" / "cards_wholereview"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FRESH = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda/fresh_20260714")
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY)
pc = PaperclipClient.from_env()
MODEL = "claude-opus-4-8"
PRICE_IN, PRICE_OUT = 15.0, 75.0
UUID_RE = re.compile(r'^[0-9a-f-]{30,}$')
INTEGRATED_PRIORITY = ['MultidisciplineR', 'IntegratedR', 'MedR']

PROMPT = """You are an FDA regulatory analyst. Read the ENTIRE FDA review below and extract a structured drug card. Return ONLY JSON (no prose), this exact schema, null when the review does not state it — NEVER invent:

{{
  "pivotal_trials": [{{"trial_name":"...","nct":"NCT... or null","phase":"...","design":"...","population":"...","endpoint":"the prespecified primary endpoint","n":null or int,"result":"HR/p/ORR/mPFS numbers","met":true|false|null}}],
  "indication": "approved indication (one sentence)",
  "target_moa": "mechanism of action",
  "modality": "small molecule | mAb | ADC | CAR-T | gene therapy | peptide | oligonucleotide | enzyme | radiopharmaceutical | bispecific | other",
  "key_safety": "most common / most serious AEs with rates",
  "boxed_warning": {{"present":true|false,"detail":"... or null"}},
  "rems": {{"present":true|false,"detail":"... or null"}},
  "companion_dx": {{"present":true|false,"detail":"named test + biomarker or null"}},
  "advisory_committee": {{"convened":true|false|null,"vote":"e.g. 8-3 or null"}},
  "first_cycle_approval": {{"value":true|false|null,"detail":"basis or null"}},
  "approval_program": {{
    "accelerated_approval": {{"present":true|false,"surrogate_endpoint":"... or null","confirmatory_requirement":"required confirmatory trial or null"}},
    "breakthrough_therapy": {{"present":true|false,"basis":"... or null"}},
    "fast_track": {{"present":true|false,"basis":"... or null"}},
    "orphan_drug": {{"present":true|false,"orphan_indication":"... or null"}},
    "priority_review": {{"present":true|false,"basis":"... or null"}},
    "qidp": {{"present":true|false,"basis":"... or null"}},
    "rmat": {{"present":true|false,"basis":"... or null"}},
    "summary": "1-2 sentence regulatory-pathway narrative"
  }}
}}

Rules: only JSON; never invent; `present` true only if the review states that designation was granted for THIS drug (not a comparator, not the disease being rare). For accelerated approval give the surrogate endpoint + confirmatory requirement.

Drug: {drug} ({active}) — Application {app}, Sponsor {sponsor}
"""

def load_meta(app):
    def rd(f): return csv.DictReader(open(FRESH / f, encoding="latin-1"), delimiter="\t")
    n = app[3:]; m = {"type": app[:3], "appno": n}
    for r in rd("Applications.txt"):
        if r["ApplNo"] == n: m["sponsor"] = r.get("SponsorName", ""); break
    for r in rd("Products.txt"):
        if r["ApplNo"] == n: m["drug"] = r.get("DrugName", ""); m["active"] = r.get("ActiveIngredient", ""); break
    for r in rd("Submissions.txt"):
        if r["ApplNo"] == n and r.get("SubmissionType") == "ORIG" and r.get("SubmissionStatus") == "AP":
            m["approval_date"] = (r.get("SubmissionStatusDate") or "")[:10]
            m["review_priority"] = r.get("ReviewPriority", ""); break
    return m

def discover_integrated(app):
    digits = app[3:].lstrip('0') or '0'
    sql = (f"SELECT document_id, SUBSTRING(public_url FROM 1 FOR 60), SUBSTRING(public_url FROM 61 FOR 120), "
           f"SUBSTRING(source_file FROM 1 FOR 120) FROM documents WHERE public_url ILIKE '%{digits}%' OR source_file ILIKE '%{digits}%' LIMIT 400")
    r = pc.sql(sql, source='fda')
    by = {t: [] for t in INTEGRATED_PRIORITY}
    for line in r.output.split('\n'):
        p = [x.strip() for x in line.split('|')]
        if len(p) != 4 or not UUID_RE.match(p[0]): continue
        bn = ((p[1]+p[2]) or p[3]).rsplit('/', 1)[-1].lower()
        if not bn.endswith('.pdf') or digits not in (p[1]+p[2]+p[3]): continue
        stem = bn[:-4]
        if stem.endswith('multidiscipliner'): by['MultidisciplineR'].append(p[0])
        elif stem.endswith('integratedr'): by['IntegratedR'].append(p[0])
        elif stem.endswith('medr') or stem.endswith('medrev'): by['MedR'].append(p[0])
    for t in INTEGRATED_PRIORITY:
        if by[t]: return t, by[t][0]
    return None, None

def strip_lines(txt):
    return "\n".join(re.sub(r"^L\d+:\s?", "", ln) for ln in txt.split("\n"))

def run_one(app):
    meta = load_meta(app); drug = meta.get("drug", "?")
    rt, did = discover_integrated(app)
    if not did: return {"app": app, "error": "no integrated review", "success": False}
    doc = strip_lines(pc.execute('cat', ['--full', f'/fda/{did}/content.lines']).output)
    user = PROMPT.format(drug=drug, active=meta.get("active","?"), app=app, sponsor=meta.get("sponsor","?")) + "\n\n=== FDA REVIEW ===\n" + doc
    resp = client.messages.create(model=MODEL, max_tokens=4000, messages=[{"role": "user", "content": user}])
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"): text = text.rsplit("```", 1)[0]
        if text.startswith("json"): text = text[4:].strip()
    try: card = json.loads(text)
    except Exception as e: card = {"_parse_error": str(e), "_raw": text[:400]}
    cost = resp.usage.input_tokens/1e6*PRICE_IN + resp.usage.output_tokens/1e6*PRICE_OUT
    out = {"app_key": app, "_meta": meta, "card": card, "_model": MODEL,
           "_source_review": rt, "_source_doc": did,
           "_tokens": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}}
    (OUT_DIR / f"{app}.json").write_text(json.dumps(out, indent=2))
    return {"app": app, "drug": drug, "success": "_parse_error" not in card,
            "cost_usd": round(cost, 4), "tokens_in": resp.usage.input_tokens, "src": rt}

def main():
    apps = sys.argv[1:]
    if not apps: print(__doc__); sys.exit(1)
    print(f"WHOLE-REVIEW (design A base, Opus 4.8) build of {len(apps)} cards\n", flush=True)
    total = 0.0
    for i, app in enumerate(apps, 1):
        print(f"--- [{i}/{len(apps)}] {app} ---", flush=True)
        t0 = time.time(); r = run_one(app); total += r.get("cost_usd", 0)
        print(f"  done {time.time()-t0:.0f}s  cost=${r.get('cost_usd',0):.3f}  ok={r.get('success')}  "
              f"src={r.get('src')}  in_tok={r.get('tokens_in','?')}", flush=True)
        if r.get("error"): print(f"  ERROR: {r['error']}", flush=True)
    print(f"\n=== total ${total:.2f} for {len(apps)} ({total/max(len(apps),1):.2f}/card) ===", flush=True)

if __name__ == "__main__":
    main()

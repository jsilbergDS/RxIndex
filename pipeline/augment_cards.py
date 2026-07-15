#!/usr/bin/env python3
"""Augment drug-card JSONL with:
  1. indication_details (structured parse of verbatim indication, via Sonnet)
  2. supplements (interesting AP'd supplements from openFDA — efficacy / new-indication)

In-place augmentation of an existing cards JSONL. Skips work already done.

Usage:
  python augment_cards.py <cards.jsonl>
"""
import os, sys, json, time, re, urllib.request
import anthropic

if len(sys.argv) < 2: print(__doc__); sys.exit(1)
PATH = sys.argv[1]
MODEL = "claude-sonnet-4-6"
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY)

# --- 1. Indication structuring ---
IND_PROMPT = """You parse FDA-approved indication text into structured fields.

Indication text:
\"\"\"
{indication}
\"\"\"

Return JSON with EXACTLY these keys (use null when not specified):
{{
  "line_of_treatment": "first-line / second-line+ / refractory or relapsed / after specific prior therapy / not specified",
  "setting": "metastatic / locally advanced / advanced / unresectable / adjuvant / neoadjuvant / non-metastatic / not applicable",
  "population_restrictions": "key biomarker/age/subgroup restrictions, e.g. 'BRCA-mutated', 'adults', 'pediatric ≥6 yr', or 'none'",
  "combination": "monotherapy / in combination with X (specify) / not specified",
  "prior_therapy_required": "what prior therapy is required, or 'none'"
}}

Be concise. Quote specific language from the indication when relevant. Output JSON only, no commentary."""

def parse_indication(text):
    if not text or len(text) < 10: return None
    msg = client.messages.create(model=MODEL, max_tokens=400,
        messages=[{"role":"user","content": IND_PROMPT.format(indication=text)}])
    raw = msg.content[0].text.strip()
    # strip code fences if present
    raw = re.sub(r'^```(?:json)?\s*', '', raw); raw = re.sub(r'\s*```$', '', raw)
    try: return json.loads(raw)
    except json.JSONDecodeError: return {"_parse_error": raw[:300]}

# --- 2. Supplements (openFDA) ---
INTERESTING_CLASSES = {"EFFICACY", "EFFICACY-S", "TYPE 6", "TYPE 4", "TYPE 5"}
# Class descriptions to also accept (case-insensitive substring match):
INTERESTING_DESC_KEYWORDS = ("efficacy", "new indication", "new use", "labeling - efficacy")

def fetch_supplements(app):
    """app like 'NDA208712' or 'BLA761204'. Return list of approved interesting supplements."""
    url = f"https://api.fda.gov/drug/drugsfda.json?search=application_number:{app}&limit=1"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.load(r)
    except Exception as e:
        return [{"_error": str(e)[:200]}]
    out = []
    for rec in data.get("results", []):
        for s in rec.get("submissions", []):
            if s.get("submission_type") != "SUPPL": continue
            if s.get("submission_status") != "AP": continue
            cc = (s.get("submission_class_code") or "").upper()
            cd = (s.get("submission_class_code_description") or "")
            keep = (cc in INTERESTING_CLASSES) or any(kw in cd.lower() for kw in INTERESTING_DESC_KEYWORDS)
            if not keep: continue
            docs = s.get("application_docs", []) or []
            letter_url = next((d.get("url") for d in docs if (d.get("type") or "").lower() == "letter"), None)
            label_url  = next((d.get("url") for d in docs if (d.get("type") or "").lower() == "label"),  None)
            review_url = next((d.get("url") for d in docs if (d.get("type") or "").lower() == "review"), None)
            out.append({
                "submission_number": s.get("submission_number"),
                "class_code": cc, "class_description": cd,
                "approval_date": s.get("submission_status_date"),
                "letter_url": letter_url, "label_url": label_url, "review_url": review_url,
            })
    out.sort(key=lambda x: x.get("approval_date") or "")
    return out

# --- Main ---
cards = [json.loads(line) for line in open(PATH)]
print(f"Loaded {len(cards)} cards", flush=True)

n_ind_new = n_supp_new = 0
out_lines = []
for i, d in enumerate(cards, 1):
    m = d.get("_meta", {})
    app = f"{m.get('type','')}{m.get('appno','')}"
    drug = m.get("drug","?")

    if "indication_details" not in d:
        d["indication_details"] = parse_indication(m.get("indication",""))
        n_ind_new += 1

    if "supplements" not in d:
        d["supplements"] = fetch_supplements(app)
        n_supp_new += 1

    n_supp = len(d.get("supplements") or [])
    print(f"  [{i}/{len(cards)}] {app:<12} {drug:<22} ind={'OK' if d.get('indication_details') else '-'} supp={n_supp}", flush=True)
    out_lines.append(json.dumps(d))
    time.sleep(0.2)  # gentle on openFDA

with open(PATH + ".tmp", "w") as f: f.write("\n".join(out_lines) + "\n")
os.replace(PATH + ".tmp", PATH)
print(f"\nAugmented {n_ind_new} indications, {n_supp_new} supplements lookups -> {PATH}", flush=True)

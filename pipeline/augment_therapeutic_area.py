#!/usr/bin/env python3
"""Add therapeutic_area tag to each drug card via Sonnet.

Uses indication + target_moa + modality to assign a single therapeutic area
from a standardized taxonomy.

Usage:
  python augment_therapeutic_area.py <cards.jsonl>
"""
import os, sys, json, time, re
import anthropic

if len(sys.argv) < 2: print(__doc__); sys.exit(1)
PATH = sys.argv[1]
MODEL = "claude-sonnet-4-6"
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY)

# Standardized taxonomy
TA_OPTIONS = [
    "Oncology",
    "Neurology",
    "Cardiology",
    "Hematology",
    "Infectious Disease",
    "Endocrinology / Diabetes",
    "Rheumatology / Immunology",
    "Dermatology",
    "Gastroenterology",
    "Psychiatry",
    "Pulmonology",
    "Ophthalmology",
    "Imaging / Diagnostic",
    "Genetic / Rare Disease",
    "Women's Health",
    "Other",
]

PROMPT = """Classify the FDA-approved drug into ONE therapeutic area from this fixed list:
{options}

Drug: {drug}
Active ingredient: {active}
Indication: {indication}
Target / MoA: {target_moa}
Modality: {modality}

Use the most specific applicable category. For rare metabolic / genetic diseases (lysosomal storage, etc.), prefer "Genetic / Rare Disease". For solid tumors and hematologic malignancies, use "Oncology". For monoclonal antibodies targeting cytokines in inflammatory conditions, use "Rheumatology / Immunology" (or "Dermatology" if specifically skin).

Output ONLY the chosen category name from the list above, nothing else."""

def classify(card):
    m = card.get('_meta', {})
    drug = m.get('drug') or m.get('active') or '?'
    prompt = PROMPT.format(
        options='\n'.join(f"- {o}" for o in TA_OPTIONS),
        drug=drug,
        active=m.get('active',''),
        indication=(m.get('indication') or '')[:600],
        target_moa=card.get('target_moa','') or '',
        modality=card.get('modality','') or '',
    )
    msg = client.messages.create(model=MODEL, max_tokens=30,
        messages=[{"role":"user","content":prompt}])
    out = msg.content[0].text.strip()
    out = re.sub(r'^[-* ]+', '', out).strip()
    # Snap to closest known option (in case Sonnet adds punctuation)
    for o in TA_OPTIONS:
        if out.lower() == o.lower(): return o
    for o in TA_OPTIONS:
        if o.lower() in out.lower(): return o
    return "Other"

cards = [json.loads(line) for line in open(PATH)]
print(f"Loaded {len(cards)} cards", flush=True)
n = 0
for i, d in enumerate(cards, 1):
    if d.get('therapeutic_area'): continue
    ta = classify(d)
    d['therapeutic_area'] = ta
    n += 1
    print(f"  [{i}/{len(cards)}] {d['_meta'].get('drug','?'):<22} -> {ta}", flush=True)
    time.sleep(0.15)

with open(PATH + ".tmp", "w") as f: f.write("\n".join(json.dumps(d) for d in cards) + "\n")
os.replace(PATH + ".tmp", PATH)
print(f"\nTagged {n} cards with therapeutic_area")

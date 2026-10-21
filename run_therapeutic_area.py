#!/usr/bin/env python3
"""Pipeline stage: assign therapeutic_area to each card from a FIXED taxonomy,
based on its indication + target_moa + modality (same approach as the original
augment_therapeutic_area.py, upgraded to Opus). Writes therapeutic_area into
data/cards_single/<APP>.json.

Run this AFTER the heal loop (so repair doesn't drop the tag), BEFORE convert.
Usage: PYTHONUNBUFFERED=1 python3 run_therapeutic_area.py [APP ...]
"""
import json, sys, re, glob
from pathlib import Path
import anthropic

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY, timeout=120.0, max_retries=3)
MODEL = "claude-opus-4-8"

TA_OPTIONS = [
    "Oncology", "Neurology", "Cardiology", "Hematology", "Infectious Disease",
    "Endocrinology / Diabetes", "Rheumatology / Immunology", "Dermatology",
    "Gastroenterology", "Psychiatry", "Pulmonology", "Ophthalmology",
    "Imaging / Diagnostic", "Genetic / Rare Disease", "Women's Health", "Other",
]

PROMPT = """Classify the FDA-approved drug into ONE therapeutic area from this fixed list:
{options}

Drug: {drug}
Active ingredient: {active}
Indication: {indication}
Target / MoA: {moa}
Modality: {modality}

Use the most specific applicable category. For rare metabolic / genetic diseases (lysosomal storage, etc.), prefer "Genetic / Rare Disease". For solid tumors and hematologic malignancies, use "Oncology". For monoclonal antibodies targeting cytokines in inflammatory conditions, use "Rheumatology / Immunology" (or "Dermatology" if specifically skin).

Output ONLY the chosen category name from the list above, nothing else."""

def t(f):
    return f.get("text", f.get("value", f)) if isinstance(f, dict) else f

def classify(card, meta):
    ind = t(card.get("indication")) or meta.get("indication") or ""
    prompt = PROMPT.format(
        options="\n".join(f"- {o}" for o in TA_OPTIONS),
        drug=meta.get("drug", "?"), active=meta.get("active", ""),
        indication=str(ind)[:600], moa=str(t(card.get("target_moa")) or ""),
        modality=str(t(card.get("modality")) or ""))
    out = client.messages.create(model=MODEL, max_tokens=30,
                                 messages=[{"role": "user", "content": prompt}]).content[0].text.strip()
    out = re.sub(r"^[-* ]+", "", out).strip()
    return out if out in TA_OPTIONS else "Other"

def main():
    apps = sys.argv[1:] or [Path(f).stem for f in sorted(glob.glob(str(SRC / "*.json")))]
    for app in apps:
        p = SRC / f"{app}.json"
        if not p.exists():
            print(f"  SKIP {app}: no card file", flush=True); continue
        d = json.load(open(p))
        if not isinstance(d.get("card"), dict):   # e.g. build stored invalid JSON as a str (r189 BLA125409)
            print(f"  SKIP {app}: card is {type(d.get('card')).__name__}, not a dict", flush=True); continue
        ta = classify(d["card"], d.get("_meta", {}))
        d["card"]["therapeutic_area"] = ta
        (SRC / f"{app}.json").write_text(json.dumps(d, indent=2))
        print(f"  {d['_meta'].get('drug', '')[:16]:<16} -> {ta}", flush=True)

if __name__ == "__main__":
    main()

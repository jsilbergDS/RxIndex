#!/usr/bin/env python3
"""Extract AdCom discussion detail from FDA review docs (SumR/ODMemo/MedR).

For each card with advisory_committee.convened=True and no advisory_committee_detail
(or one with no linkages from the AdComm Explorer dataset), pull the SumR's
Advisory Committee section and extract key concerns + discussion themes.

Usage:
  python augment_adcom_discussion.py <cards.jsonl>
"""
import os, sys, json, re, time
sys.path.insert(0, '/home/users/jsilberg/.paperclip/lib')
from gxl_paperclip.client import PaperclipClient
import anthropic

if len(sys.argv) < 2: print(__doc__); sys.exit(1)
PATH = sys.argv[1]
MODEL = "claude-sonnet-4-6"
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY)
pc = PaperclipClient.from_env()

PROMPT = """You are reading the FDA review's Advisory Committee Meeting section for {drug}.

Extract structured discussion detail. Output JSON only:
{{
  "meeting_date": "...",
  "committee_name": "...",
  "vote": "...",
  "discussion_summary": "2-4 sentence summary of what the AC discussed and how they voted",
  "linkages": [
    {{"concern": "...", "change": "...", "category": "Safety / Efficacy / Population / Labeling / Dosing / Other"}}
  ]
}}

For "linkages":
- Each item should be ONE concrete concern raised by the AC PAIRED with the resulting label/protocol/PMR change (or "none" if no documented change).
- Quote concern language when possible.
- Include 1-4 linkages (whatever is documented).
- Use empty list [] if the AC's discussion is captured but no specific concern→change pairs can be derived.

REVIEW TEXT (Advisory Committee section + surrounding context):
{text}"""

def find_review_docs(app):
    digits = re.sub(r'^[A-Z]+', '', app).lstrip('0') or '0'
    sql = (
        "SELECT document_id, "
        "SUBSTRING(public_url FROM 1 FOR 50), "
        "SUBSTRING(public_url FROM 51 FOR 100) "
        "FROM documents WHERE public_url ILIKE '%" + digits + "%' "
        "AND (public_url ILIKE '%SumR%' OR public_url ILIKE '%ODMemo%' OR public_url ILIKE '%MedR%' OR public_url ILIKE '%MultidisciplineR%' OR public_url ILIKE '%IntegratedR%') "
        "AND public_url NOT ILIKE '%/label/%' AND public_url NOT ILIKE '%appletter%' "
        "LIMIT 30"
    )
    r = pc.sql(sql, source='fda')
    UUID_RE = re.compile(r'^[0-9a-f-]{30,}$')
    PRIORITY = {'SumR':0, 'MultidisciplineR':1, 'IntegratedR':2, 'ODMemo':3, 'MedR':4}
    found = []
    for line in r.output.split('\n'):
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 3: continue
        doc_id = parts[0]
        if not UUID_RE.match(doc_id): continue
        url = (parts[1]+parts[2]).strip()
        for kw, p in PRIORITY.items():
            if kw in url:
                found.append((p, doc_id, kw)); break
    found.sort()
    return found

def grep_ac_section(text, max_chars=18000):
    """Find lines around 'Advisory Committee' section."""
    lines = text.split('\n')
    keep = set()
    pattern = re.compile(r'advisory committee|advisory commi|AC vote|AC meeting|AC discussion|committee voted|panel voted', re.I)
    for i, line in enumerate(lines):
        if pattern.search(line):
            for j in range(max(0, i-5), min(len(lines), i+80)):
                keep.add(j)
    if not keep: return text[:max_chars]
    out = '\n'.join(lines[i] for i in sorted(keep))[:max_chars]
    if len(out) < 200: return text[:max_chars]
    return out

def extract_discussion(card):
    m = card.get('_meta', {})
    app = f"{m.get('type','')}{m.get('appno','')}"
    drug = m.get('drug', '?')
    docs = find_review_docs(app)
    for _, doc_id, kw in docs[:3]:  # try up to 3 docs in priority order
        try:
            r = pc.execute("cat", ["--full", f"/fda/{doc_id}/content.lines"])
        except Exception:
            continue
        text = re.sub(r'^\d+\s*\|\s*', '', r.output, flags=re.M)
        ac_text = grep_ac_section(text)
        if len(ac_text) < 300: continue
        msg = client.messages.create(model=MODEL, max_tokens=2000,
            messages=[{"role":"user","content": PROMPT.format(drug=drug, text=ac_text)}])
        raw = msg.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw); raw = re.sub(r'\s*```$', '', raw)
        try:
            d = json.loads(raw)
            d['source'] = f"extracted from {kw}"
            return d
        except json.JSONDecodeError:
            continue
    return {'_error': 'no extractable AC discussion found'}

cards = [json.loads(l) for l in open(PATH)]
n = 0
for c in cards:
    ac = c.get('advisory_committee') or {}
    if not ac.get('convened'): continue
    existing = c.get('advisory_committee_detail') or {}
    # Skip drugs already enriched with linkages from the AdComm Explorer dataset
    if existing.get('source') == 'adcomm_explorer_dataset' and existing.get('linkages'):
        continue
    drug = c['_meta'].get('drug', '?')
    print(f"Extracting AC discussion for {drug}...", flush=True)
    d = extract_discussion(c)
    if d.get('_error'):
        print(f"  -> error: {d['_error']}", flush=True); continue
    # Merge into existing detail (preserve fields)
    new_detail = {**existing, **{k: v for k, v in d.items() if v}}
    c['advisory_committee_detail'] = new_detail
    n += 1
    nlinks = len(new_detail.get('linkages') or [])
    print(f"  -> source={new_detail.get('source')} linkages={nlinks} summary={(new_detail.get('discussion_summary') or '')[:120]}", flush=True)
    time.sleep(0.2)

with open(PATH+'.tmp','w') as f: f.write('\n'.join(json.dumps(d) for d in cards) + '\n')
os.replace(PATH+'.tmp', PATH)
print(f"\nEnriched {n} cards with AC discussion detail")

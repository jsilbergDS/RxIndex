#!/usr/bin/env python3
"""Enrich YES-flagged card fields with detailed text from FDA labels (via paperclip).

For each card, when any of these are True, fetch the original (Orig1s000) label
from paperclip and extract:
  - boxed_warning.full_text       (the actual boxed warning paragraph)
  - rems.detail                   (the REMS elements/rationale)
  - companion_dx.detail           (the named test + biomarker)

Stores into the existing fields as new sub-keys, so the original `present`/`content`
short summaries remain untouched.

Usage:
  python augment_yes_flags.py <cards.jsonl>
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

PROMPT = """You are reading the original FDA label (prescribing information) for {drug}. Extract the following from this label text. Use null if a field is not present. Output JSON only.

Fields to extract:
- boxed_warning_text: the FULL text of the BOXED WARNING (also called "Black Box Warning"). Quote verbatim, but you can break long warnings into the key bullet points. Include the title. Use null if the label has no boxed warning.
- rems_detail: a 1-3 sentence summary of the REMS — what elements (ETASU? communication plan?), what risk it mitigates, what monitoring is required. Use null if no REMS.
- companion_dx_detail: name of the companion diagnostic test (e.g. "cobas EGFR Mutation Test v2"), the biomarker tested (e.g. "EGFR exon 19 deletion or L858R mutation"), and the indication link. Use null if no companion Dx required.

Output JSON with keys exactly:
{{"boxed_warning_text": "...", "rems_detail": "...", "companion_dx_detail": "..."}}

LABEL TEXT (excerpts):
{text}"""

def find_orig_label_doc(app):
    digits = re.sub(r'^[A-Z]+', '', app).lstrip('0') or '0'
    # Prefer Orig1s000 label (original approval)
    sql = (
        "SELECT document_id, "
        "SUBSTRING(public_url FROM 1 FOR 50), "
        "SUBSTRING(public_url FROM 51 FOR 100) "
        "FROM documents "
        f"WHERE public_url ILIKE '%{digits}%' AND public_url ILIKE '%/label/%' "
        "LIMIT 50"
    )
    r = pc.sql(sql, source='fda')
    UUID_RE = re.compile(r'^[0-9a-f-]{30,}$')
    candidates = []
    for line in r.output.split('\n'):
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 3: continue
        doc_id = parts[0]
        if not UUID_RE.match(doc_id): continue
        url = (parts[1]+parts[2]).strip()
        # Prefer Orig1s000 (original approval) labels
        score = 0
        if 'orig1s000' in url.lower(): score -= 100
        if 'lbl.pdf' in url.lower(): score -= 10
        candidates.append((score, doc_id, url))
    if not candidates: return None
    candidates.sort()
    return candidates[0]

def extract_yes_detail(card):
    m = card.get('_meta', {})
    app = f"{m.get('type','')}{m.get('appno','')}"
    drug = m.get('drug', '?')
    found = find_orig_label_doc(app)
    if not found:
        return {'_error': 'no label doc found'}
    _, doc_id, url = found
    try:
        r = pc.execute("cat", ["--full", f"/fda/{doc_id}/content.lines"])
    except Exception as e:
        return {'_error': f'cat error: {e}'}
    text = re.sub(r'^\d+\s*\|\s*', '', r.output, flags=re.M)
    # Truncate label text - look for first 12000 chars (covers boxed warning + indications + REMS section)
    text = text[:12000]
    msg = client.messages.create(model=MODEL, max_tokens=1500,
        messages=[{"role":"user","content": PROMPT.format(drug=drug, text=text)}])
    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw); raw = re.sub(r'\s*```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {'_parse_error': raw[:400]}

cards = [json.loads(l) for l in open(PATH)]
n = 0
for c in cards:
    cdx = c.get('companion_dx')
    rems = c.get('rems') or {}
    bxw = c.get('boxed_warning') or {}
    needs = (cdx is True) or rems.get('present') or bxw.get('present')
    if not needs: continue
    # Skip if already enriched
    if any(k.endswith('_detail') for k in (rems.keys() | bxw.keys())) and bxw.get('full_text'):
        continue
    drug = c['_meta'].get('drug', '?')
    print(f"Enriching {drug}...", flush=True)
    detail = extract_yes_detail(c)
    if detail.get('boxed_warning_text') and bxw.get('present'):
        bxw['full_text'] = detail['boxed_warning_text']
        c['boxed_warning'] = bxw
    if detail.get('rems_detail') and rems.get('present'):
        rems['detail'] = detail['rems_detail']
        c['rems'] = rems
    if detail.get('companion_dx_detail') and cdx is True:
        c['companion_dx_detail'] = detail['companion_dx_detail']
    n += 1
    print(f"  -> boxed:{bool(bxw.get('full_text'))} rems_detail:{bool(rems.get('detail'))} cdx_detail:{bool(c.get('companion_dx_detail'))}", flush=True)
    time.sleep(0.2)

with open(PATH+'.tmp','w') as f: f.write('\n'.join(json.dumps(d) for d in cards) + '\n')
os.replace(PATH+'.tmp', PATH)
print(f"\nEnriched {n} cards")

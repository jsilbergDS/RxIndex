#!/usr/bin/env python3
"""Add a one-line description to each supplement using paperclip for content.

For each supplement, find the approval letter in paperclip by matching the
filename pattern (e.g. 208065Orig1s006ltr.pdf), read its OCR'd text via
paperclip's cat, and ask Sonnet what changed (new indication/population/dose).

Usage:
  python augment_supplements_paperclip.py <cards.jsonl>
"""
import os, sys, json, time, re
sys.path.insert(0, '/home/users/jsilberg/.paperclip/lib')
from gxl_paperclip.client import PaperclipClient
import anthropic

if len(sys.argv) < 2: print(__doc__); sys.exit(1)
PATH = sys.argv[1]
MODEL = "claude-sonnet-4-6"
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY)
pc = PaperclipClient.from_env()

PROMPT = """You are reading an FDA supplemental approval letter. In 1-2 sentences, describe what changed with this supplement — specifically the new indication, new population, new dose, or new use that was approved. Quote specific population or setting language when possible. If the letter just refers to a labeling change without a substantive efficacy/safety/use change, say so.

LETTER TEXT:
{text}

Output one line only, no preamble. Start with a verb if possible, e.g. "Expanded indication to include...", "Added pediatric population...", "Added 200 mg dose...", "Updated labeling only..."."""

# Cache: app -> {basename: document_id} from one paperclip query per app
LETTER_DOCS = {}

def discover_letters_for_app(app):
    """Return dict basename->document_id for all letter docs (ltr pattern) of this app."""
    if app in LETTER_DOCS: return LETTER_DOCS[app]
    digits = re.sub(r'^[A-Z]+', '', app).lstrip('0') or '0'
    sql = (
        "SELECT document_id, "
        "SUBSTRING(public_url FROM 1 FOR 50), "
        "SUBSTRING(public_url FROM 51 FOR 50), "
        "SUBSTRING(public_url FROM 101 FOR 150) "
        "FROM documents "
        f"WHERE public_url ILIKE '%{digits}%ltr.pdf' "
        "AND public_url ILIKE '%appletter%' "
        "LIMIT 200"
    )
    try:
        r = pc.sql(sql, source='fda')
    except Exception as e:
        print(f"  [discover err] {app}: {e}", flush=True)
        LETTER_DOCS[app] = {}; return {}
    out = {}
    UUID_RE = re.compile(r'^[0-9a-f-]{30,}$')
    for line in r.output.split('\n'):
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 4: continue
        doc_id = parts[0]
        if not UUID_RE.match(doc_id): continue
        url = (parts[1] + parts[2] + parts[3]).strip()
        if not url: continue
        bn = url.rsplit('/', 1)[-1].lower()
        out[bn] = doc_id
    LETTER_DOCS[app] = out
    return out

def read_letter(doc_id, max_chars=8000):
    try:
        r = pc.execute("cat", ["--full", f"/fda/{doc_id}/content.lines"])
    except Exception as e:
        return None, f"paperclip cat error: {e}"
    text = r.output if hasattr(r, 'output') else str(r)
    # content.lines format: one line per OCR line, sometimes with a leading line number
    # Strip line number prefixes
    cleaned = re.sub(r'^\d+\s*\|\s*', '', text, flags=re.M)
    return cleaned[:max_chars], None

def describe(letter_url, app):
    if not letter_url: return None
    bn = letter_url.rsplit('/', 1)[-1].lower()
    docs = discover_letters_for_app(app)
    doc_id = docs.get(bn)
    if not doc_id:
        return f"(letter not in paperclip index: {bn})"
    text, err = read_letter(doc_id)
    if err: return f"(read error: {err})"
    if not text or len(text) < 100: return "(letter text empty)"
    try:
        msg = client.messages.create(model=MODEL, max_tokens=200,
            messages=[{"role":"user","content": PROMPT.format(text=text)}])
        return msg.content[0].text.strip()
    except Exception as e:
        return f"(Sonnet error: {e})"

# --- Main ---
cards = [json.loads(line) for line in open(PATH)]
print(f"Loaded {len(cards)} cards", flush=True)

# Reset existing (placeholder) descriptions so we redo with paperclip
n_redone = 0
for d in cards:
    for s in d.get("supplements") or []:
        if s.get("description") and "(could not read letter" in s.get("description",""):
            s["description"] = None  # force redo

for i, d in enumerate(cards, 1):
    m = d.get("_meta", {})
    app = f"{m.get('type','')}{m.get('appno','')}"
    drug = m.get("drug","?")
    supps = d.get("supplements") or []
    for s in supps:
        if s.get("description"): continue
        s["description"] = describe(s.get("letter_url"), app)
        n_redone += 1
        print(f"  [{i}/{len(cards)}] {drug:<22} S-{s.get('submission_number')} -> {(s['description'] or '')[:100]}", flush=True)
        time.sleep(0.2)

with open(PATH + ".tmp", "w") as f: f.write("\n".join(json.dumps(d) for d in cards) + "\n")
os.replace(PATH + ".tmp", PATH)
print(f"\nUpdated {n_redone} supplement descriptions via paperclip -> {PATH}")

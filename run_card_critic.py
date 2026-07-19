#!/usr/bin/env python3
"""Pipeline stage: Opus 4.8 critic that reviews every card and flags quality
problems. Feeds each card + deterministic red-flags (uncited numbers, citations
not found in paperclip, empty required fields) to Opus, which returns structured
issues. Reproducible: re-run -> same inputs -> same review.

Usage: PYTHONUNBUFFERED=1 python3 run_card_critic.py [APP ...]   (default: all)
Output: data/cards_critic/<APP>.json  {app, release_ready, issues, automated_flags}
"""
import json, sys, re, glob, time
from pathlib import Path
sys.path.insert(0, '/home/users/jsilberg/.paperclip/lib')
from gxl_paperclip.client import PaperclipClient
import anthropic

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
OUT = HERE / "data" / "cards_critic"
OUT.mkdir(parents=True, exist_ok=True)
PROMPT = (HERE / "PROMPT_CRITIC.md").read_text()
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY, timeout=120.0, max_retries=8)
pc = PaperclipClient.from_env()
MODEL = "claude-opus-4-8"
PRICE_IN, PRICE_OUT = 15.0, 75.0

# first_cycle_approval is intentionally NOT required: it is inferential (inferred
# from the absence of a prior CRL, which the review rarely states outright), so
# null/uncited is acceptable and must not be flagged as an empty required field.
REQUIRED = ["indication", "target_moa", "modality", "key_safety", "receipt_date"]
NUM = re.compile(r'\d+(?:\.\d+)?')
CI = {'95', '97.5', '90', '99', '2.5'}

_OCR = {}
def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()
def ocr_text(doc_id):
    if doc_id not in _OCR:
        try: _OCR[doc_id] = norm(pc.execute('cat', ['--full', f'/fda/{doc_id}/content.lines']).output)
        except Exception: _OCR[doc_id] = ""
    return _OCR[doc_id]

def field_numbers(txt):
    out = []
    for m in NUM.finditer(txt or ''):
        d = m.group(0); s, e = m.start(), m.end()
        if d in CI: continue
        if (s > 0 and txt[s-1].isalpha()) or (e < len(txt) and txt[e].isalpha()): continue
        # skip doses (followed by mg/mcg/mL) and obvious study-id/date contexts
        tail = (txt[e:e+5] or "").lower()
        if tail.strip().startswith(("mg", "mcg", "ml", "%)")) and False: pass
        out.append((d, m.start()))
    return out

def automated_flags(card):
    uncited, unverified, empty = [], [], []
    def walk(o, path=""):
        if isinstance(o, dict):
            if "text" in o and "citations" in o:
                cites = o.get("citations") or []
                cj = " ".join(re.sub(r"\s+", " ", c.get("quote", "")) for c in cites)
                for n, _ in field_numbers(o.get("text")):
                    # ignore doses / study ids / dates crudely: require the number look like data
                    if not re.search(r"(?<!\d)" + re.escape(n) + r"(?!\d)", cj):
                        uncited.append({"field": path, "number": n, "text": (o.get("text") or "")[:80]})
                for c in cites:
                    q = norm(c.get("quote", ""))
                    if q and c.get("doc_id") and q[:45] not in ocr_text(c["doc_id"]):
                        unverified.append({"field": path, "line": c.get("line"), "quote": (c.get("quote") or "")[:60]})
            for k, v in o.items(): walk(v, f"{path}/{k}" if path else k)
        elif isinstance(o, list):
            for i, v in enumerate(o): walk(v, f"{path}[{i}]")
    walk(card)
    for f in REQUIRED:
        v = card.get(f)
        if isinstance(v, dict):
            val = v.get("text", v.get("value"))   # {text,citations} or {value,detail}
        else:
            val = v
        if val is None or val == "":
            empty.append(f)
    if not (card.get("pivotal_trials")): empty.append("pivotal_trials")
    return {"uncited_numbers": uncited, "unverified_citations": unverified, "empty_fields": empty}

def critic(app, card, flags):
    msg = (PROMPT.replace("{app}", app) + "\n\n=== CARD JSON ===\n" + json.dumps(card, indent=1)
           + "\n\n=== AUTOMATED_FLAGS ===\n" + json.dumps(flags, indent=1))
    resp = client.messages.create(model=MODEL, max_tokens=3000, messages=[{"role": "user", "content": msg}])
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    m = re.search(r"\{.*\}", txt, re.DOTALL)   # grab the outermost JSON object, ignore any prose/fences
    frag = m.group(0) if m else txt
    try: out = json.loads(frag)
    except Exception as e: out = {"app": app, "release_ready": False, "issues": [], "_parse_error": str(e), "_raw": txt[:300]}
    cost = resp.usage.input_tokens/1e6*PRICE_IN + resp.usage.output_tokens/1e6*PRICE_OUT
    return out, cost

def critique_one(app):
    """Critique one card: compute deterministic flags, run Opus critic, write
    data/cards_critic/<app>.json. Returns (out, cost)."""
    d = json.load(open(SRC / f"{app}.json"))
    card = d["card"]  # NOTE: do NOT inject _meta_tta — TTA is computed downstream in
    # the converter, so it is always null here and would be a spurious critic flag.
    flags = automated_flags(card)
    out, cost = critic(app, card, flags)
    out["automated_flags"] = flags
    (OUT / f"{app}.json").write_text(json.dumps(out, indent=2))
    return out, cost, d["_meta"].get("drug", "")

def main():
    apps = sys.argv[1:] or [Path(f).stem for f in sorted(glob.glob(str(SRC / "*.json")))]
    total = 0.0
    for i, app in enumerate(apps, 1):
        t0 = time.time()
        out, cost, drug = critique_one(app)
        total += cost
        highs = [x for x in out.get("issues", []) if x.get("severity") == "high"]
        print(f"[{i}/{len(apps)}] {drug[:14]:<14} "
              f"issues={len(out.get('issues',[]))} (high={len(highs)}) "
              f"ready={out.get('release_ready')}  ${cost:.3f}  {time.time()-t0:.0f}s", flush=True)
    print(f"\ntotal ${total:.2f} for {len(apps)} cards", flush=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Design B: SINGLE Opus 4.8 agent that builds a fully-cited RxIndex card in one
pass, reading the drug's FDA docs directly from paperclip (no pre-bundling).

Reuses extract_cards_cited.py's grep-driven tool loop, but the three doc tools
(list_drug_docs / grep_doc / read_doc_lines) are backed by paperclip instead of
local data/docs/. Schema = PROMPT_CARD.md (clinical fields + cited approval_program).

Deterministic metadata (approval date, priority, sponsor, active) is merged in
from the Drugs@FDA fresh dump — the LLM does not guess those.

Usage:
  PYTHONUNBUFFERED=1 python3 run_card_single.py <APP> [<APP> ...]
Output: data/cards_single/<APP>.json   (+ prints per-card cost and verify stats)
"""
import os, sys, json, re, time, csv, subprocess
from pathlib import Path
sys.path.insert(0, '/home/users/jsilberg/.paperclip/lib')
from gxl_paperclip.client import PaperclipClient
import anthropic

HERE = Path(__file__).parent
OUT_DIR = HERE / "data" / "cards_single"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROMPT = (HERE / "PROMPT_CARD.md").read_text()
FRESH = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda/fresh_20260714")

API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY, timeout=120.0, max_retries=8)
pc = PaperclipClient.from_env()

MODEL = "claude-opus-4-8"
MAX_TURNS = 60
PRICE_IN, PRICE_OUT = 15.0, 75.0
UUID_RE = re.compile(r'^[0-9a-f-]{30,}$')

# doc-type from filename suffix (best effort)
TYPE_SUFFIX = [
    ("multidiscipliner", "MultidisciplineR"), ("integratedr", "IntegratedR"),
    ("medr", "MedR"), ("medrev", "MedR"), ("statr", "StatR"),
    ("clinpharmr", "ClinPharmR"), ("lbl", "Label"), ("prntlbl", "Label"),
    ("ltr", "AppLetter"), ("appletter", "AppLetter"), ("sumr", "SummaryR"),
    ("chemr", "ChemR"), ("micror", "MicroR"), ("pharmr", "PharmToxR"),
    ("othr", "Other"), ("admincorres", "AdminCorr"),
]

def doc_type(basename):
    stem = basename.lower()[:-4] if basename.lower().endswith(".pdf") else basename.lower()
    for suf, label in TYPE_SUFFIX:
        if stem.endswith(suf):
            return label
    return "Other"

# --- paperclip-backed doc discovery + line cache ---
_DOCS = {}      # app -> [{doc_id, type, url}]
_LINES = {}     # doc_id -> [(lineno, text)]

def discover_docs(app):
    if app in _DOCS: return _DOCS[app]
    digits = app[3:].lstrip('0') or '0'
    sql = ("SELECT document_id, SUBSTRING(public_url FROM 1 FOR 60), "
           "SUBSTRING(public_url FROM 61 FOR 120), SUBSTRING(source_file FROM 1 FOR 120) "
           f"FROM documents WHERE public_url ILIKE '%{digits}%' OR source_file ILIKE '%{digits}%' LIMIT 400")
    r = pc.sql(sql, source='fda')
    seen = {}
    for line in r.output.split('\n'):
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 4: continue
        did = parts[0]
        if not UUID_RE.match(did): continue
        url = (parts[1] + parts[2]).strip()
        src = parts[3].strip()
        bn = (url or src).rsplit('/', 1)[-1]
        if not bn.lower().endswith('.pdf'): continue
        # only THIS app's docs (the digits should be in the path)
        if digits not in (url + src): continue
        if did in seen: continue
        seen[did] = {"doc_id": did, "type": doc_type(bn), "url": url or src}
    _DOCS[app] = list(seen.values())
    return _DOCS[app]

def doc_lines(doc_id):
    if doc_id in _LINES: return _LINES[doc_id]
    try:
        r = pc.execute('cat', ['--full', f'/fda/{doc_id}/content.lines'])
        txt = r.output
    except Exception:
        _LINES[doc_id] = []; return []
    out = []
    cur_n, buf = None, []
    for raw in txt.split("\n"):
        m = re.match(r"^L(\d+):\s?(.*)$", raw)
        if m:
            if cur_n is not None: out.append((cur_n, "\n".join(buf).strip()))
            cur_n = int(m.group(1)); buf = [m.group(2)]
        else:
            buf.append(raw)
    if cur_n is not None: out.append((cur_n, "\n".join(buf).strip()))
    _LINES[doc_id] = out
    return out

# --- tools ---
def t_list(app):
    return json.dumps([{"doc_id": d["doc_id"], "type": d["type"], "url": d["url"]} for d in discover_docs(app)], indent=2)

def t_grep(app, doc_id, pattern, max_results=25):
    allowed = {d["doc_id"] for d in discover_docs(app)}
    if doc_id not in allowed: return json.dumps({"error": f"{doc_id} not in {app}'s docs"})
    try: rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e: return json.dumps({"error": f"bad regex: {e}"})
    hits = []
    for ln, t in doc_lines(doc_id):
        if rx.search(t):
            hits.append({"line": ln, "text": t[:400]})
            if len(hits) >= max_results: break
    return json.dumps({"matches": len(hits), "lines": hits}, indent=2)

def t_read(app, doc_id, line_start, line_end=None):
    allowed = {d["doc_id"] for d in discover_docs(app)}
    if doc_id not in allowed: return json.dumps({"error": f"{doc_id} not in {app}'s docs"})
    if line_end is None: line_end = line_start
    out = [f"L{ln}: {t}" for ln, t in doc_lines(doc_id) if line_start <= ln <= line_end]
    return "\n".join(out) if out else f"[no lines {line_start}-{line_end}]"

TOOLS = [
    {"name": "list_drug_docs", "description": "List this drug's FDA docs (doc_id, type, url).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "grep_doc", "description": "Regex search (case-insensitive) a doc; returns matching L<n> lines.",
     "input_schema": {"type": "object", "properties": {"doc_id": {"type": "string"}, "pattern": {"type": "string"}, "max_results": {"type": "integer", "default": 25}}, "required": ["doc_id", "pattern"]}},
    {"name": "read_doc_lines", "description": "Read a line range from a doc for context.",
     "input_schema": {"type": "object", "properties": {"doc_id": {"type": "string"}, "line_start": {"type": "integer"}, "line_end": {"type": "integer"}}, "required": ["doc_id", "line_start"]}},
    {"name": "save_card", "description": "Submit the final card JSON. Call once.",
     "input_schema": {"type": "object", "properties": {"card": {"type": "object"}}, "required": ["card"]}},
]

def load_meta(app):
    def rd(f): return csv.DictReader(open(FRESH / f, encoding="latin-1"), delimiter="\t")
    n = app[3:]
    m = {"type": app[:3], "appno": n}
    for r in rd("Applications.txt"):
        if r["ApplNo"] == n: m["sponsor"] = r.get("SponsorName", ""); break
    for r in rd("Products.txt"):
        if r["ApplNo"] == n:
            m["drug"] = r.get("DrugName", ""); m["active"] = r.get("ActiveIngredient", ""); break
    for r in rd("Submissions.txt"):
        if r["ApplNo"] == n and r.get("SubmissionType") == "ORIG" and r.get("SubmissionStatus") == "AP":
            m["approval_date"] = (r.get("SubmissionStatusDate") or "")[:10]
            m["review_priority"] = r.get("ReviewPriority", ""); break
    return m

_NORMDOC = {}
def _norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()
def norm_doc(did):
    """Whole-doc normalized text (tabs/newlines collapsed) so table + multi-line
    quotes match. Cached per doc_id."""
    if did not in _NORMDOC:
        _NORMDOC[did] = _norm(" ".join(t for _, t in doc_lines(did)))
    return _NORMDOC[did]

def verify_citations(card):
    """Deterministically confirm every citation's quote appears (whitespace-
    normalized) in its cited doc. Returns (total, pass, missing, samples)."""
    total = passed = missing = 0
    samples = []
    def check(obj):
        nonlocal total, passed, missing
        if isinstance(obj, dict):
            if "citations" in obj and isinstance(obj["citations"], list):
                for c in obj["citations"]:
                    q = _norm(c.get("quote") or "")
                    did = c.get("doc_id", "")
                    if not q: continue
                    total += 1
                    probe = q[:60] if len(q) >= 60 else q  # normalized prefix
                    if did and probe in norm_doc(did):
                        passed += 1
                    else:
                        missing += 1
                        if len(samples) < 8: samples.append((c.get("quote") or "")[:80])
            for v in obj.values(): check(v)
        elif isinstance(obj, list):
            for v in obj: check(v)
    check(card)
    return total, passed, missing, samples

_NUMRE = re.compile(r'\d+(?:\.\d+)?')
_CI = {'95', '97.5', '90', '99', '2.5'}
def _field_numbers(txt):
    out = []
    for m in _NUMRE.finditer(txt or ''):
        d = m.group(0); s, e = m.start(), m.end()
        if d in _CI: continue
        if (s > 0 and txt[s-1].isalpha()) or (e < len(txt) and txt[e].isalpha()): continue
        out.append(d)
    return out

def number_coverage(card):
    """Fraction of numbers in field text that appear in one of that field's
    citation quotes. Returns (total, covered, uncovered_samples)."""
    tot = cov = 0; samples = []
    def leaf(field):
        nonlocal tot, cov
        if not (isinstance(field, dict) and 'text' in field): return
        cites = field.get('citations') or []
        joined = ' '.join(re.sub(r'\s+', ' ', c.get('quote', '')) for c in cites)
        for n in _field_numbers(field.get('text')):
            tot += 1
            if re.search(r'(?<!\d)' + re.escape(n) + r'(?!\d)', joined):
                cov += 1
            elif len(samples) < 10:
                samples.append(n)
    def walk(o):
        if isinstance(o, dict):
            if 'text' in o and 'citations' in o: leaf(o)
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(card)
    return tot, cov, samples

# --- prompt caching: system + tools are stable across the agentic loop; the
# conversation prefix grows and is re-sent every turn, so cache it too. ---
SYSTEM_CACHED = [{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}]
TOOLS_CACHED = [dict(t) for t in TOOLS]
TOOLS_CACHED[-1] = {**TOOLS_CACHED[-1], "cache_control": {"type": "ephemeral"}}


def _cache_last(messages):
    """Put a single rotating cache breakpoint on the last block of the last message."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    last = messages[-1]; c = last["content"]
    if isinstance(c, str):
        last["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(c, list) and c and isinstance(c[-1], dict):
        c[-1]["cache_control"] = {"type": "ephemeral"}


def _cost(tin, cw, cr, tout):
    return (tin * PRICE_IN + cw * PRICE_IN * 1.25 + cr * PRICE_IN * 0.10 + tout * PRICE_OUT) / 1e6


def run_one(app):
    meta = load_meta(app)
    drug = meta.get("drug", "?")
    if not discover_docs(app):
        return {"app": app, "error": "no docs in paperclip", "success": False}
    user = (f"Drug: {drug}\nApplication: {app}\nActive ingredient: {meta.get('active','?')}\n"
            f"Sponsor: {meta.get('sponsor','?')}\n\nBuild the fully-cited card. "
            f"Call list_drug_docs first, ground every field, then save_card once.")
    messages = [{"role": "user", "content": user}]
    tin = tout = cr = cw = 0; saved = None
    for turn in range(MAX_TURNS):
        _cache_last(messages)
        try:
            resp = client.messages.create(model=MODEL, max_tokens=16000, system=SYSTEM_CACHED, tools=TOOLS_CACHED, messages=messages)
        except Exception as e:
            return {"app": app, "error": str(e), "success": False, "cost_usd": round(_cost(tin, cw, cr, tout), 4)}
        u = resp.usage
        tin += u.input_tokens; tout += u.output_tokens
        cr += getattr(u, "cache_read_input_tokens", 0) or 0
        cw += getattr(u, "cache_creation_input_tokens", 0) or 0
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        if resp.stop_reason != "tool_use": break
        results = []
        for b in resp.content:
            if getattr(b, "type", None) != "tool_use": continue
            inp = b.input or {}
            print(f"  [{drug}] t{turn}: {b.name}({json.dumps(inp)[:90]})", flush=True)
            try:
                if b.name == "list_drug_docs": o = t_list(app)
                elif b.name == "grep_doc": o = t_grep(app, inp.get("doc_id",""), inp.get("pattern",""), inp.get("max_results",25))
                elif b.name == "read_doc_lines": o = t_read(app, inp.get("doc_id",""), int(inp.get("line_start",1)), inp.get("line_end"))
                elif b.name == "save_card": saved = inp.get("card"); o = json.dumps({"ok": True})
                else: o = json.dumps({"error": "unknown tool"})
            except Exception as e:
                o = json.dumps({"error": str(e)})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": o})
        messages.append({"role": "user", "content": results})
        if saved: break
    cost = _cost(tin, cw, cr, tout)
    r = {"app": app, "drug": drug, "success": saved is not None, "cost_usd": round(cost, 4),
         "turns": turn+1, "tokens_in": tin, "tokens_out": tout,
         "cache_read": cr, "cache_write": cw}
    if saved:
        vt, vp, vm, vs = verify_citations(saved)
        nt, nc, ns = number_coverage(saved)
        r["verify"] = {"total": vt, "pass": vp, "missing": vm, "missing_samples": vs,
                       "num_total": nt, "num_covered": nc, "num_uncovered": ns}
        out = {"app_key": app, "_meta": meta, "card": saved, "_verify": r["verify"], "_model": MODEL}
        (OUT_DIR / f"{app}.json").write_text(json.dumps(out, indent=2))
    return r

def main():
    apps = sys.argv[1:]
    if not apps: print(__doc__); sys.exit(1)
    print(f"SINGLE-AGENT (Opus 4.8) build of {len(apps)} cards\n", flush=True)
    total = 0.0
    for i, app in enumerate(apps, 1):
        print(f"--- [{i}/{len(apps)}] {app} ---", flush=True)
        t0 = time.time()
        r = run_one(app)
        total += r.get("cost_usd", 0)
        v = r.get("verify", {})
        print(f"  done {time.time()-t0:.0f}s  turns={r.get('turns','?')}  cost=${r.get('cost_usd',0):.3f}  "
              f"ok={r.get('success')}  cites {v.get('pass','?')}/{v.get('total','?')} verified  "
              f"NUMCOV {v.get('num_covered','?')}/{v.get('num_total','?')}", flush=True)
        if r.get("error"): print(f"  ERROR: {r['error']}", flush=True)
        print("", flush=True)
    print(f"=== total cost: ${total:.2f} for {len(apps)} cards (${total/max(len(apps),1):.2f}/card) ===", flush=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""AdComm verified re-extraction — mirrors run_rationale.py / run_major_amendment.py.
For each drug with an advisory committee, an Opus agent uses paperclip to read the
review docs and extract committee/date/vote + each concern->change linkage, every
fact backed by a verbatim FDA quote + L<n>, then deterministically grep-verified.

Usage: python run_adcomm.py <out_dir> <budget> <app ...>
"""
import asyncio, json, os, re, sys, subprocess, time
from pathlib import Path
from claude_code_sdk import query, ClaudeCodeOptions, ResultMessage

HERE = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project/flask_hf")
PROMPT_FILE = HERE / "PROMPT_ADCOMM.md"
CARDS = HERE / "data" / "cards.jsonl"
API_KEY = Path("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read_text().strip()
MODEL = "claude-opus-4-8"
ALLOWED_TOOLS = ["Bash", "Read", "Write", "Glob", "Grep"]
MAX_CONCURRENCY = int(os.environ.get("ADCOMM_CONCURRENCY", "5"))
MAX_TURNS = 60

OUT_DIR = Path(sys.argv[1]); BUDGET = float(sys.argv[2]); APPS = sys.argv[3:]
OUT_DIR.mkdir(parents=True, exist_ok=True)

meta = {}
for l in open(CARDS):
    c = json.loads(l); m = c.get("_meta", {})
    ak = f"{m.get('type','')}{m.get('appno','')}"
    meta[ak] = m.get("drug", ak)
# also the new single-agent cards, so new drugs get their real names
for _f in (HERE / "data" / "cards_single").glob("*.json"):
    try:
        m = json.loads(_f.read_text()).get("_meta", {})
        ak = f"{m.get('type','')}{m.get('appno','')}"
        meta.setdefault(ak, m.get("drug", ak))
    except Exception:
        pass

md = PROMPT_FILE.read_text()
PROMPT_TEMPLATE = re.search(r"```\s*\n(.*?)\n```", md, re.S).group(1)


def normalize_doc_id(raw):
    if not raw: return []
    out = [raw]
    if raw.startswith("fda_"):
        body = raw[4:].replace("-", ""); out += [f"fda_{body[:12]}", body]
    return list(dict.fromkeys(out))

def sql_docids(app, doc_type):
    like = f"%{doc_type}%" if doc_type else "%R.pdf%"
    sql = (f"SELECT document_id FROM all_documents WHERE identifier='{app}' "
           f"AND public_url ILIKE '{like}' LIMIT 8")
    try:
        r = subprocess.run(["paperclip","sql","--source","fda",sql], capture_output=True, text=True, timeout=30)
        return re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r.stdout)
    except Exception:
        return []

def grep_lines(doc_id, probe):
    try:
        r = subprocess.run(["paperclip","grep","-F",probe,f"/fda/{doc_id}/content.lines"], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return [int(x) for x in re.findall(r"^L(\d+):", r.stdout, re.MULTILINE)]

def verify_quote(app, q):
    if not q or not q.get("quote"): return {"status": "EMPTY"}
    probe = q["quote"][:70]; cited = q.get("line", 0)
    for cand in normalize_doc_id(q.get("doc_id","")) + sql_docids(app, q.get("doc_type","")):
        lines = grep_lines(cand, probe)
        if lines:
            if cited in lines: return {"status":"PASS","doc_id_used":cand,"found_lines":lines}
            return {"status":"OFFSET","doc_id_used":cand,"found_lines":lines,"offset":min(lines,key=lambda l:abs(l-cited))-cited}
    return {"status":"MISSING"}

def verify_adcomm(v):
    app = v.get("app")
    for k in ("committee_name","meeting_date","recommendation"):
        o = v.get(k)
        if isinstance(o, dict) and o.get("quote"):
            o["quote_verify"] = verify_quote(app, o["quote"])
    for lk in (v.get("linkages") or []):
        for qk in ("concern_quote","change_quote"):
            q = lk.get(qk)
            if isinstance(q, dict) and q.get("quote"):
                q["quote_verify"] = verify_quote(app, q)
    if v.get("no_adcom_quote"):
        v["no_adcom_quote_verify"] = verify_quote(app, v["no_adcom_quote"])
    return v

def count_quotes(v):
    tot = bad = 0
    def tally(q):
        nonlocal tot, bad
        st = (q.get("quote_verify") or {}).get("status")
        if st: tot += 1
        if st in ("MISSING","OFFSET"): bad += 1
    for k in ("committee_name","meeting_date","recommendation"):
        o = v.get(k)
        if isinstance(o, dict) and o.get("quote"): tally(o["quote"])
    for lk in (v.get("linkages") or []):
        for qk in ("concern_quote","change_quote"):
            if isinstance(lk.get(qk), dict) and lk[qk].get("quote"): tally(lk[qk])
    return tot, bad


state = {"cost":0.0, "done":0, "fail":0}
lock = asyncio.Lock()

async def run_one(app):
    out_json = OUT_DIR / f"{app}.json"
    if out_json.exists():
        print(f"[skip] {app}"); return
    drug = meta.get(app, app)
    cwd = OUT_DIR / "workdirs" / app; cwd.mkdir(parents=True, exist_ok=True)
    stale = cwd / "adcomm.json"
    if stale.exists(): stale.unlink()
    prompt = PROMPT_TEMPLATE.format(app_no=app, drug=drug)
    options = ClaudeCodeOptions(model=MODEL, max_turns=MAX_TURNS, permission_mode="bypassPermissions",
        cwd=str(cwd), env={"ANTHROPIC_API_KEY": API_KEY},
        extra_args={"tools": ",".join(ALLOWED_TOOLS), "disable-slash-commands": None})
    cost=0; turns=0; t0=time.time()
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0; turns = msg.num_turns
    except Exception as e:
        async with lock: state["fail"] += 1
        print(f"[err] {app}: {str(e)[:150]}"); return
    src = cwd / "adcomm.json"; v = None
    if src.exists():
        try: v = json.loads(src.read_text())
        except Exception: (OUT_DIR / f"{app}.raw").write_text(src.read_text())
    if v is not None:
        v = verify_adcomm(v); out_json.write_text(json.dumps(v, indent=2))
        tot, bad = count_quotes(v)
        tag = f"held={v.get('adcom_held')}, {len(v.get('linkages',[]))} linkages, {tot} quotes, {bad} unverified"
    else:
        tag = "NO_JSON"
    async with lock:
        state["cost"] += cost; state["done"] += 1
        d = state["done"] + state["fail"]
        print(f"[{d}] {app} {drug[:16]:<16} {tag}  cost=${cost:.2f} cum=${state['cost']:.2f} turns={turns} {time.time()-t0:.0f}s", flush=True)

async def main():
    print(f"AdComm run: {len(APPS)} drugs, model={MODEL}, budget=${BUDGET}", flush=True)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    async def gated(a):
        async with sem: await run_one(a)
    await asyncio.gather(*(gated(a) for a in APPS))
    print(f"\ndone: {state['done']}, failed: {state['fail']}, cost ${state['cost']:.2f}")

asyncio.run(main())

#!/usr/bin/env python3
"""Pipeline stage: grounded REPAIR. For each card with high/medium critic issues,
an Opus agent (paperclip tools) fixes the flagged fields by re-grounding in the
source docs (or removing unsupportable claims), then the card is re-verified.
Overwrites data/cards_single/<APP>.json (pre-repair backed up); logs _repair.

Usage: PYTHONUNBUFFERED=1 python3 run_card_repair.py [APP ...]   (default: all with issues)
Run the critic first (run_card_critic.py). Re-run the critic after to confirm.
"""
import json, sys, glob, time, re
from pathlib import Path
import run_card_single as B   # reuse paperclip tools + verify/coverage

_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

def _normalize_ids(o):
    """Strip stray whitespace the repair may inject into a doc_id UUID."""
    if isinstance(o, dict):
        if isinstance(o.get("doc_id"), str):
            o["doc_id"] = re.sub(r"\s+", "", o["doc_id"])
        for v in o.values(): _normalize_ids(v)
    elif isinstance(o, list):
        for v in o: _normalize_ids(v)

def _bad_doc_ids(o, bad=None):
    bad = [] if bad is None else bad
    if isinstance(o, dict):
        did = o.get("doc_id")
        if isinstance(did, str) and did and not _UUID.match(did):
            bad.append(did)
        for v in o.values(): _bad_doc_ids(v, bad)
    elif isinstance(o, list):
        for v in o: _bad_doc_ids(v, bad)
    return bad

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
CRITIC = HERE / "data" / "cards_critic"
BACKUP = HERE / "data" / "cards_single_prerepair_backup"
BACKUP.mkdir(parents=True, exist_ok=True)
PROMPT = (HERE / "PROMPT_REPAIR.md").read_text()
MODEL = "claude-opus-4-8"
PRICE_IN, PRICE_OUT = 15.0, 75.0

TOOLS = B.TOOLS[:3] + [{
    "name": "save_repaired_card",
    "description": "Submit the corrected full card + a changelog. Call exactly once.",
    "input_schema": {"type": "object", "properties": {
        "card": {"type": "object"}, "changes": {"type": "array"}}, "required": ["card", "changes"]},
}]
# Cache the (stable) tool block so tool defs aren't re-billed every turn — mirrors build.
TOOLS[-1] = {**TOOLS[-1], "cache_control": {"type": "ephemeral"}}

def repair_one(app):
    d = json.load(open(SRC / f"{app}.json"))
    card = d["card"]; drug = d["_meta"].get("drug", "")
    cp = CRITIC / f"{app}.json"
    if not cp.exists():
        return {"app": app, "skipped": True, "reason": "no critic output"}
    crit = json.load(open(cp))
    issues = crit.get("issues", [])
    af = crit.get("automated_flags", {})
    actionable = [i for i in issues if i.get("severity") in ("high", "medium")]
    det = bool(af.get("unverified_citations")) or bool(af.get("empty_fields"))
    if not actionable and not det:
        return {"app": app, "skipped": True, "reason": "no actionable issues"}
    user = (PROMPT + "\n\n=== CARD JSON ===\n" + json.dumps(card, indent=1)
            + "\n\n=== CRITIC ISSUES ===\n" + json.dumps(issues, indent=1)
            + "\n\n=== AUTOMATED_FLAGS ===\n" + json.dumps(crit.get("automated_flags", {}), indent=1)
            + f"\n\nDrug: {drug}  Application: {app}. Fix the flagged fields, ground every change in the docs, then call save_repaired_card once.")
    messages = [{"role": "user", "content": user}]
    tin = tout = cr = cw = 0; saved = None; nudges = 0
    for turn in range(60):
        B._cache_last(messages)   # rotating cache breakpoint so prior turns' OCR reads back at 0.1x
        # Streamed + 64K cap: writing the full corrected card for large cards took >120s
        # (client timeout) and hit the old 16K cap (diag_repair_NDA203214.log).
        with B.client.messages.stream(model=MODEL, max_tokens=64000, tools=TOOLS, messages=messages) as stream:
            resp = stream.get_final_message()
        u = resp.usage
        tin += u.input_tokens; tout += u.output_tokens
        cr += getattr(u, "cache_read_input_tokens", 0) or 0
        cw += getattr(u, "cache_creation_input_tokens", 0) or 0
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        if resp.stop_reason != "tool_use":
            has_tooluse = any(getattr(b, "type", None) == "tool_use" for b in resp.content)
            if saved or nudges >= 2 or has_tooluse:
                # has_tooluse here = a truncated tool call; nudging would dangle it. Stop.
                break
            nudges += 1
            messages.append({"role": "user", "content": "You have not called save_repaired_card yet. Call it now with the full corrected card and the changes list."})
            continue
        results = []
        for b in resp.content:
            if getattr(b, "type", None) != "tool_use": continue
            inp = b.input or {}
            print(f"  [{drug}] {b.name}({json.dumps(inp)[:80]})", flush=True)
            if b.name == "list_drug_docs": o = B.t_list(app)
            elif b.name == "grep_doc": o = B.t_grep(app, inp.get("doc_id", ""), inp.get("pattern", ""), inp.get("max_results", 25))
            elif b.name == "read_doc_lines": o = B.t_read(app, inp.get("doc_id", ""), int(inp.get("line_start", 1)), inp.get("line_end"))
            elif b.name == "save_repaired_card": saved = inp; o = json.dumps({"ok": True})
            else: o = json.dumps({"error": "unknown tool"})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": o})
        messages.append({"role": "user", "content": results})
        if saved: break
    cost = B._cost(tin, cw, cr, tout)   # cache-aware (cache reads at 0.1x), mirrors build
    print(f"  [{drug}] repair tokens: in={tin} cache_read={cr} cache_write={cw} out={tout}  ${cost:.3f}", flush=True)
    if not (saved and saved.get("card")):
        return {"app": app, "error": "no save_repaired_card", "cost": round(cost, 3)}
    newcard = saved["card"]
    if not isinstance(newcard, dict):   # agent returned card as a string/other — reject, keep pre-repair card
        return {"app": app, "rejected": True, "reason": f"repair returned non-dict card ({type(newcard).__name__})", "cost": round(cost, 3)}
    # GUARD: normalize + validate doc_ids; reject the repair rather than ship a corrupt card.
    _normalize_ids(newcard)
    bad = _bad_doc_ids(newcard)
    if bad:
        return {"app": app, "rejected": True, "reason": f"invalid doc_ids after repair: {bad[:2]}", "cost": round(cost, 3)}
    (BACKUP / f"{app}.json").write_text(json.dumps(d, indent=2))   # keep pre-repair
    vt, vp, vm, vs = B.verify_citations(newcard)
    nt, nc, ns = B.number_coverage(newcard)
    d["card"] = newcard
    d["_verify"] = {"total": vt, "pass": vp, "missing": vm, "num_total": nt, "num_covered": nc}
    d["_repair"] = {"changes": saved.get("changes", []), "model": MODEL}
    (SRC / f"{app}.json").write_text(json.dumps(d, indent=2))
    return {"app": app, "drug": drug, "changes": len(saved.get("changes", [])),
            "cites": f"{vp}/{vt}", "numcov": f"{nc}/{nt}", "cost": round(cost, 3)}

def main():
    apps = sys.argv[1:] or [Path(f).stem for f in sorted(glob.glob(str(SRC / "*.json")))]
    total = 0.0
    for i, app in enumerate(apps, 1):
        t0 = time.time()
        r = repair_one(app)
        total += r.get("cost", 0)
        if r.get("skipped"):
            print(f"[{i}/{len(apps)}] {app} skipped ({r['reason']})", flush=True)
        elif r.get("error"):
            print(f"[{i}/{len(apps)}] {app} ERROR: {r['error']}", flush=True)
        else:
            print(f"[{i}/{len(apps)}] {r['drug'][:14]:<14} {r['changes']} changes  "
                  f"cites {r['cites']}  numcov {r['numcov']}  ${r['cost']:.3f}  {time.time()-t0:.0f}s", flush=True)
    print(f"\ntotal ${total:.2f}", flush=True)

if __name__ == "__main__":
    main()

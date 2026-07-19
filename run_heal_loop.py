#!/usr/bin/env python3
"""Self-healing loop. Per card: critic -> (if high/medium issues) grounded repair
-> re-critic, up to MAX_ROUNDS. Stops when no high/medium issues remain, the round
cap is hit, or a repair is rejected by the guards (so a corrupt repair is never
shipped). Prints per-card residual flags so nothing high-severity ships silently.

Usage: PYTHONUNBUFFERED=1 python3 run_heal_loop.py [APP ...]   (default: all)
Requires: cards already built in data/cards_single/.
"""
import json, sys, glob, time
from pathlib import Path
import run_card_critic as C
import run_card_repair as R

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
MAX_ROUNDS = 3

def problems(out):
    """Combined view: high/medium critic issues PLUS reliable deterministic flags
    (unverified citations, empty required fields). The deterministic ones are
    authoritative — the LLM critic cannot suppress them by returning issues:[]."""
    probs = [i for i in out.get("issues", []) if i.get("severity") in ("high", "medium")]
    af = out.get("automated_flags", {})
    for u in af.get("unverified_citations", []):
        probs.append({"severity": "high", "field": u.get("field"),
                      "issue": f"citation quote not found in paperclip OCR: {(u.get('quote') or '')[:45]}"})
    for f in af.get("empty_fields", []):
        probs.append({"severity": "medium", "field": f, "issue": "required field empty"})
    return probs

def heal(app):
    out, cost, drug = C.critique_one(app)
    total = cost; rounds = 0; note = ""
    while problems(out) and rounds < MAX_ROUNDS:
        before = len(problems(out))
        r = R.repair_one(app)
        total += r.get("cost", 0)
        if r.get("skipped"): note = "no actionable issues"; break
        if r.get("rejected"): note = f"repair REJECTED: {r.get('reason')}"; break
        if r.get("error"): note = f"repair error: {r.get('error')}"; break
        out, cost, drug = C.critique_one(app)
        total += cost; rounds += 1
        after = len(problems(out))
        # NO-PROGRESS GUARD: if a repair round didn't REDUCE the problem count, the
        # remaining issues aren't groundable (semantic churn like Datroway's `met`).
        # Stop and surface them for a human instead of burning more rounds.
        if after and after >= before:
            note = f"stopped: no progress ({before}->{after} issues) — surfaced for review"
            break
    return {"app": app, "drug": drug, "rounds": rounds, "residual": problems(out),
            "ready": out.get("release_ready"), "cost": total, "note": note}

def main():
    apps = sys.argv[1:] or [Path(f).stem for f in sorted(glob.glob(str(SRC / "*.json")))]
    total = 0.0; results = []
    for i, app in enumerate(apps, 1):
        t0 = time.time()
        try:
            r = heal(app)
        except Exception as e:
            # One card's API error (e.g. transient 529 overload) must not kill the
            # whole batch — record it and move on.
            r = {"app": app, "drug": app, "rounds": 0, "residual": [], "cost": 0.0,
                 "note": f"ERROR (skipped): {str(e)[:70]}"}
        total += r["cost"]; results.append(r)
        print(f"[{i}/{len(apps)}] {r['drug'][:14]:<14} rounds={r['rounds']} "
              f"residual={len(r['residual'])} {('| ' + r['note']) if r['note'] else ''} "
              f"${r['cost']:.2f} {time.time()-t0:.0f}s", flush=True)
    clean = [r for r in results if not r["residual"]]
    print(f"\n=== HEAL SUMMARY  total ${total:.2f} ===", flush=True)
    print(f"clean (no high/medium residual): {len(clean)}/{len(results)}", flush=True)
    for r in results:
        if r["residual"]:
            print(f"\n{r['drug']} ({r['app']}) — {len(r['residual'])} residual {('| '+r['note']) if r['note'] else ''}:", flush=True)
            for x in r["residual"]:
                print(f"  [{x.get('severity')}] {x.get('field')}: {str(x.get('issue'))[:88]}", flush=True)

if __name__ == "__main__":
    main()

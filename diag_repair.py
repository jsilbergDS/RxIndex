#!/usr/bin/env python3
"""Diagnose why run_card_repair times out for some cards.

Runs run_card_repair.repair_one(APP) unchanged, except:
  - SRC/BACKUP point at diag_repair/<APP>/ (a copy) -> real cards are never written
  - every messages.create call is logged: turn, request size, duration, stop_reason,
    output tokens, tool calls, or the full exception
  - each call is ONE attempt with a 900s timeout (instead of 120s x 9 retries),
    so we see how long the slow call actually takes.
Usage: PYTHONUNBUFFERED=1 python3 diag_repair.py APP
"""
import json, sys, time, shutil, traceback
from pathlib import Path
import run_card_repair as R
import run_card_single as B

APP = sys.argv[1]
HERE = Path(__file__).parent
DIAG = HERE / "diag_repair" / APP
(DIAG / "backup").mkdir(parents=True, exist_ok=True)
shutil.copy(HERE / "data" / "cards_single" / f"{APP}.json", DIAG / f"{APP}.json")
R.SRC = DIAG
R.BACKUP = DIAG / "backup"

orig_client = B.client
fast = orig_client.with_options(timeout=900.0, max_retries=0)
calls = []

class _Messages:
    def create(self, **kw):
        n = len(calls) + 1
        req_chars = len(json.dumps(kw.get("messages", []), default=str))
        print(f"[call {n}] start  request={req_chars:,} chars  msgs={len(kw.get('messages', []))}", flush=True)
        t0 = time.time()
        try:
            resp = fast.messages.create(**kw)
        except Exception as e:
            dt = time.time() - t0
            calls.append({"call": n, "secs": round(dt, 1), "req_chars": req_chars, "error": repr(e)})
            print(f"[call {n}] EXCEPTION after {dt:.1f}s: {repr(e)}", flush=True)
            raise
        dt = time.time() - t0
        u = resp.usage
        tools = [(b.name, len(json.dumps(b.input))) for b in resp.content if getattr(b, "type", None) == "tool_use"]
        rec = {"call": n, "secs": round(dt, 1), "req_chars": req_chars, "stop": resp.stop_reason,
               "in": u.input_tokens, "cache_read": getattr(u, "cache_read_input_tokens", 0),
               "out": u.output_tokens, "tools": tools}
        calls.append(rec)
        print(f"[call {n}] done {dt:.1f}s stop={resp.stop_reason} out_tokens={u.output_tokens} "
              f"in={u.input_tokens} cache_read={rec['cache_read']} tools={tools}", flush=True)
        return resp

class _Client:
    messages = _Messages()

B.client = _Client()

t0 = time.time()
try:
    r = R.repair_one(APP)
    print("RESULT:", json.dumps(r, default=str)[:500], flush=True)
except Exception:
    traceback.print_exc()
print(f"TOTAL {time.time()-t0:.0f}s over {len(calls)} calls", flush=True)
json.dump(calls, open(DIAG / "calls.json", "w"), indent=1)
print("DIAG_DONE", flush=True)

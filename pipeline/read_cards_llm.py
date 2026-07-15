#!/usr/bin/env python3
"""Have Opus 4.8 read each per-drug card and answer: does this drug belong in
the cohort of "approved despite failed prespecified primary endpoint in a
pivotal trial"?

Grounded only in the card's verbatim quotes.
"""
import json, os, sys, time, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import anthropic

PROJECT = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project")
CARDS_DIR = PROJECT / "four_bucket_cards"
OUT = PROJECT / "four_bucket_verified" / "_llm_verdicts.json"

API_KEY = Path("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read_text().strip()
MODEL = "claude-opus-4-8"
CONCURRENCY = int(os.environ.get("READ_CONCURRENCY", "6"))
client = anthropic.Anthropic(api_key=API_KEY)

PROMPT = """You are reviewing a card of verbatim FDA quotes for one drug approval.
The card contains four buckets of quotes: (1) prespecified primary endpoint(s),
(2) endpoint outcome (met / missed), (3) FDA characterization of each trial's
role (pivotal / supportive / confirmatory / exploratory / dose-ranging / etc.),
(4) FDA rationale for approval.

Answer, based ONLY on the quotes in this card (no external knowledge):

Q1. Which trial(s), if any, does the FDA state had a prespecified primary
endpoint that WAS NOT MET? Quote the specific bucket-2 line.

Q2. For each such trial, does FDA characterize it as PIVOTAL (or "adequate
and well-controlled" or a Phase 3 trial submitted as the basis for approval)?
Quote the specific bucket-3 line. If FDA calls it supportive / confirmatory /
exploratory / dose-ranging / Phase 1 or 2 / not adequate and well-controlled,
quote that.

Q3. Verdict:
  - KEEP  = at least one trial that FDA characterizes as pivotal had a
            prespecified primary endpoint that FDA states was not met.
  - DROP  = no such trial (either no failed endpoint anywhere, OR the failure
            was in a trial FDA characterizes as non-pivotal).
  - UNCLEAR = the quotes are contradictory or insufficient to decide.

Q4. One-sentence justification citing the exact quotes you rely on.

Return STRICT JSON only:
{{
  "app": "{app}",
  "drug": "{drug}",
  "q1_failed_trials": [
    {{"trial": "<name>", "outcome_quote": "<verbatim>"}}
  ],
  "q2_role_per_failed_trial": [
    {{"trial": "<name>", "role_quote": "<verbatim>", "pivotal_per_fda": true | false | "unclear"}}
  ],
  "verdict": "KEEP" | "DROP" | "UNCLEAR",
  "justification": "<one sentence, citing the quotes>"
}}

CARD:
---
{card}
---"""


def read_one(app: str, drug: str):
    path = CARDS_DIR / f"{app}.md"
    card = path.read_text()
    prompt = PROMPT.format(app=app, drug=drug, card=card)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=2000,
            messages=[{"role":"user","content":prompt}]
        )
    except Exception as e:
        return {"app": app, "drug": drug, "error": str(e)[:300]}
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    verdict = None
    if m:
        try:
            verdict = json.loads(m.group(0))
        except Exception:
            pass
    return {
        "app": app, "drug": drug,
        "verdict_obj": verdict, "raw_text": text if verdict is None else None,
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
    }


def main():
    CARDS_FILE = PROJECT / "endpoint_cards.jsonl"
    drug_meta = {json.loads(l)["app"]: json.loads(l) for l in open(CARDS_FILE)}
    apps = sorted(p.stem for p in CARDS_DIR.glob("*.md") if p.stem != "index")
    results = {}
    lock = Lock()
    ti = to = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(read_one, a, drug_meta.get(a,{}).get("drug","?")): a for a in apps}
        n_done = 0
        for fut in as_completed(futs):
            r = fut.result()
            with lock:
                results[r["app"]] = r
                ti += r.get("tokens_in", 0); to += r.get("tokens_out", 0)
                n_done += 1
                v = (r.get("verdict_obj") or {}).get("verdict", "?")
                cost = ti*5/1e6 + to*25/1e6
                print(f"[{n_done}/{len(apps)}] {r['app']} {r['drug'][:22]:<22}  verdict={v}  cum=${cost:.2f}", flush=True)

    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)

    # Summary
    from collections import Counter
    counter = Counter()
    for a, r in results.items():
        v = (r.get("verdict_obj") or {}).get("verdict", "PARSE_FAIL")
        counter[v] += 1
    cost = ti*5/1e6 + to*25/1e6
    print(f"\n=== SUMMARY ===")
    for v, n in sorted(counter.items()): print(f"  {v}: {n}")
    print(f"Cost: ${cost:.2f}")
    print(f"Saved: {OUT}")


if __name__ == "__main__":
    main()

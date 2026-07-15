#!/usr/bin/env python3
"""Semantic critic for cards_recited.jsonl. Adapted from endpoints_app/verify_red_chips.py.

For each field in each cited card, feed the field's `text` and the FULL LINE CONTEXT
of every citation into Haiku 4.5 and prompt it to REFUTE the claim. Adversarial
framing (refute-not-confirm) counteracts confirmation bias.

Verdict per field:
  - supported:       text is a faithful summary of the cited quotes/lines
  - overstated:      text implies more than the quotes support (adds nuance, "significantly", "non-inferior" that isn't stated)
  - misattributed:   text conflates content from unrelated passages
  - numeric_mismatch: text contains numbers the quotes don't back
  - fabricated:      text contains claims with no support in the quotes at all

Output: data/critic_verdicts.json  (list of per-field verdicts with reasons)

Run:
  cd flask_hf
  PYTHONUNBUFFERED=1 python3 critic_semantic.py [APP_KEY ...]
Runs on all cited drugs if no args.
"""
import json, re, sys, time
from pathlib import Path
from anthropic import Anthropic
from tqdm import tqdm

HERE = Path(__file__).parent
DATA = HERE / "data"
DOCS = DATA / "docs"
RECITED = DATA / "cards_recited.jsonl"
OUT = DATA / "critic_verdicts.json"

client = Anthropic(api_key=open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip())
MODEL = "claude-haiku-4-5-20251001"
PRICE_IN, PRICE_OUT = 1.0, 5.0  # $/M tokens


def get_line(doc_id: str, line_n: int) -> str | None:
    p = DOCS / f"{doc_id}.txt"
    if not p.exists(): return None
    cur_n, buf = None, []
    for raw in p.read_text().split("\n"):
        m = re.match(r"^L(\d+):\s?(.*)$", raw)
        if m:
            if cur_n == line_n: return "\n".join(buf)
            cur_n = int(m.group(1)); buf = [m.group(2)]
        else:
            buf.append(raw)
    return "\n".join(buf) if cur_n == line_n else None


def build_prompt(field_path: str, text: str, citations: list) -> str:
    # For each citation, show quote + the ACTUAL line context (line ±1) so critic can compare
    src_blocks = []
    for i, c in enumerate(citations[:15]):
        did = c.get("doc_id", "")
        ln = c.get("line", 0)
        quote = c.get("quote", "")
        # Show cited line + adjacent lines
        ctx_lines = []
        for n in range(max(0, ln - 1), ln + 2):
            t = get_line(did, n)
            if t: ctx_lines.append(f"L{n}: {t[:300]}")
        ctx_str = "\n".join(ctx_lines) if ctx_lines else "(line not found)"
        src_blocks.append(
            f"[{i+1}] extraction says quote={quote!r} at doc={did[:8]} line={ln}\n"
            f"     ACTUAL SOURCE lines {ln-1}-{ln+1}:\n{ctx_str}"
        )
    sources = "\n\n".join(src_blocks)

    return f"""You are a skeptical FDA reviewer auditing a drug card claim for hallucination.

A field in a drug card contains this text:

  FIELD PATH: {field_path}
  TEXT: "{text}"

The extraction claims this text is supported by these citations from FDA source docs. For each citation, we show the ACTUAL text at the cited line so you can verify what's really there:

{sources}

YOUR JOB: try to REFUTE the claim. Assume something is wrong unless the quotes + source lines clearly support the text. Be adversarial — do NOT accept vague matches. Look specifically for:
- Words in the text that don't appear in any source line (e.g. "significantly", "non-inferior", "sustained")
- Numbers in the text that don't appear in any source line (or appear with different meaning)
- Claims that combine or conflate content from unrelated passages
- Overstatement (text says more than the sources support)

Output ONLY this JSON — no prose, no markdown:
{{
  "verdict": "supported" | "overstated" | "misattributed" | "numeric_mismatch" | "fabricated",
  "confidence": "high" | "medium" | "low",
  "issues": ["<specific problem 1>", "<specific problem 2>"],
  "reason": "<one-sentence justification>"
}}

Categories:
- supported: text is a faithful summary; every substantive claim is grounded
- overstated: text adds interpretation/emphasis beyond what quotes say (e.g. "non-inferior" when quotes only show HR)
- misattributed: text combines content from unrelated passages, or attributes finding to wrong study
- numeric_mismatch: one or more numbers in text don't match quotes
- fabricated: substantive claims have no basis in the shown quotes at all
"""


def classify(field_path: str, field: dict) -> dict:
    text = field.get("text", "")
    cites = field.get("citations", [])
    if not text.strip() or not cites:
        return {"verdict": "no_data", "confidence": "high", "issues": [], "reason": "empty field or no citations"}
    prompt = build_prompt(field_path, text, cites)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        cost = (resp.usage.input_tokens/1e6)*PRICE_IN + (resp.usage.output_tokens/1e6)*PRICE_OUT
        raw = resp.content[0].text.strip()
        # Strip code fences if any
        raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.M)
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            # Try to salvage the JSON substring
            mm = re.search(r"\{[^{}]*\}", raw, re.S)
            v = json.loads(mm.group(0)) if mm else {"verdict": "parse_error", "raw": raw[:200]}
        v["_cost_usd"] = round(cost, 5)
        v["_tokens"] = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
        return v
    except Exception as e:
        return {"verdict": "api_error", "reason": str(e)[:200]}


def iter_cited_fields(card, prefix=""):
    if isinstance(card, dict):
        if "text" in card and "citations" in card:
            yield prefix, card
        else:
            for k, v in card.items():
                yield from iter_cited_fields(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(card, list):
        for i, v in enumerate(card):
            yield from iter_cited_fields(v, f"{prefix}.{i}")


def main():
    if not RECITED.exists():
        raise SystemExit(f"{RECITED} not found — run extract_cards_cited.py first")
    rows = [json.loads(l) for l in open(RECITED)]
    # Optional filter
    if len(sys.argv) > 1:
        keys = set(sys.argv[1:])
        rows = [r for r in rows if r["app_key"] in keys]
    # Drug index
    drug_by_key = {}
    import csv
    for r in csv.DictReader(open(DATA / "drug_index.csv")):
        drug_by_key[r["app_key"]] = r["drug"]

    print(f"Semantic critic on {len(rows)} drugs with {MODEL}", flush=True)
    for r in rows:
        print(f"  {r['app_key']} = {drug_by_key.get(r['app_key'], '?')}", flush=True)
    print()

    results = []
    total_cost = 0.0
    for row in rows:
        app_key = row["app_key"]
        drug = drug_by_key.get(app_key, "?")
        fields = list(iter_cited_fields(row["card"]))
        print(f"--- {drug} ({app_key}): {len(fields)} fields ---", flush=True)
        for path, field in tqdm(fields, desc=f"{drug:12s}"):
            v = classify(path, field)
            results.append({"app_key": app_key, "drug": drug, "field_path": path,
                            "text": field["text"][:200],
                            "n_citations": len(field["citations"]),
                            "verdict": v.get("verdict"),
                            "confidence": v.get("confidence"),
                            "issues": v.get("issues", []),
                            "reason": v.get("reason", ""),
                            "cost_usd": v.get("_cost_usd", 0)})
            total_cost += v.get("_cost_usd", 0)

    with open(OUT, "w") as f:
        json.dump({"model": MODEL, "total_cost_usd": round(total_cost, 4),
                    "verdicts": results}, f, indent=2, ensure_ascii=False)

    print(f"\n=== Summary ===", flush=True)
    print(f"Total cost: ${total_cost:.3f}", flush=True)
    from collections import Counter
    tally = Counter(r["verdict"] for r in results)
    for k, n in tally.most_common():
        pct = 100.0 * n / len(results)
        print(f"  {k:20s}  {n:>3}  ({pct:5.1f}%)", flush=True)
    print(f"\nSaved: {OUT}", flush=True)


if __name__ == "__main__":
    main()

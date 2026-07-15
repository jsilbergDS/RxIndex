#!/usr/bin/env python3
"""Re-extract drug cards with per-claim line citations, using Opus 4.8 as a
grep-driven agent over the bundled FDA review documents.

The agent gets three tools — list_drug_docs, grep_doc, read_doc_lines — and
must output structured JSON where every text/number claim carries the doc_id +
line(s) where it appears verbatim in the source.

Output: data/cards_recited.jsonl  (one card per line, same shape as cards.jsonl
        but each field becomes {text, citations: [{quote, doc_id, line}, ...]})

Run on Sherlock (paperclip not required at runtime — we read local data/docs/):
  cd flask_hf
  PYTHONUNBUFFERED=1 python3 extract_cards_cited.py [APP_KEY ...]

If no APP_KEY args are given, runs the 5-drug pilot set.
"""
import os, sys, json, re, time
from pathlib import Path
from anthropic import Anthropic

HERE = Path(__file__).parent
DATA = HERE / "data"
DOCS_DIR = DATA / "docs"
DOCS_INDEX = json.load(open(DATA / "docs_index.json"))
PAGES_META = json.load(open(DATA / "pages_meta.json"))
CARDS = [json.loads(l) for l in open(DATA / "cards.jsonl")]
CARDS_BY_KEY = {f"{c['_meta']['type']}{c['_meta']['appno'].zfill(6)}": c for c in CARDS}

API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = Anthropic(api_key=API_KEY)

MODEL = "claude-opus-4-8"
MAX_TURNS = 40

# Opus 4.8 pricing (as of mid-2026; verify on console)
PRICE_IN_PER_M = 15.0
PRICE_OUT_PER_M = 75.0

PILOT_KEYS = ["BLA208471", "NDA206488", "BLA761025", "NDA208065", "NDA217388"]


# --- Doc loading + caches ---

_DOC_LINES: dict[str, list[tuple[int, str]]] = {}

def doc_lines(doc_id: str) -> list[tuple[int, str]]:
    if doc_id in _DOC_LINES: return _DOC_LINES[doc_id]
    p = DOCS_DIR / f"{doc_id}.txt"
    if not p.exists():
        _DOC_LINES[doc_id] = []
        return []
    out = []
    cur_n, cur_buf = None, []
    for raw in p.read_text().split("\n"):
        m = re.match(r"^L(\d+):\s?(.*)$", raw)
        if m:
            if cur_n is not None:
                out.append((cur_n, "\n".join(cur_buf).strip()))
            cur_n = int(m.group(1))
            cur_buf = [m.group(2)]
        else:
            cur_buf.append(raw)
    if cur_n is not None:
        out.append((cur_n, "\n".join(cur_buf).strip()))
    _DOC_LINES[doc_id] = out
    return out


# --- Tool implementations ---

def tool_list_drug_docs(app_key: str) -> str:
    docs = DOCS_INDEX.get(app_key, [])
    if not docs:
        return json.dumps({"error": f"no docs for {app_key}"})
    out = []
    for d in docs:
        info = PAGES_META.get(d["document_id"], {})
        out.append({
            "doc_id": d["document_id"],
            "type": d["type"],
            "total_pages": info.get("total_pages"),
            "total_lines": info.get("total_lines"),
            "public_url": d["public_url"],
        })
    return json.dumps(out, indent=2)


def tool_grep_doc(app_key: str, doc_id: str, pattern: str, max_results: int = 20) -> str:
    # Validate doc_id belongs to drug
    allowed = {d["document_id"] for d in DOCS_INDEX.get(app_key, [])}
    if doc_id not in allowed:
        return json.dumps({"error": f"doc {doc_id} not in {app_key}'s allowed list"})
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return json.dumps({"error": f"invalid regex: {e}"})
    hits = []
    for ln, t in doc_lines(doc_id):
        if rx.search(t):
            hits.append({"line": ln, "text": t[:400]})
            if len(hits) >= max_results: break
    return json.dumps({"matches": len(hits), "lines": hits}, indent=2)


def tool_read_doc_lines(app_key: str, doc_id: str, line_start: int, line_end: int | None = None) -> str:
    allowed = {d["document_id"] for d in DOCS_INDEX.get(app_key, [])}
    if doc_id not in allowed:
        return json.dumps({"error": f"doc {doc_id} not in {app_key}'s allowed list"})
    if line_end is None: line_end = line_start
    lines = doc_lines(doc_id)
    out = []
    for ln, t in lines:
        if line_start <= ln <= line_end:
            out.append(f"L{ln}: {t}")
        if ln > line_end: break
    return "\n".join(out) if out else f"[no lines in range {line_start}-{line_end}]"


TOOLS = [
    {
        "name": "list_drug_docs",
        "description": "List all FDA documents for this drug. Returns doc_id, type, total_pages, total_lines, public_url for each.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "grep_doc",
        "description": ("Search a doc for a regex pattern (case-insensitive). Returns up to "
                        "max_results matching lines with their L<line> numbers and text."),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "pattern": {"type": "string", "description": "Regex pattern. Examples: 'primary endpoint', 'HR\\s*\\d', 'mechanism of action'."},
                "max_results": {"type": "integer", "default": 20},
            },
            "required": ["doc_id", "pattern"],
        },
    },
    {
        "name": "read_doc_lines",
        "description": "Read a contiguous range of lines from a doc. Use this after grep_doc to see fuller context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer", "description": "Inclusive. If omitted, reads just line_start."},
            },
            "required": ["doc_id", "line_start"],
        },
    },
    {
        "name": "save_card",
        "description": "Submit the final structured card. Call this exactly once at the end.",
        "input_schema": {
            "type": "object",
            "properties": {"card": {"type": "object", "description": "The full structured card JSON. See system prompt for schema."}},
            "required": ["card"],
        },
    },
]


SYSTEM_PROMPT = """You are an FDA-review extractor. For ONE drug approval, build a structured card where EVERY text claim and EVERY number is anchored to a verbatim line in an FDA review document.

WORKFLOW:
1. Call list_drug_docs first to see what docs are available.
2. Use grep_doc to find specific information (start with broad searches, then narrow).
3. Use read_doc_lines to verify exact wording before recording a claim.
4. Build the card. Every field value's `citations` array must reference REAL lines you grep'd or read.
5. Call save_card ONCE with the final structured card.

CITATION RULES — non-negotiable:
- Every text claim and every number must have a citation: {quote, doc_id, line}.
- `quote` = the exact verbatim string from the doc line (or a substring of it).
- `line` = the L<n> integer where it appears.
- For a numeric field like "HR 1.02 (95% CI 0.89-1.17); 406/3034 (13.4%) vs 399/3034 (13.2%)", produce ONE citation per distinct claim (1.02, 0.89-1.17, 406/3034, 13.4%, 399/3034, 13.2%). They may all share the same doc_id/line.
- If you cannot find a verbatim source for a claim, OMIT THAT CLAIM. Do not fabricate.
- Prefer the MedR (medical review) or IntegratedR for clinical content; Lbl for indication/dosing; AppLetter for supplements.

OUTPUT SCHEMA — return exactly this shape via save_card:
{
  "pivotal_trials": [
    {
      "phase":      {"text": "Phase 3", "citations": [{"quote": "Phase 3", "doc_id": "...", "line": 137}]},
      "trial_name": {"text": "ELIXA (EFC11319)", "citations": [...]},
      "design":     {"text": "...", "citations": [...]},
      "endpoint":   {"text": "...", "citations": [...]},
      "n":          {"text": "6063", "citations": [{"quote": "6063 patients", "doc_id": "...", "line": 2840}]},
      "result":     {"text": "HR 1.02 (95% CI 0.89-1.17); ...",
                     "citations": [
                       {"quote": "1.02", "doc_id": "...", "line": 325},
                       {"quote": "0.89", "doc_id": "...", "line": 325},
                       ...
                     ]}
    }
  ],
  "indication":  {"text": "DRUG is indicated for...", "citations": [{"quote": "...", "doc_id": "...", "line": 5}]},
  "key_safety":  {"text": "...", "citations": [...]},
  "target_moa":  {"text": "...", "citations": [...]},
  "modality":    {"text": "mAb | small molecule | peptide | ASO | ...", "citations": [...]}
}

Be efficient with greps. Aim for 10-20 grep calls + 5-15 read_doc_lines, not 100.
Do NOT dump the full doc. Use targeted searches."""


def run_one_drug(app_key: str) -> dict:
    """Run the agent on one drug. Returns {card, cost_usd, turns, success}."""
    if app_key not in DOCS_INDEX:
        return {"error": f"no docs for {app_key}", "success": False}

    card_meta = CARDS_BY_KEY.get(app_key, {}).get("_meta", {})
    drug = card_meta.get("drug", "?")
    user_msg = f"""Drug: {drug}
APP_KEY: {app_key}

Extract a verifiable structured card for this drug. Use grep_doc and read_doc_lines on the available FDA docs to ground every claim. Call save_card with the final result."""

    messages = [{"role": "user", "content": user_msg}]
    total_in, total_out = 0, 0
    saved_card = None

    for turn in range(MAX_TURNS):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            print(f"  [{drug}] API error on turn {turn}: {e}", flush=True)
            return {"error": str(e), "success": False, "cost_usd": (total_in/1e6)*PRICE_IN_PER_M + (total_out/1e6)*PRICE_OUT_PER_M}

        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

        if resp.stop_reason != "tool_use":
            print(f"  [{drug}] turn {turn}: stop_reason={resp.stop_reason}, no tool_use, terminating", flush=True)
            break

        tool_results = []
        for b in resp.content:
            if getattr(b, "type", None) != "tool_use": continue
            name = b.name
            inp = b.input or {}
            print(f"  [{drug}] turn {turn}: {name}({json.dumps(inp)[:120]})", flush=True)
            try:
                if name == "list_drug_docs":
                    out = tool_list_drug_docs(app_key)
                elif name == "grep_doc":
                    out = tool_grep_doc(app_key, inp.get("doc_id",""), inp.get("pattern",""), inp.get("max_results", 20))
                elif name == "read_doc_lines":
                    out = tool_read_doc_lines(app_key, inp.get("doc_id",""), int(inp.get("line_start", 1)), inp.get("line_end"))
                elif name == "save_card":
                    saved_card = inp.get("card")
                    out = json.dumps({"ok": True, "message": "Card saved. You may stop now."})
                else:
                    out = json.dumps({"error": f"unknown tool {name}"})
            except Exception as e:
                out = json.dumps({"error": str(e)})
            tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})

        messages.append({"role": "user", "content": tool_results})
        if saved_card: break

    cost = (total_in / 1e6) * PRICE_IN_PER_M + (total_out / 1e6) * PRICE_OUT_PER_M
    return {"card": saved_card, "cost_usd": round(cost, 4),
            "turns": turn+1, "tokens_in": total_in, "tokens_out": total_out,
            "success": saved_card is not None, "app_key": app_key, "drug": drug}


def main():
    keys = sys.argv[1:] or PILOT_KEYS
    out_path = DATA / "cards_recited.jsonl"
    # Verify every app_key maps to a known drug — fail fast if not, so we
    # don't silently extract the wrong drug.
    print(f"Extracting {len(keys)} drugs with {MODEL}", flush=True)
    for k in keys:
        if k not in CARDS_BY_KEY:
            sys.exit(f"FATAL: {k} not found in cards.jsonl")
        print(f"  {k} = {CARDS_BY_KEY[k]['_meta']['drug']}", flush=True)
    print(f"\nOutput: {out_path}\n", flush=True)
    results = []
    total_cost = 0.0
    with open(out_path, "w") as f:
        for i, k in enumerate(keys, 1):
            print(f"--- [{i}/{len(keys)}] {k} ---", flush=True)
            t0 = time.time()
            r = run_one_drug(k)
            dt = time.time() - t0
            total_cost += r.get("cost_usd", 0)
            results.append(r)
            print(f"  done in {dt:.1f}s   turns={r.get('turns','?')}   cost=${r.get('cost_usd', 0):.3f}   ok={r.get('success', False)}", flush=True)
            if r.get("card"):
                f.write(json.dumps({"app_key": k, "card": r["card"]}) + "\n")
                f.flush()
            print("", flush=True)

    print(f"\n=== Summary ===", flush=True)
    print(f"Total cost: ${total_cost:.2f}", flush=True)
    for r in results:
        ok = "OK" if r.get("success") else "FAIL"
        print(f"  {ok}  {r.get('app_key')}  ${r.get('cost_usd', 0):.3f}  {r.get('turns','?')} turns", flush=True)


if __name__ == "__main__":
    main()

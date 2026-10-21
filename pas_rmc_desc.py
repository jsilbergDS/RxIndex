#!/usr/bin/env python3
"""RMC 'what changed' — for each RECENT MAJOR CHANGES event, read the referenced
section in the label that announced it (AFTER) vs the immediately-prior label
(BEFORE), and have Opus state the delta with a grep-verified quote.

Targeted section-level diff guided by the label's own RMC box — not a whole-label diff.
Usage: PYTHONUNBUFFERED=1 python3 pas_rmc_desc.py [APP ...]   (default: Leqembi only)
Output: pas_rmc_desc_out/<app_key>.json
"""
import json, re, sys, time
from pathlib import Path
import anthropic
import pas_pilot as P
from pas_rmc_pilot import parse_rmc, categorize

OUT = Path(__file__).parent / "pas_rmc_desc_out"; OUT.mkdir(exist_ok=True)
API_KEY = open("/oak/stanford/groups/jamesz/jsilberg/.anthropic_api_key").read().strip()
client = anthropic.Anthropic(api_key=API_KEY, timeout=120.0, max_retries=6)
MODEL = "claude-opus-4-8"

PROMPT = """You are an FDA label-change extractor. A drug's label listed a change to {SECTION} dated {DATE}
in its RECENT MAJOR CHANGES box. Below is that section's text in the label BEFORE this date and
in the label version that ANNOUNCED the change (AFTER). Lines are prefixed L<n>:.

Describe in ONE concise factual line WHAT CHANGED (what was added, strengthened, or newly required),
anchored to a verbatim quote from the AFTER text.

Return STRICT JSON:
{"change": {"text":"<one line>","kind":"added|strengthened|revised",
            "quote":"<verbatim from AFTER, copied exactly from an L<n> line>","line":<L number>}}
RULES:
- Describe the DELTA (before -> after), NOT the whole section.
- If BEFORE is empty/absent, the section is newly added -> kind:"added".
- The quote MUST be copied exactly from an AFTER L<n> line (grep-verified; non-matching dropped).
- If you cannot identify a substantive change, return {"change": null}.
"""


def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()


def section_refs(sect):
    if sect.lower().startswith("boxed warning"):
        return ["BOXED"]
    m = re.search(r"\(([^)]*)\)", sect)
    return re.findall(r"\d+(?:\.\d+)?", m.group(1)) if m else []


def get_section(blocks, ref):
    """Return line-tagged text of one label section (BOXED, '4', '5.1', ...)."""
    if ref == "BOXED":
        for i, (n, t) in enumerate(blocks):
            if re.match(r"^#{0,3}\s*WARNING:", t.strip().split("\n")[0]):
                body = [f"L{n}: {t}"]
                for (nn, tt) in blocks[i + 1:i + 6]:
                    if tt.strip().split("\n")[0].strip().startswith("##"):
                        break
                    body.append(f"L{nn}: {tt}")
                return "\n".join(body)[:1400]
        return ""
    esc = ref.replace(".", r"\.")
    for i, (n, t) in enumerate(blocks):
        head = t.strip().split("\n")[0].strip()
        if re.match(rf"^#{{0,3}}\s*{esc}\s+[A-Za-z]", head):
            body = [f"L{n}: {t}"]
            for (nn, tt) in blocks[i + 1:i + 12]:
                h2 = tt.strip().split("\n")[0].strip()
                if re.match(r"^#{0,3}\s*\d+(\.\d+)?\s+[A-Z]", h2):
                    break
                if tt.strip():
                    body.append(f"L{nn}: {tt}")
            return "\n".join(body)[:1400]
    return ""


def describe(sect, date, before_txt, after_txt):
    msg = (PROMPT.replace("{SECTION}", sect).replace("{DATE}", date)
           + f"\n\n=== BEFORE ({'absent' if not before_txt.strip() else 'prior label'}) ===\n"
           + (before_txt or "(section not present in the prior label)")
           + "\n\n=== AFTER (label that announced the change) ===\n" + after_txt)
    resp = client.messages.create(model=MODEL, max_tokens=600,
                                  messages=[{"role": "user", "content": msg}])
    cost = resp.usage.input_tokens/1e6*15.0 + resp.usage.output_tokens/1e6*75.0
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if not m:
        return None, cost
    try:
        ch = json.loads(m.group(0), strict=False).get("change")
    except Exception:
        return None, cost
    if isinstance(ch, dict) and ch.get("quote"):
        if norm(ch["quote"])[:40] not in norm(after_txt):   # grep-verify the quote
            ch["quote_verified"] = False
        else:
            ch["quote_verified"] = True
    return ch, cost


def run(app_key):
    docs = P.label_docs(app_key)
    if not docs:
        return {"app_key": app_key, "drug": P.DRUG.get(app_key), "error": "no label docs"}
    parsed = [P.read_doc(d["doc_id"]) for d in docs]     # cache each version's blocks
    seen = set(); events = []; cost = 0.0
    for k, blocks in enumerate(parsed):
        rev = docs[k].get("rev")
        vlabel = f"{rev[1]}/{rev[0]}" if rev else str(docs[k].get("url_year"))
        for line, sect, date in parse_rmc(blocks):
            key = (re.sub(r"\s+", " ", sect.lower()), date)
            if key in seen:
                continue
            seen.add(key)
            cat, kind = categorize(sect)
            after = "".join(get_section(blocks, r) + "\n" for r in section_refs(sect))
            before = "".join(get_section(parsed[k - 1], r) + "\n" for r in section_refs(sect)) if k > 0 else ""
            ch, c = describe(sect, date, before, after) if after.strip() else (None, 0.0)
            cost += c
            events.append({"date": date, "category": cat, "section": sect,
                           "announced_in": vlabel, "doc_id": docs[k]["doc_id"], "line": line,
                           "change": ch})
    events.sort(key=lambda e: (int(e["date"].split("/")[1]), int(e["date"].split("/")[0])))
    return {"app_key": app_key, "drug": P.DRUG.get(app_key), "cost": round(cost, 3), "events": events}


def main():
    apps = sys.argv[1:] or ["BLA761269"]     # default: Leqembi only
    for app_key in apps:
        t0 = time.time()
        r = run(app_key)
        (OUT / f"{app_key}.json").write_text(json.dumps(r, indent=2))
        if r.get("error"):
            print(f"{r['drug']} ({app_key}): {r['error']}", flush=True); continue
        print(f"\n### {r['drug']} ({app_key})  {len(r['events'])} events  ${r['cost']:.2f}  {time.time()-t0:.0f}s", flush=True)
        for e in r["events"]:
            ch = e["change"]
            if ch:
                v = "" if ch.get("quote_verified", True) else "  [QUOTE UNVERIFIED]"
                print(f"  {e['date']:>8} [{e['category']:8}] {e['section'][:44]}", flush=True)
                print(f"           -> ({ch.get('kind')}) {ch.get('text')}{v}", flush=True)
            else:
                print(f"  {e['date']:>8} [{e['category']:8}] {e['section'][:44]}  -> (no change extracted)", flush=True)


if __name__ == "__main__":
    main()

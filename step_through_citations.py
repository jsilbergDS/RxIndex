#!/usr/bin/env python3
"""Click-through test: for every citation on a card, hit the live /doc route the
way the superscript link does, and check (1) HTTP 200, (2) the cited quote is
actually present in the rendered doc, (3) it anchored (highlighted line)."""
import json, sys, re, urllib.request, urllib.parse, urllib.error

BASE = "http://localhost:7871"

def hit(doc_id, line, quote):
    q = urllib.parse.quote((quote or "")[:60])
    url = f"{BASE}/doc/{doc_id}?line={line}&q={q}"
    try:
        r = urllib.request.urlopen(url, timeout=20)
        html = r.read().decode("utf-8", "replace")
        code = r.getcode()
    except urllib.error.HTTPError as e:
        return e.code, "HTTP_ERROR", url
    except Exception as e:
        return 0, f"EXC:{str(e)[:30]}", url
    qn = re.sub(r"\s+", " ", quote or "").strip().lower()[:35]
    doc_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()
    present = qn in doc_txt if qn else False
    anchored = 'class="ln hl"' in html or "ln hl" in html
    if not present:
        status = "QUOTE_ABSENT"      # doc loaded but the cited quote isn't in it
    elif not anchored:
        status = "no_anchor"         # quote present but no highlighted line
    else:
        status = "OK"
    return code, status, url

def citations(card):
    out = []
    def walk(o, path=""):
        if isinstance(o, dict):
            if isinstance(o.get("citations"), list):
                for c in o["citations"]:
                    if c.get("doc_id"):
                        out.append((path, c))
            for k, v in o.items():
                walk(v, f"{path}/{k}" if path else k)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
    walk(card)
    return out

apps = sys.argv[1:] or ["NDA218436", "NDA219713", "NDA217673"]
grand_ok = grand_tot = 0
for app in apps:
    d = json.load(open(f"data/cards_single/{app}.json"))
    cits = citations(d["card"])
    print(f"\n=== {d['_meta']['drug']} ({app}) — {len(cits)} citations ===")
    npass = 0
    fails = []
    for path, c in cits:
        code, status, url = hit(c["doc_id"], c.get("line", 1), c.get("quote", ""))
        ok = (code == 200 and status in ("OK", "no_anchor"))
        if ok:
            npass += 1
        else:
            fails.append((path, code, status, c.get("quote", "")[:45], c.get("line")))
    grand_ok += npass; grand_tot += len(cits)
    print(f"  RESOLVE {npass}/{len(cits)}  (opens 200 + quote present)")
    for path, code, status, q, line in fails:
        print(f"    FAIL [{code}/{status}] L{line} {path}: {q!r}")
print(f"\nTOTAL RESOLVE {grand_ok}/{grand_tot}")

#!/usr/bin/env python3
"""Ground-truth check: for every citation on a card, pull the doc's OCR straight
from paperclip and confirm the quote appears there, and at (or near) the cited
line. No web route, no PDF — just card citation vs paperclip content.lines."""
import json, sys, re
sys.path.insert(0, '/home/users/jsilberg/.paperclip/lib')
from gxl_paperclip.client import PaperclipClient
pc = PaperclipClient.from_env()

_CACHE = {}
def doc_lines(doc_id):
    if doc_id in _CACHE: return _CACHE[doc_id]
    try:
        raw = pc.execute('cat', ['--full', f'/fda/{doc_id}/content.lines']).output
    except Exception:
        _CACHE[doc_id] = []; return []
    out, cur, buf = [], None, []
    for line in raw.split("\n"):
        m = re.match(r"^L(\d+):\s?(.*)$", line)
        if m:
            if cur is not None: out.append((cur, "\n".join(buf)))
            cur = int(m.group(1)); buf = [m.group(2)]
        else: buf.append(line)
    if cur is not None: out.append((cur, "\n".join(buf)))
    _CACHE[doc_id] = out
    return out

def norm(s): return re.sub(r"\s+", " ", s or "").strip().lower()

def find_quote(doc_id, quote):
    """Return the line number where the (normalized) quote is found, else None."""
    q = norm(quote)
    if not q: return None
    probe = q[:45]
    for n, t in doc_lines(doc_id):
        if probe in norm(t):
            return n
    # multi-line: search the whole normalized doc, report the block start
    full = " ".join(norm(t) for _, t in doc_lines(doc_id))
    if probe in full:
        return -1  # present but spans blocks
    return None

def citations(card):
    out = []
    def walk(o, path=""):
        if isinstance(o, dict):
            if isinstance(o.get("citations"), list):
                for c in o["citations"]:
                    if c.get("doc_id"): out.append((path, c))
            for k, v in o.items(): walk(v, f"{path}/{k}" if path else k)
        elif isinstance(o, list):
            for i, v in enumerate(o): walk(v, f"{path}[{i}]")
    walk(card)
    return out

apps = sys.argv[1:] or ["NDA218436", "NDA219713", "NDA217673"]
gtot = gok = gline = 0
for app in apps:
    d = json.load(open(f"data/cards_single/{app}.json"))
    cits = citations(d["card"])
    ok = line_ok = 0
    fails = []
    for path, c in cits:
        found = find_quote(c["doc_id"], c.get("quote", ""))
        cited = c.get("line")
        if found is None:
            fails.append(("NOT_IN_DOC", path, cited, None, c.get("quote", "")[:50]))
        else:
            ok += 1
            if found == -1 or (cited is not None and abs(found - cited) <= 3):
                line_ok += 1
            else:
                fails.append(("LINE_OFF", path, cited, found, c.get("quote", "")[:50]))
    gtot += len(cits); gok += ok; gline += line_ok
    print(f"\n=== {d['_meta']['drug']} ({app}) — {len(cits)} citations ===")
    print(f"  quote present in paperclip OCR : {ok}/{len(cits)}")
    print(f"  AND at/near the cited line (±3): {line_ok}/{len(cits)}")
    for kind, path, cited, found, q in fails:
        loc = f"cited L{cited}, found L{found}" if kind == "LINE_OFF" else f"cited L{cited}, NOT FOUND"
        print(f"    {kind}: {path} ({loc}) | {q!r}")
print(f"\nTOTAL: quote-present {gok}/{gtot}, line-accurate {gline}/{gtot}")

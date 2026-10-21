#!/usr/bin/env python3
"""Post-approval safety (PAS) pilot — DETERMINISTIC label-version diff.

For each pilot drug: pull its label versions from paperclip, pick the earliest and
latest full label, and diff the boxed warning + Contraindications (sec 4) + Warnings
& Precautions subsection headers (sec 5.x). Every detected change carries the label
doc_id + line number it appears on. NO LLM here — pure text, fully grounded.

Usage: PYTHONUNBUFFERED=1 python3 pas_pilot.py
Output: pas_pilot_out/<app_key>.json  + a printed summary.
"""
import json, re, subprocess, glob
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "pas_pilot_out"; OUT.mkdir(exist_ok=True)

PILOT = ["BLA761269",  # LEQEMBI (the example)
         "NDA214012",  # LEQVIO
         "NDA214665",  # LUMAKRAS
         "NDA215358",  # SCEMBLIX
         "NDA213378",  # LYBALVI
         "NDA215866",  # MOUNJARO
         "BLA761174",  # JEMPERLI
         "BLA761210",  # RYBREVANT
         "NDA214377",  # VERQUVO
         "NDA214662"]  # LIVMARLI

DRUG = {}
for f in glob.glob(str(HERE / "data/cards_new/*.json")):
    m = json.load(open(f))["_meta"]; DRUG[m["type"] + m["appno"].zfill(6)] = m["drug"]


def sh(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=120).stdout


def label_docs(app_key):
    """[(year, doc_id, total_lines)] for this app's label PDFs, one fullest doc per year."""
    q = (f"SELECT document_id, public_url, total_lines "
         f"FROM all_documents WHERE identifier='{app_key}' AND public_url LIKE '%/label/%'")
    out = sh(["paperclip", "sql", "--source", "fda", q])
    rows = []
    for ln in out.splitlines():
        p = [x.strip() for x in ln.split("|")]
        if len(p) == 3 and re.match(r"^[0-9a-f-]{36}$", p[0]):
            did, url, tl = p[0], p[1], (int(p[2]) if p[2].isdigit() else 0)
            if tl < 50:                       # drop empty / near-empty OCR (carton/insert stubs)
                continue
            my = re.search(r"label/(\d{4})", url)
            rows.append({"doc_id": did, "url_year": int(my.group(1)) if my else 0, "lines": tl})
    # TRUE chronological order = the "Revised: M/YYYY" date printed INSIDE each label.
    # The URL year and supplement number both fail to separate two labels dated the same
    # year (e.g. Leqembi Jan-2023 accel approval vs Jul-2023 traditional approval, both
    # url-year 2023 / suppl s000). Read each label's own revision date and sort by it.
    for r in rows:
        rev = _rev_date(r["doc_id"])
        r["rev"] = rev                        # (year, month) or None
        r["sortkey"] = rev if rev else (r["url_year"], 0)
    rows.sort(key=lambda r: r["sortkey"])
    return rows


def _rev_date(doc_id):
    """Parse the label's own 'Revised: M/YYYY' stamp -> (year, month). Latest stamp wins."""
    out = sh(["paperclip", "grep", "-inE", "Revised: ?[0-9]{1,2}/[0-9]{4}",
              f"/fda/{doc_id}/content.lines"])
    dates = [(int(m.group(2)), int(m.group(1)))
             for m in re.finditer(r"Revised:\s*(\d{1,2})/(\d{4})", out)]
    return max(dates) if dates else None


def read_doc(doc_id):
    """Ordered [(lineno, text)] with multi-physical-line L-blocks accumulated.
    Uses `grep -nE '^'` (returns the FULL doc with intrinsic L<n> line numbers);
    `cat` truncates and strips line numbers, so it must NOT be used here."""
    out = sh(["paperclip", "grep", "-nE", "^", f"/fda/{doc_id}/content.lines"])
    blocks = []; cur_n = None; cur = []
    for ln in out.split("\n"):
        m = re.match(r"^L(\d+):\s?(.*)$", ln)
        if m:
            if cur_n is not None:
                blocks.append((cur_n, "\n".join(cur)))
            cur_n = int(m.group(1)); cur = [m.group(2)]
        elif cur_n is not None:
            cur.append(ln)
    if cur_n is not None:
        blocks.append((cur_n, "\n".join(cur)))
    return blocks


def _clean(s):
    # drop trailing ToC/table junk after a tab or 2+ spaces, and boxed continuations
    return re.split(r"\t|  +|\*|See full prescribing", s)[0].strip().rstrip("*").strip()


def extract(blocks):
    boxed = []       # (line, title)
    warn = []        # (line, "5.N Title")
    contra = (None, "")
    for i, (n, t) in enumerate(blocks):
        head = t.strip().split("\n")[0].strip()
        mb = re.match(r"^#{0,3}\s*WARNING:\s*(.+)$", head)
        if mb:
            boxed.append((n, _clean(mb.group(1))))
        mw = re.match(r"^#{0,3}\s*(5\.\d+)\s+(.+)$", head)
        if mw:
            warn.append((n, f"{mw.group(1)} {_clean(mw.group(2))}"))
        # section-4 body: capture following blocks until the next numbered section heading
        if re.match(r"^#{0,3}\s*4\s+CONTRAINDICATIONS\s*$", head) and not contra[1]:
            body = []
            for (nn, tt) in blocks[i + 1:i + 8]:
                h2 = tt.strip().split("\n")[0].strip()
                if re.match(r"^#{0,3}\s*\d+(\.\d+)?\s+[A-Z]", h2):
                    break
                if tt.strip():
                    body.append(re.sub(r"\s+", " ", tt.strip()))
            if body:
                contra = (n, " ".join(body)[:220])

    def dedupe(items):
        seen = set(); out = []
        for n, v in items:
            k = re.sub(r"\s+", " ", v).lower()
            if k not in seen:
                seen.add(k); out.append((n, v))
        return out
    return {"boxed": dedupe(boxed), "warnings": dedupe(warn), "contra": contra}


def norm(s): return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", s.lower())).strip()
def title_only(w): return norm(re.sub(r"^5\.\d+\s*", "", w))


def diff(app_key):
    docs = label_docs(app_key)
    if len(docs) < 1:
        return {"app_key": app_key, "drug": DRUG.get(app_key), "error": "no label docs"}
    first, last = docs[0], docs[-1]     # true earliest and latest label by 'Revised:' date
    d0, d1 = first["doc_id"], last["doc_id"]
    def _vlabel(r):
        rv = r.get("rev")
        return f"{rv[1]}/{rv[0]}" if rv else str(r.get("url_year"))
    y0, y1 = _vlabel(first), _vlabel(last)
    e0, e1 = extract(read_doc(d0)), extract(read_doc(d1))
    changes = []
    # 1) boxed warning
    b0 = {norm(t) for _, t in e0["boxed"]}
    if not e0["boxed"] and e1["boxed"]:
        for n, t in e1["boxed"]:
            changes.append({"type": "boxed_warning_added", "title": t,
                            "doc_id": d1, "line": n, "year": y1})
    else:
        for n, t in e1["boxed"]:
            if norm(t) not in b0:
                changes.append({"type": "boxed_warning_changed", "title": t,
                                "doc_id": d1, "line": n, "year": y1})
    # 2) new sec-5 warnings
    w0 = {title_only(t) for _, t in e0["warnings"]}
    for n, t in e1["warnings"]:
        if title_only(t) and title_only(t) not in w0:
            changes.append({"type": "new_warning", "title": t,
                            "doc_id": d1, "line": n, "year": y1})
    # 3) contraindication change (coarse; skip when current is None/empty)
    c0line, c0txt = e0["contra"]; c1line, c1txt = e1["contra"]
    if norm(c1txt) not in ("", "none") and norm(c1txt) != norm(c0txt):
        changes.append({"type": "contraindications_changed",
                        "initial": c0txt or "(none)", "current": c1txt,
                        "doc_id": d1, "line": c1line, "year": y1})
    return {"app_key": app_key, "drug": DRUG.get(app_key),
            "initial_label": {"year": y0, "doc_id": d0, "boxed_present": bool(e0["boxed"]),
                              "n_warnings": len(e0["warnings"])},
            "current_label": {"year": y1, "doc_id": d1, "boxed_present": bool(e1["boxed"]),
                              "n_warnings": len(e1["warnings"])},
            "changes": changes}


def main():
    for app_key in PILOT:
        r = diff(app_key)
        (OUT / f"{app_key}.json").write_text(json.dumps(r, indent=2))
        if r.get("error"):
            print(f"\n### {r['drug']} ({app_key}) — {r['error']}", flush=True); continue
        il, cl = r["initial_label"], r["current_label"]
        print(f"\n### {r['drug']} ({app_key})  label {il['year']} -> {cl['year']}  "
              f"boxed {il['boxed_present']}->{cl['boxed_present']}  "
              f"#warn {il['n_warnings']}->{cl['n_warnings']}  | {len(r['changes'])} change(s)", flush=True)
        for c in r["changes"]:
            print(f"   [{c['type']}] {c.get('title', c.get('current',''))[:80]}  (L{c.get('line')}, {c.get('year')})", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Post-approval change timeline from the label's own RECENT MAJOR CHANGES box.

For each pilot drug: read every label version paperclip holds (ordered by the
label's 'Revised:' date), parse the 'RECENT MAJOR CHANGES' block from each
(entries look like 'Boxed Warning  7/2023', 'Warnings and Precautions (5.1)  11/2024'),
union them across versions, bucket safety vs efficacy, and print the dated timeline.

NO diffing, NO LLM — the label self-reports what changed and when; every entry is
grounded in a label doc_id + line. Usage: python3 pas_rmc_pilot.py
"""
import json, re, time
from pathlib import Path
import pas_pilot as P                       # reuse label_docs / read_doc / DRUG / PILOT / _rev_date

OUT = Path(__file__).parent / "pas_rmc_out"; OUT.mkdir(exist_ok=True)
DATE_RE = re.compile(r"^(.*?\S)\s+(\d{1,2}/\d{4})\s*$")


def parse_rmc(blocks):
    """[(line, section_label, 'M/YYYY')] from the RECENT MAJOR CHANGES box."""
    idx = None
    for i, (n, t) in enumerate(blocks):
        if re.search(r"RECENT MAJOR CHANGES", t.split("\n")[0], re.I):
            idx = i; break
    if idx is None:
        return []
    events = []
    for j in range(idx, min(idx + 8, len(blocks))):
        n, t = blocks[j]
        head = t.split("\n")[0].strip()
        if j > idx and head.startswith("##"):     # next Highlights section -> stop
            break
        for row in t.split("\n"):
            r = row.strip().lstrip("#").strip()
            if not r or "RECENT MAJOR" in r.upper():
                continue
            m = DATE_RE.match(r)
            if m:
                events.append((n, m.group(1).strip(), m.group(2)))
    return events


def categorize(sect):
    s = sect.lower()
    if s.startswith("boxed warning"):
        return "SAFETY", "boxed_warning"
    m = re.search(r"\((\d+)", sect)          # section NUMBER in the ref is definitive
    num = m.group(1) if m else None
    by_num = {"4": ("SAFETY", "contraindications"), "5": ("SAFETY", "warnings_precautions"),
              "1": ("EFFICACY", "indications"), "2": ("EFFICACY", "dosage")}
    if num in by_num:
        return by_num[num]
    if "contraindication" in s:                     return "SAFETY", "contraindications"
    if "warnings and precautions" in s or s.startswith("warnings"): return "SAFETY", "warnings_precautions"
    if "indications" in s:                          return "EFFICACY", "indications"
    if "dosage" in s:                               return "EFFICACY", "dosage"
    return "OTHER", re.split(r"\(", sect)[0].strip().lower()


def _dkey(d):
    mo, yr = d.split("/")
    return (int(yr), int(mo))


def build(app_key):
    docs = P.label_docs(app_key)
    if not docs:
        return {"app_key": app_key, "drug": P.DRUG.get(app_key), "error": "no label docs"}
    seen = {}
    for r in docs:
        rev = r.get("rev")
        vlabel = f"{rev[1]}/{rev[0]}" if rev else str(r.get("url_year"))
        for line, sect, date in parse_rmc(P.read_doc(r["doc_id"])):
            cat, key = categorize(sect)
            k = (re.sub(r"\s+", " ", sect.lower()), date)      # dedup same entry across versions
            if k not in seen:
                seen[k] = {"date": date, "category": cat, "kind": key, "section": sect,
                           "doc_id": r["doc_id"], "line": line, "found_in_label": vlabel}
    events = sorted(seen.values(), key=lambda e: _dkey(e["date"]))
    return {"app_key": app_key, "drug": P.DRUG.get(app_key),
            "n_label_versions": len(docs),
            "first_label": f"{docs[0].get('rev') and str(docs[0]['rev'][1])+'/'+str(docs[0]['rev'][0])}",
            "events": events}


def main():
    for app_key in P.PILOT:
        r = build(app_key)
        (OUT / f"{app_key}.json").write_text(json.dumps(r, indent=2))
        if r.get("error"):
            print(f"\n### {r['drug']} ({app_key}) — {r['error']}", flush=True); continue
        saf = [e for e in r["events"] if e["category"] == "SAFETY"]
        print(f"\n### {r['drug']} ({app_key})  {r['n_label_versions']} label versions  "
              f"| {len(r['events'])} dated change(s), {len(saf)} safety", flush=True)
        for e in r["events"]:
            print(f"   {e['date']:>8}  [{e['category']:8}] {e['section'][:66]}  (L{e['line']})", flush=True)


if __name__ == "__main__":
    main()

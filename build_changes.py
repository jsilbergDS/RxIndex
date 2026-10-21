#!/usr/bin/env python3
"""Unified 'Changes since approval' per drug, merged by month.

Spine  = ALL approved supplements from Drugs@FDA Submissions.txt (EFFICACY / LABELING /
         REMS; CMC skipped) — the authoritative, can't-miss-one list.
Content = paperclip: efficacy supplements carry a cited description (from the card);
          safety/label changes come from the label's RECENT MAJOR CHANGES (label_changes/).

Output: data/changes/<app>.json  { app_key, boxed_warning_at_approval, entries:[...] }
Usage: python3 build_changes.py [APP ...]   (default: trial set)
"""
import json, glob, csv, re, sys
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).parent
SUBM = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda/fresh_20260714/Submissions.txt")
LOOKUP = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda/fresh_20260714/SubmissionClass_Lookup.txt")
OUT = HERE / "data" / "changes"; OUT.mkdir(parents=True, exist_ok=True)
CLS = {r["SubmissionClassCodeID"]: r["SubmissionClassCode"] for r in csv.DictReader(open(LOOKUP), delimiter="\t")}
KEEP = {"2", "3", "21"}   # EFFICACY, LABELING, REMS  (skip 4 = MANUF/CMC)

TRIAL = ["BLA761269", "NDA214012", "NDA214665", "NDA215358", "NDA213378",
         "NDA215866", "BLA761174", "BLA761210", "NDA214377", "NDA214662"]


def mkey(d):
    d = str(d or "")
    m = re.search(r"(\d{4})-(\d{2})", d)
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{1,2})/(\d{4})", d)
    if m: return (int(m.group(2)), int(m.group(1)))
    return None


def load_subs(appnos):
    """Returns (keep_map, alldate_map).
    keep_map[appno]   = [ {num, class, date} ...]  for tracked classes (EFFICACY/LABELING/REMS)
    alldate_map[appno][date] = [all AP SUPPL nums that day, ANY class] — used to reconstruct
    FDA's COMBINED label filename (co-approved supplements share one PDF, e.g. s012s015lbl.pdf)."""
    keep = defaultdict(list)
    alldate = defaultdict(lambda: defaultdict(list))
    for r in csv.DictReader(open(SUBM, encoding="latin-1"), delimiter="\t"):
        if not (r["SubmissionType"] == "SUPPL" and r["SubmissionStatus"] == "AP"
                and r["ApplNo"].lstrip("0") in appnos):
            continue
        a = r["ApplNo"].lstrip("0"); date = (r.get("SubmissionStatusDate") or "")[:10]
        alldate[a][date].append(int(r["SubmissionNo"]))
        if r["SubmissionClassCodeID"] in KEEP:
            keep[a].append({"num": int(r["SubmissionNo"]), "class": CLS[r["SubmissionClassCodeID"]], "date": date})
    return keep, alldate


def doc_urls(app_key):
    """{supplement_number: FDA doc URL} from paperclip (prefer the label, else the letter).

    Handles COMBINED filenames — one PDF can cover several co-approved supplements, e.g.
    214985s010s012lbl.pdf (→ 10 AND 12) or 761269Orig1s001lbl.pdf, or a cross-application
    combined label 761195s004,761304s003lbl.pdf (→ this app's s004). Every supplement number
    in THIS app's run is mapped to the same URL."""
    import subprocess
    appno = re.sub(r"\D", "", app_key)
    q = ("SELECT substring(public_url from '(.*/drugsatfda_docs/[a-z]+)/'), "
         "substring(public_url from '/([0-9]{4})/'), substring(public_url from '[^/]+\\.pdf$') "
         f"FROM all_documents WHERE identifier='{app_key}' AND public_url LIKE '%.pdf'")
    out = subprocess.run(["paperclip", "sql", "--source", "fda", q], capture_output=True, text=True, timeout=60).stdout
    res = {}
    for ln in out.splitlines():
        p = [x.strip() for x in ln.split("|")]
        if len(p) != 3 or not p[2].lower().endswith(".pdf") or not p[1].isdigit():
            continue
        prefix, year, fn = p
        low = fn.lower()
        kind = "lbl" if "lbl" in low else ("ltr" if "ltr" in low else None)
        if not kind:
            continue
        m = re.search(rf"{appno}(?:orig\d+)?((?:s0*\d+)+)", low)   # this app's supplement run
        if not m:
            continue
        nums = [int(x) for x in re.findall(r"s0*(\d+)", m.group(1))]
        url = f"{prefix}/{year}/{fn}"
        for num in nums:
            if num not in res or kind == "lbl":
                res[num] = (url, kind)
    return {n: u for n, (u, _) in res.items()}


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _lbl(appno, year, nums):
    return (f"https://www.accessdata.fda.gov/drugsatfda_docs/label/{year}/"
            f"{appno}" + "".join(f"s{n:03d}" for n in nums) + "lbl.pdf")


def resolve_supp_url(appno, num, date, siblings):
    """FDA's label-PDF URL for a supplement not in the paperclip corpus. Co-approved
    supplements share ONE combined PDF (e.g. s012s015lbl.pdf), so try the same-day
    combined filename before the single. Returns (url, resolves)."""
    k = mkey(date); year = k[0] if k else ""
    sibs = sorted(set(siblings) | {num})
    cands = []
    if len(sibs) > 1:
        cands.append(_lbl(appno, year, sibs))     # combined label for the day
    cands.append(_lbl(appno, year, [num]))        # single-supplement label
    for c in cands:
        if url_resolves(c):
            return c, True
    return cands[-1], False                        # display the single form; flag as no-PDF


def url_resolves(url):
    """True only if the URL returns 200 application/pdf. FDA's WAF blocks the default
    curl UA (302 -> abuse-detection page), so a browser UA is required."""
    import subprocess
    try:
        out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-L", "-m", "25", "-A", _UA,
                              "-w", "%{http_code} %{content_type}", url],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        code, _, ct = out.partition(" ")
        return code == "200" and "pdf" in ct
    except Exception:
        return False


def _pretty_sections(events):
    """Readable, deduped RMC subsection labels (the specific change), e.g.
    'Warnings and Precautions, Hyperglycemia (5.3)' -> 'Hyperglycemia (5.3)'.
    Dedupes by section number; drops boxed (handled separately)."""
    out = {}
    for e in events:
        s = (e.get("section") or "").strip()
        if not s or s.lower().startswith("boxed"):
            continue
        core = s
        for pre in (r"warnings,?\s*and\s*precautions", r"contraindications?",
                    r"dosage\s+and\s+administration", r"indications?\s+and\s+usage"):
            core = re.sub(rf"^\s*{pre},?\s*", "", core, flags=re.I)
        core = re.sub(r"\((\d+)\s*[.,]\s*(\d+)\)", r"(\1.\2)", core)   # OCR "(5, 2)" -> "(5.2)"
        core = re.sub(r"\s+", " ", core).strip()
        m = re.search(r"\((\d+(?:\.\d+)?)\)", core)
        key = m.group(1) if m else re.sub(r"\s*\(.*", "", core).lower()
        if key not in out or len(core) > len(out[key]):
            out[key] = core
    return "; ".join(v for v in out.values() if re.search(r"[A-Za-z]", v))


def build(app_key, subs, alldate=None):
    urls = doc_urls(app_key)
    card = json.load(open(HERE / "data" / "cards_new" / f"{app_key}.json"))
    appno = str(card["_meta"].get("appno", "")).lstrip("0")
    dgroups = (alldate or {}).get(appno, {})
    boxed_at = bool((card.get("boxed_warning") or {}).get("present"))
    lcp = HERE / "data" / "label_changes" / f"{app_key}.json"
    lc = json.load(open(lcp)) if lcp.exists() else {"events": []}

    months = defaultdict(lambda: {"classes": set(), "nums": [], "urls": [], "supp_docs": [], "eff_supp": None, "safety": [], "eff_rmc": [], "rems": False})
    for s in subs.get(appno, []):
        k = mkey(s["date"])
        if not k: continue
        months[k]["classes"].add(s["class"]); months[k]["nums"].append(s["num"])
        in_corpus = s["num"] in urls
        if in_corpus:
            u, resolves = urls[s["num"]], True            # corpus PDFs are known-good
        else:
            u, resolves = resolve_supp_url(appno, s["num"], s["date"], dgroups.get(s["date"], []))
        months[k]["urls"].append(u)
        months[k]["supp_docs"].append({"num": s["num"], "url": u, "in_corpus": in_corpus, "resolves": resolves})
        if s["class"] == "REMS": months[k]["rems"] = True
    for s in (card.get("supplements") or []):
        k = mkey(s.get("approval_date"))
        if not k: continue
        months[k]["classes"].add("EFFICACY")
        if isinstance(s.get("description"), dict) and s["description"].get("text"):
            months[k]["eff_supp"] = s
    for e in lc.get("events", []):
        k = mkey(e["date"])
        if not k: continue
        (months[k]["safety"] if e.get("category") == "SAFETY" else months[k]["eff_rmc"]).append(e)

    seen_boxed = boxed_at
    entries = []
    for k in sorted(months):
        m = months[k]; y, mo = k
        safety_summary = safety_src = None
        if m["safety"]:
            secs = [e.get("section", "") for e in m["safety"]]
            boxed = any(s.lower().startswith("boxed") for s in secs)
            parts = []
            if boxed:
                parts.append("Boxed warning added" if not seen_boxed else "Boxed warning revised"); seen_boxed = True
            detail = _pretty_sections(m["safety"])   # the specific subsection(s) that changed
            joined = " ".join(secs).lower()
            if detail:
                area = ("Contraindications" if re.search(r"\(4", joined) and not re.search(r"\(5", joined)
                        else "Warnings & Precautions")
                parts.append(f"{area}: {detail}")
            elif re.search(r"\(4", joined):
                parts.append("Contraindications updated")
            elif re.search(r"\(5", joined) or "warnings" in joined:
                parts.append("Warnings & Precautions updated")
            safety_summary = ("; ".join(parts) or "Safety labeling updated")
            src = next((e for e in m["safety"] if e.get("section", "").lower().startswith("boxed")), m["safety"][0])
            safety_src = {"doc_id": src.get("doc_id"), "line": src.get("line")}
        eff_note = eff_src = None
        if not m["eff_supp"] and m["eff_rmc"]:
            secs = [e.get("section", "") for e in m["eff_rmc"]]
            joined = " ".join(secs).lower()
            detail = _pretty_sections(m["eff_rmc"])   # subsection where the section body carries one (e.g. Dosage 2.x)
            if re.search(r"\(2", joined) or "dosage" in joined:
                eff_note = f"Dosage & Administration: {detail}" if detail else "Dosing updated"
            elif re.search(r"\(1", joined) or "indication" in joined:
                eff_note = f"Indications & Usage: {detail}" if detail else "Indication / usage updated"
            else:
                eff_note = detail or None
            if eff_note:
                se = m["eff_rmc"][0]; eff_src = {"doc_id": se.get("doc_id"), "line": se.get("line")}
        entries.append({"date": f"{mo}/{y}", "classes": sorted(m["classes"]),
                        "efficacy_supp": m["eff_supp"], "efficacy_note": eff_note, "eff_src": eff_src,
                        "safety_summary": safety_summary, "safety_src": safety_src, "rems": m["rems"],
                        "suppl_url": (m["urls"][0] if m["urls"] else None),
                        "suppl_docs": sorted(m["supp_docs"], key=lambda x: x["num"])})
    return {"app_key": app_key, "boxed_warning_at_approval": boxed_at, "entries": entries}


def main():
    apps = sys.argv[1:] or TRIAL
    appnos = {str(json.load(open(HERE / "data" / "cards_new" / f"{a}.json"))["_meta"]["appno"]).lstrip("0")
              for a in apps if (HERE / "data" / "cards_new" / f"{a}.json").exists()}
    subs, alldate = load_subs(appnos)
    for a in apps:
        try:
            r = build(a, subs, alldate)
        except FileNotFoundError:
            print(f"{a}: no card", flush=True); continue
        (OUT / f"{a}.json").write_text(json.dumps(r, indent=2))
        print(f"\n{r['app_key']}  (boxed at approval: {r['boxed_warning_at_approval']})", flush=True)
        blank = 0
        for e in r["entries"]:
            eff = (((e.get("efficacy_supp") or {}).get("description") or {}).get("text")) or e.get("efficacy_note") or ""
            bits = "/".join(e["classes"]) or "—"
            desc = eff or e["safety_summary"] or ("REMS supplement" if e["rems"] else "")
            saf = f"  ‹safety: {e['safety_summary']}›" if (e["safety_summary"] and eff) else ""
            if not desc: blank += 1
            print(f"    {e['date']:>8}  [{bits:20}]  {desc[:66] or '(no content — labeling supplement not in label record)'}{saf}", flush=True)
        print(f"    -> {blank}/{len(r['entries'])} entries with no describable content", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Backfill missing pivotal-trial NCT ids from ClinicalTrials.gov (no LLM).

FDA reviews often don't print NCT numbers (60% of pivotal trials had none, Oct 2026).
The cards usually DO name the trial by sponsor study id (e.g. "TPU-TAS-102-301",
"CLDE225A2201", "20120296") or acronym ("FIREFISH"). ClinicalTrials.gov records those
as OrgStudyId / SecondaryId / Acronym, so we look them up.

Rules (precision first -- leave blank rather than guess):
  * candidates come from the trial_name text only;
  * a candidate is accepted only if ClinicalTrials.gov returns EXACTLY ONE study;
  * study-id matches: the matched record must also mention the drug (ingredient / brand
    in interventions or title) -- unless the id is long and alphanumeric (>=8 chars with
    letters and digits), which is specific enough on its own;
  * acronym matches are always searched together with the ingredient (query.intr).
The FDA-cited `nct` field is NEVER changed. Results go in trial["nct_ctgov"] =
{"nct", "matched_on", "via", "title"} and are shown as "via ClinicalTrials.gov".

Usage:
  python3 backfill_nct_ctgov.py --validate   # run on trials that HAVE an FDA-cited NCT; report agreement; writes nothing
  python3 backfill_nct_ctgov.py --apply [APP ...]   # fill missing NCTs + flag FDA-vs-registry conflicts (skips human-edited cards)
"""
import json, re, sys, time, glob, urllib.request, urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

HERE = Path(__file__).parent
SRC = HERE / "data" / "cards_single"
CACHE_P = HERE / "data" / "ctgov_cache_v2.json"   # v2: stores secondary ids
CACHE = json.load(open(CACHE_P)) if CACHE_P.exists() else {}
NCT = re.compile(r"NCT\d{8}")
STOP = {"STUDY", "TRIAL", "PHASE", "PART", "COHORT", "OPEN", "LABEL", "NSCLC", "SCLC", "AML", "CLL",
        "HER2", "EGFR", "ALK", "ROS1", "BRAF", "KRAS", "FDA", "NDA", "BLA", "USA", "LGS", "MS", "RMS",
        "CML", "ALL", "MCL", "DLBCL", "SMA", "DMD", "HIV", "HCV", "HBV", "COPD", "GIST", "RCC", "HCC",
        "PFS", "ORR", "DOR", "BICR", "IRC", "LDL", "ITT", "MITT", "PBO", "EXT", "OLE", "PIVOTAL", "ONLY",
        "AND", "WITH", "THE", "DOUBLE", "BLIND", "PLACEBO", "RANDOMIZED", "STUDIES", "TRIALS"}

def ctgov(term, intr=None):
    key = f"{term}||{intr}"
    if key in CACHE:
        return CACHE[key]
    p = {"query.term": term, "pageSize": "5",
         "fields": "NCTId,BriefTitle,OfficialTitle,OrgStudyId,SecondaryId,Acronym,InterventionName"}
    if intr:
        p["query.intr"] = intr
    u = "https://clinicaltrials.gov/api/v2/studies?" + urllib.parse.urlencode(p)
    for attempt in range(4):
        try:
            d = json.load(urllib.request.urlopen(u, timeout=30)); break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return None   # network failure: don't cache
    out = []
    for s in d.get("studies", []):
        ps = s["protocolSection"]; im = ps["identificationModule"]
        out.append({"nct": im["nctId"], "title": im.get("briefTitle", ""),
                    "official": im.get("officialTitle", ""), "acr": im.get("acronym"),
                    "org": (im.get("orgStudyIdInfo") or {}).get("id"),
                    "sec": [x.get("id", "") for x in im.get("secondaryIdInfos", [])],
                    "interv": [i.get("name", "") for i in (ps.get("armsInterventionsModule") or {}).get("interventions", [])]})
    CACHE[key] = out
    return out

def candidates(name):
    """(kind, token) from a trial name, most specific first."""
    s = name or ""
    ids, acrs = [], []
    for m in re.finditer(r"\b[A-Za-z0-9]+(?:[-._/][A-Za-z0-9]+)+\b", s):            # hyphenated codes
        t = m.group(0).strip(".")
        if re.search(r"\d", t) and len(t) >= 5 and not NCT.match(t):
            ids.append(t)
    for m in re.finditer(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{6,}\b", s):  # GO29293, CLDE225A2201
        if not NCT.match(m.group(0)) and m.group(0) not in ids:
            ids.append(m.group(0))
    for m in re.finditer(r"\b\d{6,}\b", s):                                          # 20120296
        ids.append(m.group(0))
    for m in re.finditer(r"\b[A-Z][A-Z]{3,}(?:[- ]?[0-9]{1,3}[A-Z]?)?\b", s):         # FIREFISH, MONALEESA-2
        t = m.group(0)
        if t.split("-")[0].split(" ")[0] not in STOP and t not in ids:
            acrs.append(t)
    # don't fall back to a fragment of a longer id ("ASTX727" from "ASTX727-01-B" is a drug code)
    ids = [x for x in ids if not any(x != y and x.lower() in y.lower() for y in ids)]
    seen, out = set(), []
    for k, t in [("id", x) for x in ids] + [("acronym", x) for x in acrs]:
        if t.lower() not in seen:
            seen.add(t.lower()); out.append((k, t))
    return out

def mentions_drug(study, drug_terms):
    blob = " ".join([study["title"], study["official"], " ".join(study["interv"])]).lower()
    return any(t in blob for t in drug_terms)

_SUB = re.compile(r"sub-?study|extension|roll-?over|long-term follow|continuation", re.I)

def _n(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())

def lookup(trial_name, drug_terms, ingredient):
    # ignore trailing mentions of supportive studies ("... (with supportive Study AALL07P4)")
    name = re.split(r"\bsupportiv", trial_name or "", flags=re.I)[0]
    for kind, tok in candidates(name):
        if kind == "id":
            res = ctgov(f'AREA[OrgStudyId]"{tok}" OR AREA[SecondaryId]"{tok}"')
            if res is None or len(res) != 1:
                continue
            st = res[0]
            # exact id equality (search hits on partial ids / drug codes like "BHV-3000" are rejected)
            if _n(tok) not in {_n(st["org"])} | {_n(x) for x in st.get("sec", [])}:
                continue
            if _SUB.search(st["title"]):
                continue
            ndig = len(re.findall(r"\d", tok))
            specific = ndig >= 3 and len(tok) >= 8 and re.search(r"[A-Za-z]", tok)
            if mentions_drug(st, drug_terms) or specific:
                return {"nct": st["nct"], "matched_on": f"study id {tok}", "via": "ClinicalTrials.gov",
                        "title": st["title"]}
        elif ingredient:
            res = ctgov(f'AREA[Acronym]"{tok}"', ingredient)
            if res and len(res) == 1 and _n(res[0].get("acr")) == _n(tok) and not _SUB.search(res[0]["title"]):
                st = res[0]
                return {"nct": st["nct"], "matched_on": f"acronym {tok} + {ingredient}",
                        "via": "ClinicalTrials.gov", "title": st["title"]}
    return None

def drug_info(d):
    m = d.get("_meta", {}) or {}
    act = (m.get("active") or "").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", act) if len(w) >= 5]
    terms = set(words[:2])
    if m.get("drug"): terms.add(m["drug"].lower())
    return terms, (words[0] if words else "")

def trial_text(t):
    v = t.get("trial_name")
    return v.get("text", "") if isinstance(v, dict) else str(v or "")

def cited_nct(t):
    """NCTs the FDA-cited field actually assigns to THIS trial: only when the text starts
    with an NCT (e.g. "Not stated for Study 1108 (the Phase III trial is NCT02516241)"
    names a different trial and must not count)."""
    v = t.get("nct"); txt = (v.get("text") or "") if isinstance(v, dict) else str(v or "")
    txt = re.sub(r"NCT\s+(\d{8})", r"NCT\1", txt.strip())
    return set(NCT.findall(txt)) if re.match(r"NCT\d{8}", txt) else set()

def main():
    validate = "--validate" in sys.argv
    apps = [a for a in sys.argv[1:] if not a.startswith("--")] or [Path(f).stem for f in sorted(glob.glob(str(SRC / "*.json")))]
    if not validate:
        import human_edits as HE
        apps = HE.filter_apps(apps, "nct_ctgov")
    jobs = []
    for a in apps:
        d = json.load(open(SRC / f"{a}.json"))
        if not isinstance(d.get("card"), dict): continue
        terms, ing = drug_info(d)
        for i, t in enumerate(d["card"].get("pivotal_trials") or []):
            has = cited_nct(t)
            if validate and has:
                jobs.append((a, i, trial_text(t), terms, ing, has))
            if not validate:
                jobs.append((a, i, trial_text(t), terms, ing, has or None))
    print(f"{'VALIDATE' if validate else 'APPLY'}: {len(jobs)} trials", flush=True)
    with ThreadPoolExecutor(4) as ex:
        res = list(tqdm(ex.map(lambda j: lookup(j[2], j[3], j[4]), jobs), total=len(jobs), desc="ctgov"))
    CACHE_P.write_text(json.dumps(CACHE))
    if validate:
        found = [(j, r) for j, r in zip(jobs, res) if r]
        agree = sum(1 for j, r in found if r["nct"] in j[5])
        print(f"lookup returned an NCT for {len(found)}/{len(jobs)} trials that have an FDA-cited NCT; "
              f"agrees with FDA-cited NCT: {agree}/{len(found)} ({100*agree/max(len(found),1):.1f}%)", flush=True)
        for j, r in found:
            if r["nct"] not in j[5]:
                print(f"  DISAGREE {j[0]} [{j[2][:60]}] fda={sorted(j[5])} ctgov={r['nct']} ({r['matched_on']}) {r['title'][:60]}", flush=True)
        return
    # missing cited NCT -> fill; cited NCT that ClinicalTrials.gov contradicts for the SAME
    # study id -> keep the FDA text, add the registry NCT + a visible conflict note.
    by_app, filled, conflicts = {}, 0, []
    for j, r in zip(jobs, res):
        if j[5] is not None and (not r or r["nct"] in j[5]):
            by_app.setdefault(j[0], []).append((j[1], None, None))   # cited NCT confirmed / unchecked: clear stale flags
            continue
        if not r:
            continue
        if j[5] is None:
            by_app.setdefault(j[0], []).append((j[1], r, None)); filled += 1
        elif r["nct"] not in j[5] and r["matched_on"].startswith("study id"):
            note = (f"FDA document lists {', '.join(sorted(j[5]))} for this trial; ClinicalTrials.gov registers "
                    f"{r['matched_on'][len('study id '):]} as {r['nct']}.")
            by_app.setdefault(j[0], []).append((j[1], r, note)); conflicts.append((j[0], note))
    for a, items in by_app.items():
        p = SRC / f"{a}.json"; d = json.load(open(p))
        for i, r, note in items:
            tr = d["card"]["pivotal_trials"][i]
            if r is None:
                tr.pop("nct_conflict", None); tr.pop("nct_ctgov", None)
                continue
            tr["nct_ctgov"] = r
            if note: tr["nct_conflict"] = note
            else: tr.pop("nct_conflict", None)
        new = json.dumps(d, indent=2)
        if new != p.read_text():
            p.write_text(new)
    n_missing = sum(1 for j in jobs if j[5] is None)
    print(f"filled {filled}/{n_missing} missing NCTs; flagged {len(conflicts)} FDA-vs-registry conflicts", flush=True)
    for a, note in conflicts:
        print(f"  CONFLICT {a}: {note}", flush=True)

if __name__ == "__main__":
    main()

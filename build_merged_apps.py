#!/usr/bin/env python3
"""Bake data/merged_apps.json: how cards for the same brand are combined.

CDER lists each application separately, so one drug can have several cards (e.g.
Briviact tablets / oral solution / injection = 3 NDAs). Two cases:
  - MERGE: same indication on every application (a formulation set) -> show ONE card
    (the one with the most citations) listing all application numbers + dosage forms.
  - SEE_ALSO: applications carry DIFFERENT indications/evidence (Sutent GIST vs RCC,
    Sabril adults vs infantile spasms, ...) -> keep every card, cross-link them.
Nothing is deleted; the secondary card files stay in data/cards_single + cards_new.
The app reads this file (Fly has no Drugs@FDA files, so forms are baked in here).

Usage: python3 build_merged_apps.py
"""
import json, glob, csv, collections
from pathlib import Path

HERE = Path(__file__).parent
PRODUCTS = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda/fresh_20260714/Products.txt")

# Brands whose applications differ in indication / evidence -> keep separate, cross-link.
# (Reviewed by hand Oct 2026; add a brand here if its applications' indications differ.)
SEE_ALSO_BRANDS = {"AKYNZEO", "SUTENT", "SABRIL", "ZYDELIG", "DATROWAY", "IOMERVU"}

cards = {Path(f).stem: json.load(open(f)) for f in glob.glob(str(HERE / "data" / "cards_new" / "*.json"))}
rec = {}
for ln in open(HERE / "data" / "cards_recited_new.jsonl"):
    if ln.strip():
        r = json.loads(ln); rec[r["app_key"]] = r["card"]

def ncit(o):
    n = 0
    if isinstance(o, dict):
        if isinstance(o.get("citations"), list): n += len(o["citations"])
        for v in o.values(): n += ncit(v)
    elif isinstance(o, list):
        for v in o: n += ncit(v)
    return n

forms = collections.defaultdict(set)
with open(PRODUCTS, encoding="latin-1") as f:
    for r in csv.DictReader(f, delimiter="\t"):
        forms[r["ApplNo"].lstrip("0")].add(r["Form"].strip())

groups = collections.defaultdict(list)
for a, d in cards.items():
    groups[(d["_meta"].get("drug") or "").upper()].append(a)

out = {"merge": {}, "groups": {}, "see_also": {}, "apps": {}}
for brand, apps in sorted(groups.items()):
    if len(apps) < 2 or not brand:
        continue
    for a in apps:
        m = cards[a]["_meta"]
        out["apps"][a] = {"form": " | ".join(sorted(forms.get(a[3:].lstrip("0"), []))) or "",
                          "approval_date": m.get("approval_date") or "",
                          "indication": (m.get("indication") or "")[:240]}
    if brand in SEE_ALSO_BRANDS:
        for a in apps:
            out["see_also"][a] = sorted(x for x in apps if x != a)
    else:
        primary = sorted(apps, key=lambda a: (-(ncit(rec.get(a, {})) + ncit(cards[a])), a))[0]
        out["groups"][primary] = sorted(apps)
        for a in apps:
            if a != primary:
                out["merge"][a] = primary

(HERE / "data" / "merged_apps.json").write_text(json.dumps(out, indent=1))
print(f"merged groups: {len(out['groups'])} (hiding {len(out['merge'])} secondary cards) | "
      f"see-also cards: {len(out['see_also'])}", flush=True)
for p, g in out["groups"].items():
    print(f"  MERGE {cards[p]['_meta'].get('drug'):<12} primary {p}: " +
          ", ".join(f"{a} [{out['apps'][a]['form']}]" for a in g), flush=True)
for a, o in sorted(out["see_also"].items()):
    print(f"  SEE-ALSO {cards[a]['_meta'].get('drug'):<10} {a} -> {o}", flush=True)

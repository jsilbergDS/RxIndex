#!/usr/bin/env python3
"""Merge data from the existing AdComm Explorer dataset into cards.

For each card whose appno is in the AdComm dataset, add an
advisory_committee_detail field with meeting info, overall recommendation,
and concern->change linkages.

Usage:
  python merge_adcomm_data.py <cards.jsonl>
"""
import os, sys, json

if len(sys.argv) < 2: print(__doc__); sys.exit(1)
PATH = sys.argv[1]

adcomm = json.load(open('/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/fixed_AdCOMM_project/adcomm_explorer_data.json'))
ad_by_app = {}
for e in adcomm:
    nda = (e.get('nda') or '').strip()
    if nda: ad_by_app[nda.zfill(6)] = e

cards = [json.loads(l) for l in open(PATH)]
n_merged = 0
for c in cards:
    appno = (c.get('_meta',{}).get('appno') or '').zfill(6)
    ad = ad_by_app.get(appno)
    if not ad: continue
    meeting = ad.get('meeting') or {}
    detail = {
        'meeting_date': meeting.get('meeting_date') or '',
        'committee_name': meeting.get('committee_name') or '',
        'overall_recommendation': meeting.get('overall_recommendation') or '',
        'therapeutic_area': ad.get('therapeutic_area') or '',
        'linkages': ad.get('linkages') or [],
        'num_linkages': ad.get('num_linkages') or 0,
        'source': 'adcomm_explorer_dataset',
    }
    c['advisory_committee_detail'] = detail
    n_merged += 1
    print(f"Merged AdComm detail for {c['_meta'].get('drug','?'):<22} ({appno}, {detail['num_linkages']} linkages)", flush=True)

with open(PATH + ".tmp", "w") as f: f.write("\n".join(json.dumps(d) for d in cards) + "\n")
os.replace(PATH + ".tmp", PATH)
print(f"\nMerged {n_merged} cards with AdComm Explorer data")

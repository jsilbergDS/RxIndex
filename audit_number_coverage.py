#!/usr/bin/env python3
"""Honest per-number citation coverage for the NEW single-agent cards.

For every number appearing in a card's field TEXT, check whether some citation
on that field has a quote containing that same number. This measures whether the
numbers a reader sees are actually backed — which the earlier verify (quote grep-
matches doc) did NOT check."""
import json, glob, re

NUM = re.compile(r'\d+(?:\.\d+)?')
CI = {'95', '97.5', '90', '99', '2.5'}

def field_numbers(txt):
    out = []
    for m in NUM.finditer(txt or ''):
        d = m.group(0)
        if d in CI:
            continue
        # skip digits embedded in identifiers (letter directly adjacent)
        s, e = m.start(), m.end()
        if (s > 0 and txt[s-1].isalpha()) or (e < len(txt) and txt[e].isalpha()):
            continue
        out.append(d)
    return out

def covered(num, cites):
    joined = ' '.join(re.sub(r'\s+', ' ', c.get('quote', '')) for c in cites)
    return re.search(r'(?<!\d)' + re.escape(num) + r'(?!\d)', joined) is not None

def walk_fields(card):
    """yield (field_label, {text,citations}) for every cited leaf."""
    for k in ('key_safety', 'target_moa', 'indication', 'modality'):
        v = card.get(k)
        if isinstance(v, dict) and 'text' in v:
            yield k, v
    for i, t in enumerate(card.get('pivotal_trials') or []):
        for k in ('result', 'endpoint', 'n', 'design', 'population'):
            v = t.get(k)
            if isinstance(v, dict) and 'text' in v:
                yield f'trial{i}.{k}', v
    ap = card.get('approval_program') or {}
    for pk, pv in ap.items():
        if isinstance(pv, dict):
            for dk, dv in pv.items():
                if isinstance(dv, dict) and 'text' in dv:
                    yield f'{pk}.{dk}', dv

tot = cov = 0
per_card = []
worst_fields = []
for f in sorted(glob.glob('data/cards_single/*.json')):
    d = json.load(open(f))
    drug = d['_meta'].get('drug', '')
    ct = cc = 0
    for label, field in walk_fields(d['card']):
        nums = field_numbers(field.get('text'))
        cites = field.get('citations') or []
        miss = [n for n in nums if not covered(n, cites)]
        ct += len(nums); cc += len(nums) - len(miss)
        if miss:
            worst_fields.append((drug, label, len(miss), len(nums), field.get('text', '')[:70]))
    tot += ct; cov += cc
    per_card.append((drug, cc, ct))

print("PER-NUMBER COVERAGE (number in text backed by a citation quote containing it):\n")
for drug, cc, ct in per_card:
    flag = '  <-- GAPS' if ct and cc < ct else ''
    pct = f'{100*cc//ct}%' if ct else '--'
    print(f'  {drug[:15]:<15} {cc}/{ct} ({pct}){flag}')
print(f'\nTOTAL: {cov}/{tot} = {100*cov//max(tot,1)}% of visible numbers are cited-with-that-number')
print(f'\nWORST fields (most uncovered numbers):')
for drug, label, nmiss, nt, txt in sorted(worst_fields, key=lambda x: -x[2])[:12]:
    print(f'  {drug[:14]:<14} {label:<22} {nmiss}/{nt} uncovered | {txt}')

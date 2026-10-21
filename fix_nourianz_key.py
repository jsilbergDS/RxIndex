#!/usr/bin/env python3
"""One-off fix: Nourianz was built under the unpadded key NDA22075, so load_meta()
(exact ApplNo match) returned nothing -> empty drug/sponsor/approval_date, and the
changes/label_changes builders found nothing. Re-key everything to NDA022075.

1. back up every NDA22075 file to data/cards_single_fix_backup/NDA22075_rekey/
2. cards_single: app_key -> NDA022075, fill empty _meta fields from load_meta()
3. cards_critic / adcomm_out: re-key (adcomm 'drug' -> real name)
4. remove the old NDA22075 files (backed up)
Then (outside this script): build_label_changes / build_changes / bundle_label_docs /
convert_single_to_app for NDA022075. No API calls.
"""
import json, shutil
from pathlib import Path
import run_card_single as B

HERE = Path(__file__).parent
OLD, NEW = "NDA22075", "NDA022075"
BK = HERE / "data" / "cards_single_fix_backup" / "NDA22075_rekey"
BK.mkdir(parents=True, exist_ok=True)

olds = {
    "cards_single": HERE / "data" / "cards_single" / f"{OLD}.json",
    "cards_new": HERE / "data" / "cards_new" / f"{OLD}.json",
    "cards_critic": HERE / "data" / "cards_critic" / f"{OLD}.json",
    "adcomm_out": HERE / "adcomm_out" / f"{OLD}.json",
    "changes": HERE / "data" / "changes" / f"{OLD}.json",
    "label_changes": HERE / "data" / "label_changes" / f"{OLD}.json",
}
for k, p in olds.items():
    assert p.exists(), p
    assert not (p.parent / f"{NEW}.json").exists(), f"{NEW} already exists in {p.parent}"
    shutil.copy2(p, BK / f"{k}__{OLD}.json")
print("backed up ->", BK, flush=True)

meta = B.load_meta(NEW)
assert meta.get("drug") == "NOURIANZ", meta

# cards_single
d = json.load(open(olds["cards_single"]))
d["app_key"] = NEW
for k, v in meta.items():
    if not d["_meta"].get(k):
        d["_meta"][k] = v
d["_meta"]["appno"] = meta["appno"]
(olds["cards_single"].parent / f"{NEW}.json").write_text(json.dumps(d, indent=2))
print("cards_single _meta:", json.dumps(d["_meta"]), flush=True)

# cards_critic
c = json.load(open(olds["cards_critic"]))
if c.get("app") == OLD: c["app"] = NEW
(olds["cards_critic"].parent / f"{NEW}.json").write_text(json.dumps(c, indent=2))

# adcomm_out
a = json.load(open(olds["adcomm_out"]))
if a.get("app") == OLD: a["app"] = NEW
if a.get("drug") == OLD: a["drug"] = meta["drug"]
(olds["adcomm_out"].parent / f"{NEW}.json").write_text(json.dumps(a, indent=2))

# remove old keyed files (all backed up above); changes/label_changes are rebuilt next
for p in olds.values():
    p.unlink()
print("removed old NDA22075 files", flush=True)

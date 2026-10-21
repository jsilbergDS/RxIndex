#!/bin/bash
# Finish the r189 batch:
#  - BLA125409 (Perjeta) card was invalid JSON -> back up, rebuild, run all per-drug stages
#  - shard 0 crashed in run_therapeutic_area on that card -> TA for the 18 apps after it,
#    and run_adcomm never ran for shard 0
#  - global finish again (convert FIRST), changes for BLA125409 only
cd /oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project/flask_hf
PY=/home/groups/akundaje/jsilberg/miniconda3/bin/python
export PYTHONUNBUFFERED=1

SHARD0="NDA022454 NDA022405 NDA022345 NDA202570 NDA203388 BLA125409 NDA202811 NDA203585 NDA202155 NDA202207 NDA201292 NDA022416 BLA125460 BLA125496 NDA206256 BLA125499 NDA022535 BLA125554 NDA207500 NDA206038 NDA206500 BLA125513 BLA125547 NDA216017"
TA_MISSING="NDA202811 NDA203585 NDA202155 NDA202207 NDA201292 NDA022416 BLA125460 BLA125496 NDA206256 BLA125499 NDA022535 BLA125554 NDA207500 NDA206038 NDA206500 BLA125513 BLA125547 NDA216017"

cp -n data/cards_single/BLA125409.json data/cards_single_fix_backup/BLA125409.r189_invalid_json.json

echo "== rebuild BLA125409 =="
$PY -u run_card_single.py BLA125409
$PY -u run_heal_loop.py BLA125409
$PY -u supplements_extract.py BLA125409
$PY -u backfill_pmr.py BLA125409

echo "== therapeutic area: BLA125409 + 18 shard-0 apps =="
$PY -u run_therapeutic_area.py BLA125409 $TA_MISSING

echo "== adcomm: shard 0 =="
$PY -u run_adcomm.py adcomm_out 48 $SHARD0

echo "== global finish (convert FIRST) =="
$PY -u convert_single_to_app.py
$PY -u bundle_cited_docs.py
$PY -u bundle_adcomm_docs.py
$PY -u build_label_changes.py BLA125409
$PY -u build_changes.py BLA125409
$PY -u bundle_label_docs.py
echo FINISH_DONE

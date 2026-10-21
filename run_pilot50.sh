#!/bin/bash
# 50-drug pilot (2016-2020), sharded x5. Per-drug stages run in parallel shards;
# global stages (changes/convert/bundles) run once at the end.
cd /oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project/flask_hf
PY=/home/groups/akundaje/jsilberg/miniconda3/bin/python
export PYTHONUNBUFFERED=1
mapfile -t ALL < pilot50_apps.txt
N=${#ALL[@]}
SHARDS=5

for s in $(seq 0 $((SHARDS-1))); do
  SHARD=()
  for ((i=s; i<N; i+=SHARDS)); do SHARD+=("${ALL[$i]}"); done
  APPS="${SHARD[*]}"
  (
    echo "shard $s apps: $APPS"
    $PY -u run_card_single.py $APPS        && \
    $PY -u run_heal_loop.py $APPS          && \
    $PY -u supplements_extract.py $APPS    && \
    $PY -u backfill_pmr.py $APPS           && \
    $PY -u run_therapeutic_area.py $APPS   && \
    $PY -u run_adcomm.py adcomm_out 20 $APPS
    echo "SHARD $s DONE"
  ) > pilot_shard_$s.log 2>&1 &
done
wait
echo "== all shards done; global finish =="
APPS_ALL="${ALL[*]}"
$PY -u convert_single_to_app.py        # FIRST: build_label_changes/build_changes read cards_new/
$PY -u bundle_cited_docs.py
$PY -u bundle_adcomm_docs.py
$PY -u build_label_changes.py $APPS_ALL
$PY -u build_changes.py $APPS_ALL
$PY -u bundle_label_docs.py
echo ALL_DONE

#!/bin/bash
# Sharded full-pipeline batch. Usage: run_batch.sh <apps_file> <n_shards> <tag>
# Per-drug stages run in parallel shards; global stages once, in the CORRECT order
# (convert BEFORE build_label_changes/build_changes, which read cards_new/).
cd /oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project/flask_hf
PY=/home/groups/akundaje/jsilberg/miniconda3/bin/python
export PYTHONUNBUFFERED=1
APPS_FILE="${1:?apps file}"; SHARDS="${2:-8}"; TAG="${3:-batch}"
mapfile -t ALL < "$APPS_FILE"
N=${#ALL[@]}
echo "batch: $N apps across $SHARDS shards (tag=$TAG)"

for s in $(seq 0 $((SHARDS-1))); do
  SHARD=()
  for ((i=s; i<N; i+=SHARDS)); do SHARD+=("${ALL[$i]}"); done
  APPS="${SHARD[*]}"
  BUD=$(( ${#SHARD[@]} * 2 ))
  (
    echo "shard $s (${#SHARD[@]} apps): $APPS"
    $PY -u run_card_single.py $APPS      && \
    $PY -u run_heal_loop.py $APPS        && \
    $PY -u supplements_extract.py $APPS  && \
    $PY -u backfill_pmr.py $APPS         && \
    $PY -u run_therapeutic_area.py $APPS && \
    $PY -u run_adcomm.py adcomm_out $BUD $APPS
    echo "SHARD $s DONE"
  ) > ${TAG}_shard_$s.log 2>&1 &
done
wait
echo "== all shards done; global finish (convert FIRST) =="
APPS_ALL="${ALL[*]}"
$PY -u convert_single_to_app.py
$PY -u bundle_cited_docs.py
$PY -u bundle_adcomm_docs.py
$PY -u build_label_changes.py $APPS_ALL
$PY -u build_changes.py $APPS_ALL
$PY -u bundle_label_docs.py
# Name every cited source (doc_id -> "Statistical Review · 2014 — <file>.pdf"); without
# these, new cards' sources show short ids.
$PY -u build_doc_meta.py
$PY -u build_doc_labels.py
echo ALL_DONE

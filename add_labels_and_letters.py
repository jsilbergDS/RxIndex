#!/usr/bin/env python3
"""Add FDA label + approval-letter documents to the existing data/ directory.

Idempotent and additive — only fetches docs not already on disk.

The original exporter (gradio_app/export_paperclip_docs.py) intentionally
excluded /label/ and /appletter/ paths because they aren't review docs. But
Sonnet read those when generating the cards, so some claims (e.g. capsule
strengths from labels, PMC numbers from approval letters) end up untraceable
in the audit. This script fills that gap.

Output:
  data/docs/<doc_id>.txt        — text content for each new label/letter
  data/docs_index.json           — updated to include new entries
  data/pages_meta.json           — updated with total_pages/total_lines for new docs

Run on Sherlock (paperclip required):
  cd flask_hf && PYTHONUNBUFFERED=1 python3 add_labels_and_letters.py
"""
import os, sys, json, re, subprocess
from pathlib import Path
from tqdm import tqdm

HERE = Path(__file__).parent
DATA = HERE / "data"
DOCS_DIR = DATA / "docs"
DOCS_INDEX_PATH = DATA / "docs_index.json"
PAGES_META_PATH = DATA / "pages_meta.json"

DOCS_DIR.mkdir(parents=True, exist_ok=True)

# Load existing state
docs_index = json.load(open(DOCS_INDEX_PATH))
pages_meta = json.load(open(PAGES_META_PATH))
existing_doc_ids = {d["document_id"] for docs in docs_index.values() for d in docs}
print(f"Starting with {len(docs_index)} drugs, {len(existing_doc_ids)} existing docs", flush=True)


def infer_label_or_letter_type(url: str) -> str | None:
    """Classify based on URL path + filename suffix.
    Returns 'Lbl' or 'AppLetter' or None."""
    u = url.lower()
    if "/label/" in u or u.endswith("lbl.pdf"):
        return "Lbl"
    if "/appletter/" in u or u.endswith("ltr.pdf"):
        return "AppLetter"
    return None


def paperclip_sql_table(sql: str) -> list[dict]:
    """Run a paperclip SQL query and parse the pipe-separated table output.
    Returns list of dicts keyed by column name."""
    r = subprocess.run(
        ["paperclip", "sql", sql, "--source", "fda"],
        capture_output=True, text=True, timeout=60,
    )
    rows = []
    headers = None
    for line in r.stdout.split("\n"):
        if "|" not in line: continue
        parts = [p.strip() for p in line.split("|")]
        if headers is None:
            # Header is the line with column names
            if any(p in ("document_id", "doc_id", "public_url") for p in parts):
                headers = parts
            continue
        if all(set(p) <= set("-+ ") for p in parts):  # separator line
            continue
        if len(parts) != len(headers):
            continue
        rows.append(dict(zip(headers, parts)))
    return rows


def paperclip_cat(doc_id: str) -> str:
    """Fetch full text content via paperclip cat."""
    r = subprocess.run(
        ["paperclip", "cat", "--full", f"/fda/{doc_id}/content.lines"],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout


# Build appno filter (digits only, no leading zeros)
appnos = sorted({re.search(r"(\d+)", k).group(1).lstrip("0") for k in docs_index})
appno_filter = " OR ".join(f"public_url ILIKE '%{a}%'" for a in appnos)

# Discover all label/letter docs for our drugs.
# We do per-appno queries to keep results manageable and properly attribute each doc.
all_new_docs = []  # list of (app_key, doc_dict)
for app_key in tqdm(sorted(docs_index.keys()), desc="Discovering labels/letters"):
    appno_digits = re.search(r"(\d+)", app_key).group(1).lstrip("0")
    sql = (
        f"SELECT document_id, "
        f"SUBSTRING(public_url FROM 1 FOR 60) AS u1, "
        f"SUBSTRING(public_url FROM 61 FOR 60) AS u2, "
        f"SUBSTRING(public_url FROM 121 FOR 80) AS u3 "
        f"FROM documents "
        f"WHERE (public_url ILIKE '%{appno_digits}%' OR source_file ILIKE '%{appno_digits}%') "
        f"AND (public_url ILIKE '%/label/%' OR public_url ILIKE '%/appletter/%' "
        f"OR public_url ILIKE '%Lbl.pdf' OR public_url ILIKE '%lbl.pdf' "
        f"OR public_url ILIKE '%ltr.pdf' OR public_url ILIKE '%Ltr.pdf') "
        f"LIMIT 200"
    )
    rows = paperclip_sql_table(sql)
    for r in rows:
        did = r.get("document_id", "").strip()
        if not did or len(did) < 30: continue
        if did in existing_doc_ids: continue
        url = (r.get("u1", "") + r.get("u2", "") + r.get("u3", "")).strip()
        if not url.startswith("http"): continue
        doc_type = infer_label_or_letter_type(url)
        if not doc_type: continue
        all_new_docs.append((app_key, {
            "document_id": did,
            "type": doc_type,
            "public_url": url,
            "source_file": url,
        }))

print(f"\nFound {len(all_new_docs)} new label/letter docs to add", flush=True)

# Fetch text content for each new doc
fetched = 0
skipped = 0
for app_key, doc in tqdm(all_new_docs, desc="Fetching text content"):
    did = doc["document_id"]
    out_path = DOCS_DIR / f"{did}.txt"
    if out_path.exists() and out_path.stat().st_size > 100:
        skipped += 1
    else:
        text = paperclip_cat(did)
        if text and len(text) > 50:
            out_path.write_text(text)
            fetched += 1
        else:
            print(f"  [empty/short] {did}", flush=True)
            continue
    # Add to docs_index (avoid duplicates per app_key)
    if not any(d["document_id"] == did for d in docs_index[app_key]):
        docs_index[app_key].append(doc)

print(f"\nFetched {fetched} new text files, {skipped} already on disk", flush=True)

# Write updated docs_index
with open(DOCS_INDEX_PATH, "w") as f:
    json.dump(docs_index, f, indent=2)

# Bulk SQL for new pages_meta entries
new_ids = [doc["document_id"] for _, doc in all_new_docs]
if new_ids:
    quoted = ",".join(f"'{i}'" for i in new_ids)
    sql = f"SELECT document_id, total_pages, total_lines FROM documents WHERE document_id IN ({quoted})"
    rows = paperclip_sql_table(sql)
    added = 0
    for r in rows:
        did = r.get("document_id", "").strip()
        if not did: continue
        try:
            tp = int(r.get("total_pages", "").strip())
            tl = int(r.get("total_lines", "").strip())
        except (ValueError, AttributeError):
            continue
        # Find public_url from docs_index
        url = ""
        for _, doc in all_new_docs:
            if doc["document_id"] == did:
                url = doc["public_url"]; break
        pages_meta[did] = {"total_pages": tp, "total_lines": tl, "public_url": url}
        added += 1
    print(f"Added {added} new entries to pages_meta", flush=True)
    with open(PAGES_META_PATH, "w") as f:
        json.dump(pages_meta, f, indent=2)

# Final summary
total_docs_now = sum(len(v) for v in docs_index.values())
unique_docs_now = len({d["document_id"] for docs in docs_index.values() for d in docs})
print(f"\nFinal: {len(docs_index)} drugs, {unique_docs_now} unique docs", flush=True)
print(f"Files in data/docs/: {len(list(DOCS_DIR.iterdir()))}", flush=True)
print(f"Total bytes: {sum(p.stat().st_size for p in DOCS_DIR.iterdir()):,}", flush=True)
print(f"\nNext: re-run claim audit -> PYTHONUNBUFFERED=1 python3 build_claim_audit.py", flush=True)

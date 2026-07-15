#!/usr/bin/env python3
"""Build per-drug Markdown cards from the verified extractions.

For each drug:
  - Header: drug name (from endpoint_cards.jsonl), app number, approval year,
    verification stats.
  - Reviews read (doc_type + doc_id + link).
  - For each trial: bucket 1 (endpoint), bucket 2 (outcome), bucket 3 (role).
    Each quote is shown with the ACTUAL line (from verifier's found_lines when
    the agent's cited line was wrong). Every quote is a clickable citation to
    https://citations.gxl.ai/fda/<doc_id>#L<n>.
  - Bucket 4: FDA's approval rationale (drug-level).
  - Footer: quotes dropped in verification (with reason).

Only PASS, BLOCK_BOUNDARY (≤5 offset), and WRONG_LINE_LARGE (real quote, wrong
line — but we show the ACTUAL line so it's accurate) quotes are surfaced.
MISSING/FABRICATED quotes are listed in the footer as "dropped" so the reader
knows how much was thrown away per drug.
"""
import json, re, csv
from pathlib import Path
from collections import defaultdict

PROJECT = Path("/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project")
VER_DIR = PROJECT / "four_bucket_verified"
CARDS_DIR = PROJECT / "four_bucket_cards"
CARDS_DIR.mkdir(exist_ok=True)
CARDS_FILE = PROJECT / "endpoint_cards.jsonl"
DIAG_TSV = PROJECT / "four_bucket_verified" / "_diagnosis.tsv"

# Load per-drug metadata (drug name, year, jama flag, therapeutic area)
drug_meta = {}
with open(CARDS_FILE) as f:
    for line in f:
        c = json.loads(line)
        drug_meta[c["app"]] = c

# Load diagnosis TSV → per-quote failure mode lookup
diag = {}
if DIAG_TSV.exists():
    with open(DIAG_TSV) as f:
        for row in csv.DictReader(f, delimiter="\t"):
            key = (row["app"], row["bucket"], int(row["cited_line"]), row["quote_preview"][:60])
            diag[key] = row["final_mode"]


BUCKET_TITLES = {
    "bucket_1_prespecified_primary_endpoint": "Bucket 1 — Prespecified primary endpoint",
    "bucket_2_success_or_failure": "Bucket 2 — Primary endpoint outcome (met / missed)",
    "bucket_3_pivotal_role_language": "Bucket 3 — FDA characterization of trial's role",
    "bucket_4_fda_approval_rationale": "Bucket 4 — FDA rationale for approval",
}


def citation_url(doc_id: str, line: int) -> str:
    """Build a clickable citations.gxl.ai URL. Normalize doc_id to fda_XXX form."""
    if not doc_id:
        return ""
    # Strip fda_ prefix if present, then take first 12 hex chars without dashes
    body = doc_id[4:] if doc_id.startswith("fda_") else doc_id
    body = body.replace("-", "")
    short = body[:12]
    return f"https://citations.gxl.ai/fda/fda_{short}#L{line}"


def format_quote(q: dict, doc_type_default: str = "") -> str:
    """Render a quote with citation link. Uses verifier's found line when
    available; falls back to cited line when there's no verify block."""
    v = q.get("verify", {})
    doc_type = q.get("doc_type") or doc_type_default
    doc_id = v.get("doc_id_used") or q.get("doc_id", "")
    cited = q.get("line", 0)
    quote_text = q.get("quote", "").strip()

    status = v.get("status", "?")
    if status == "PASS":
        display_line = cited
        note = ""
    elif status == "OFFSET":
        # OFFSET: prefer the actual found line (accurate); note offset if large
        found = v.get("found_lines", [])
        off = v.get("offset", 0)
        display_line = found[0] if found else cited
        if abs(off) <= 5:
            note = ""  # BLOCK_BOUNDARY — skip note
        else:
            note = f" *(agent cited L{cited}, actual L{display_line})*"
    else:
        display_line = cited
        note = f" *(verify: {status})*"

    url = citation_url(doc_id, display_line)
    header = f"[{doc_type} L{display_line}]({url}){note}"
    # Indent the quote as a blockquote
    q_block = "\n".join(f"> {ln}" for ln in quote_text.split("\n"))
    return f"- {header}\n{q_block}\n"


def is_surfaceable(q: dict) -> bool:
    """Include on the card if the quote was verified as real (PASS or block boundary
    or wrong-line but real text). Exclude fabrications and unverifiable."""
    v = q.get("verify", {})
    s = v.get("status", "?")
    if s == "PASS":
        return True
    if s == "OFFSET":
        # OFFSET means the text is real, just wrong line — include (we show real line)
        return True
    if s == "MISSING":
        # Consult diagnosis
        key_prefix = (q.get("quote", "")[:60])
        # We surface if diagnosis says WRONG_DOC, WHITESPACE_NORM, UNICODE_NORM, AGENT_TRUNC — all real
        # We exclude FABRICATED
        # Without diagnosis, we drop
        return False  # conservative — dropped in card, listed in footer
    return False


def render_card(app: str, extraction: dict) -> str:
    meta = drug_meta.get(app, {})
    drug = meta.get("drug", "?")
    year = meta.get("year", "?")
    in_jama = meta.get("in_jama_paper", False)
    orphan = meta.get("orphan", False)
    priority = meta.get("priority_review", False)
    ta = meta.get("therapeutic_area", "?")

    docs_read = extraction.get("docs_read", [])
    trials = extraction.get("trials", [])
    b4 = extraction.get("bucket_4_fda_approval_rationale", [])

    # Count verify stats
    total_quotes = 0
    pass_ct = offset_ct = miss_ct = 0
    dropped = []
    for t in trials:
        for b in ("bucket_1_prespecified_primary_endpoint",
                  "bucket_2_success_or_failure",
                  "bucket_3_pivotal_role_language"):
            for q in t.get(b, []):
                total_quotes += 1
                v = q.get("verify", {})
                s = v.get("status", "?")
                if s == "PASS": pass_ct += 1
                elif s == "OFFSET": offset_ct += 1
                elif s == "MISSING":
                    miss_ct += 1
                    dropped.append((t.get("trial_name","?"), b, q))
    for q in b4:
        total_quotes += 1
        v = q.get("verify", {})
        s = v.get("status", "?")
        if s == "PASS": pass_ct += 1
        elif s == "OFFSET": offset_ct += 1
        elif s == "MISSING":
            miss_ct += 1
            dropped.append(("(rationale)", "bucket_4_fda_approval_rationale", q))

    lines = []
    lines.append(f"# {drug} ({app})")
    lines.append("")
    tags = []
    tags.append(f"approved {year}")
    if in_jama: tags.append("**in JAMA 2023 (Johnston et al.) 21-drug list**")
    if orphan: tags.append("orphan")
    if priority: tags.append("priority review")
    if ta and ta != "?": tags.append(ta)
    lines.append(" · ".join(tags))
    lines.append("")
    lines.append(f"**Verification**: {pass_ct}/{total_quotes} PASS, {offset_ct} OFFSET (real quotes with cited-line adjusted), {miss_ct} DROPPED (unverifiable)")
    lines.append("")
    lines.append("**Reviews read:**")
    for d in docs_read:
        dt = d.get("doc_type","?")
        did = d.get("doc_id","?")
        # normalize for URL
        body = did[4:] if did.startswith("fda_") else did
        body = body.replace("-","")
        short = body[:12] if body else ""
        ut = d.get("url_tail","")
        if short:
            lines.append(f"- {dt}: [{ut}](https://citations.gxl.ai/fda/fda_{short})")
        else:
            lines.append(f"- {dt}: {ut}")
    lines.append("")

    # Trials
    lines.append("---")
    lines.append("")
    lines.append("## Trials")
    for t in trials:
        tname = t.get("trial_name","(unnamed trial)")
        lines.append("")
        lines.append(f"### {tname}")
        for b in ("bucket_1_prespecified_primary_endpoint",
                  "bucket_2_success_or_failure",
                  "bucket_3_pivotal_role_language"):
            qs = [q for q in t.get(b, []) if is_surfaceable(q)]
            if not qs: continue
            lines.append("")
            lines.append(f"**{BUCKET_TITLES[b].split(' — ',1)[1]}**")
            for q in qs:
                lines.append(format_quote(q))
        lines.append("")

    # Bucket 4
    b4_ok = [q for q in b4 if is_surfaceable(q)]
    if b4_ok:
        lines.append("---")
        lines.append("")
        lines.append(f"## {BUCKET_TITLES['bucket_4_fda_approval_rationale']}")
        lines.append("")
        for q in b4_ok:
            lines.append(format_quote(q))
        lines.append("")

    # Footer with dropped quotes
    if dropped:
        lines.append("---")
        lines.append("")
        lines.append(f"## Dropped in verification ({len(dropped)} quotes)")
        lines.append("")
        lines.append("These quotes could not be found in any paperclip FDA doc for this drug (fabricated, or paraphrased beyond recognition). Not surfaced in the card above.")
        lines.append("")
        for tname, bucket, q in dropped:
            bk_short = bucket.replace("bucket_","").replace("_"," ")
            lines.append(f"- **{tname}** {bk_short} L{q.get('line')}: {q.get('quote','')[:180]}…")
        lines.append("")

    return "\n".join(lines)


def main():
    files = sorted(VER_DIR.glob("*.json"))
    n_written = 0
    stats = []
    for fp in files:
        if fp.name.startswith("_"): continue
        app = fp.stem
        d = json.load(open(fp))
        md = render_card(app, d)
        out = CARDS_DIR / f"{app}.md"
        with open(out, "w") as f:
            f.write(md)
        n_written += 1
        # collect stats for the index
        drug = drug_meta.get(app, {}).get("drug", "?")
        year = drug_meta.get(app, {}).get("year", "?")
        stats.append((year, drug, app))

    # Write an index.md
    stats.sort()
    idx = ["# FDA drug-approval cards — 58 drugs (2015-2024)\n",
           "Each card contains verbatim FDA quotes from paperclip in four buckets:",
           "1. Prespecified primary endpoint",
           "2. Endpoint outcome (met / missed)",
           "3. FDA characterization of the trial's role (pivotal / supportive / etc.)",
           "4. FDA rationale for approval\n",
           "Every quote is clickable to its exact paperclip source line.\n",
           "## Drugs\n",
           "| Year | Drug | App | Card |",
           "|---|---|---|---|"]
    for year, drug, app in stats:
        idx.append(f"| {year} | {drug} | {app} | [{app}.md]({app}.md) |")
    (CARDS_DIR / "index.md").write_text("\n".join(idx))

    print(f"\nWrote {n_written} cards + index.md to {CARDS_DIR}", flush=True)


if __name__ == "__main__":
    main()

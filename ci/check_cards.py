#!/usr/bin/env python3
"""PR check for edits to data/cards_single/*.json. No API calls.

For each changed card file:
  1. valid JSON, top-level dict with a dict "card"
  2. every citation that is NEW or CHANGED vs the base branch must have a doc_id that
     is bundled in data/docs/<doc_id>.txt, and its FULL quote must appear in that doc
     (whitespace-collapsed, case-insensitive -- same normalization as
     run_card_single.verify_citations, but the whole quote, not a 60-char prefix).
Citations already on the base branch are not re-checked, so a PR is never blamed for
pre-existing problems.

Usage: python ci/check_cards.py <base_ref> [changed card paths...]
       (with no paths, diffs <base_ref>...HEAD for data/cards_single/*.json)
Exit 1 if any check fails.
"""
import json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "data" / "docs"
_NORMDOC = {}

def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()

def doc_text(doc_id):
    """Whole-doc normalized text from the bundled L<n>: line file."""
    if doc_id not in _NORMDOC:
        p = DOCS / f"{doc_id}.txt"
        if not p.exists():
            _NORMDOC[doc_id] = None
        else:
            lines = [re.sub(r"^L\d+:\s?", "", ln) for ln in p.read_text(errors="replace").split("\n")]
            _NORMDOC[doc_id] = norm(" ".join(lines))
    return _NORMDOC[doc_id]

def citations(obj, path=""):
    """Yield (field_path, citation dict) for every citation in a card."""
    if isinstance(obj, dict):
        if isinstance(obj.get("citations"), list):
            for c in obj["citations"]:
                if isinstance(c, dict):
                    yield path, c
        for k, v in obj.items():
            yield from citations(v, f"{path}/{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from citations(v, f"{path}[{i}]")

def base_version(base_ref, rel):
    r = subprocess.run(["git", "show", f"{base_ref}:{rel}"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None   # new file
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def changed_cards(base_ref):
    r = subprocess.run(["git", "diff", "--name-only", "--diff-filter=AM", f"{base_ref}...HEAD",
                        "--", "data/cards_single/*.json"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [l for l in r.stdout.split("\n") if l.strip()]

def check_file(base_ref, rel):
    errs = []
    try:
        d = json.loads((ROOT / rel).read_text())
    except Exception as e:
        return [f"{rel}: not valid JSON ({e})"]
    if not isinstance(d, dict) or not isinstance(d.get("card"), dict):
        return [f"{rel}: expected an object with a \"card\" object"]
    base = base_version(base_ref, rel)
    seen = set()
    if base and isinstance(base.get("card"), dict):
        seen = {(norm(c.get("quote")), c.get("doc_id")) for _, c in citations(base["card"])}
    n_new = 0
    for field, c in citations(d["card"]):
        q, did = norm(c.get("quote")), c.get("doc_id")
        if not q or (q, did) in seen:
            continue
        n_new += 1
        if not did:
            errs.append(f"{rel} [{field}]: citation has no doc_id")
            continue
        txt = doc_text(did)
        if txt is None:
            errs.append(f"{rel} [{field}]: doc_id {did} is not bundled in data/docs/")
        elif q not in txt:
            errs.append(f"{rel} [{field}]: quote not found verbatim in doc {did}: \"{(c.get('quote') or '')[:100]}\"")
    print(f"{rel}: {n_new} new/changed citation(s) checked, {len(errs)} problem(s)", flush=True)
    return errs

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    base_ref = sys.argv[1]
    files = sys.argv[2:] or changed_cards(base_ref)
    if not files:
        print("No changed card files.", flush=True); return
    errs = []
    for rel in files:
        errs += check_file(base_ref, rel)
    if errs:
        print("\nFAILED:", flush=True)
        for e in errs: print("  " + e, flush=True)
        sys.exit(1)
    print("\nAll card checks passed.", flush=True)

if __name__ == "__main__":
    main()

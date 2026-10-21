#!/usr/bin/env python3
"""Protect human (pull-request) edits from pipeline re-runs.

A card counts as HUMAN-EDITED if any git commit touching data/cards_single/<APP>.json
came in through a GitHub pull request: a merge commit ("Merge pull request #N ...") or
a squash/rebase merge whose subject ends in "(#N)". Pipeline output must therefore be
committed/pushed DIRECTLY (never via a PR), or every card it touches would be locked.

Pipeline stages that rewrite cards call filter_apps(apps, stage) and skip locked cards.
Override (deliberately rebuild a human-edited card): RXINDEX_INCLUDE_HUMAN_EDITED=1

CLI:  python human_edits.py            # list human-edited cards and the PRs that touched them
"""
import os, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CARDS_REL = "data/cards_single"
_PR_RE = re.compile(r"^Merge pull request #(\d+)|\(#(\d+)\)\s*$")
_CACHE = None

def human_edited():
    """{app_key: [pr numbers]} from git history. Empty dict (with a warning) if git fails."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    out = {}
    try:
        # --first-parent -m: walk main's own history and list a merge commit's files
        # relative to main (plain --name-only shows nothing for merge commits).
        r = subprocess.run(["git", "log", "--first-parent", "-m", "--format=@@%s", "--name-only", "--", CARDS_REL],
                           cwd=HERE, capture_output=True, text=True, check=True, timeout=300)
    except Exception as e:
        print(f"[human_edits] WARNING: could not read git history ({e}); no cards locked", file=sys.stderr, flush=True)
        _CACHE = out
        return out
    pr = None
    for line in r.stdout.split("\n"):
        if line.startswith("@@"):
            m = _PR_RE.search(line[2:].strip())
            pr = (m.group(1) or m.group(2)) if m else None
        elif pr and line.startswith(CARDS_REL + "/") and line.endswith(".json"):
            out.setdefault(Path(line).stem, []).append(pr)
    _CACHE = out
    return out

def filter_apps(apps, stage):
    """Drop human-edited cards from a stage's app list (unless overridden)."""
    if os.environ.get("RXINDEX_INCLUDE_HUMAN_EDITED") == "1":
        return list(apps)
    locked = human_edited()
    keep = []
    for a in apps:
        if a in locked:
            print(f"  [{stage}] SKIP {a}: human-edited via PR #{', #'.join(sorted(set(locked[a])))} "
                  f"(set RXINDEX_INCLUDE_HUMAN_EDITED=1 to override)", flush=True)
        else:
            keep.append(a)
    return keep

if __name__ == "__main__":
    he = human_edited()
    print(f"{len(he)} human-edited card(s)")
    for a, prs in sorted(he.items()):
        print(f"  {a}: PR #{', #'.join(sorted(set(prs)))}")

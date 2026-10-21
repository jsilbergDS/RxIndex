#!/usr/bin/env python3
"""Persist EVERY residual issue after the self-healing loop so it can't be lost when
cards_critic/<app>.json is overwritten by a later critic run.

Writes, under data/residuals/ (stamped, never overwritten):
  - residuals_full_<STAMP>.json  : per card -> release_ready + ALL issues (every
                                    severity) + ALL automated_flags (unverified_citations,
                                    uncited_numbers, empty_fields). Lossless.
  - residuals_full_<STAMP>.md     : human-readable listing of every issue/flag.
  - cards_critic_snapshot_<STAMP>/: verbatim copy of every cards_critic/*.json (source of truth).

Usage: python3 build_residuals_log.py <STAMP>   (STAMP e.g. 20260721_0930)
"""
import json, glob, shutil, sys
from pathlib import Path

HERE = Path(__file__).parent
CRIT = HERE / "data" / "cards_critic"
SINGLE = HERE / "data" / "cards_single"
OUT = HERE / "data" / "residuals"; OUT.mkdir(parents=True, exist_ok=True)
STAMP = sys.argv[1] if len(sys.argv) > 1 else "snapshot"

drug = {}
for fp in glob.glob(str(SINGLE / "*.json")):
    try:
        drug[Path(fp).stem] = json.load(open(fp)).get("_meta", {}).get("drug", "")
    except Exception:
        pass

full = {}
for fp in sorted(glob.glob(str(CRIT / "*.json"))):
    app = Path(fp).stem
    d = json.load(open(fp))
    af = d.get("automated_flags", {}) or {}
    full[app] = {
        "drug": drug.get(app, ""),
        "release_ready": d.get("release_ready"),
        "issues": d.get("issues", []),                      # ALL severities, verbatim
        "automated_flags": {
            "unverified_citations": af.get("unverified_citations", []),
            "uncited_numbers": af.get("uncited_numbers", []),
            "empty_fields": af.get("empty_fields", []),
        },
        "_parse_error": d.get("_parse_error"),
    }

(OUT / f"residuals_full_{STAMP}.json").write_text(json.dumps(full, indent=2))

# verbatim snapshot of the raw critic outputs so the source can never be overwritten away
snap = OUT / f"cards_critic_snapshot_{STAMP}"
if snap.exists():
    shutil.rmtree(snap)
shutil.copytree(CRIT, snap)

def nitems(r):
    af = r["automated_flags"]
    return (len(r["issues"]) + len(af["unverified_citations"])
            + len(af["uncited_numbers"]) + len(af["empty_fields"]))

lines = [f"# Residual issues — full snapshot ({STAMP})", "",
         f"Every critic issue + automated flag across {len(full)} cards. "
         f"Verbatim source: `cards_critic_snapshot_{STAMP}/`. Machine-readable: "
         f"`residuals_full_{STAMP}.json`.", ""]
total = 0
cards_with = 0
for app in sorted(full, key=lambda a: -nitems(full[a])):
    r = full[app]; af = r["automated_flags"]; n = nitems(r)
    if n == 0:
        continue
    cards_with += 1
    lines.append(f"## {r['drug']} ({app}) — release_ready={r['release_ready']} — {n} item(s)")
    for i in r["issues"]:
        lines.append(f"- [{i.get('severity', '?')}] {i.get('field', '')}: {i.get('issue', '')}"); total += 1
    for u in af["unverified_citations"]:
        lines.append(f"- [flag:unverified_citation] {u.get('field', '')} L{u.get('line')}: "
                     f"\"{(u.get('quote') or '')[:90]}\""); total += 1
    for u in af["uncited_numbers"]:
        lines.append(f"- [flag:uncited_number] {u.get('field', '')}: {u.get('number')} "
                     f"in \"{(u.get('text') or '')[:60]}\""); total += 1
    for e in af["empty_fields"]:
        lines.append(f"- [flag:empty_field] {e}"); total += 1
    lines.append("")
lines.insert(3, f"**{total} residual items across {cards_with} cards** "
                f"({len(full) - cards_with} cards had none).\n")
(OUT / f"residuals_full_{STAMP}.md").write_text("\n".join(lines))

print(f"saved {total} residual items across {cards_with} cards", flush=True)
print(f"  data/residuals/residuals_full_{STAMP}.json", flush=True)
print(f"  data/residuals/residuals_full_{STAMP}.md", flush=True)
print(f"  data/residuals/cards_critic_snapshot_{STAMP}/ ({len(list(snap.glob('*.json')))} files)", flush=True)

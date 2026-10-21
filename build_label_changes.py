#!/usr/bin/env python3
"""Track MAJOR LABEL CHANGES SINCE INITIAL APPROVAL, per drug.

Source = the label's own RECENT MAJOR CHANGES box, read from every label version in
paperclip and unioned into a dated timeline. Deterministic; no LLM.

Output per drug (data/label_changes/<app>.json):
  { app_key, drug, n_label_versions,
    boxed_warning_at_approval, boxed_warning_added,        # derived flags
    events: [ {date, category(SAFETY|EFFICACY), section} ... ] }

Usage: python3 build_label_changes.py [APP ...]   (default: a trial set)
"""
import json, sys, re
from pathlib import Path
import pas_rmc_pilot as R    # R.build(app_key) -> RMC timeline (label versions -> events)

HERE = Path(__file__).parent
CARDS = HERE / "data" / "cards_new"
OUT = HERE / "data" / "label_changes"; OUT.mkdir(parents=True, exist_ok=True)

TRIAL = ["BLA761269", "NDA214012", "NDA214665", "NDA215358", "NDA213378",
         "NDA215866", "BLA761174", "BLA761210", "NDA214377", "NDA214662"]


def _dkey(d):
    mo, yr = d.split("/"); return (int(yr), int(mo))


def approval_boxed(app_key):
    """Was there a boxed warning at approval? (from the card, if we have it)."""
    p = CARDS / f"{app_key}.json"
    if not p.exists():
        return None
    return bool((json.load(open(p)).get("boxed_warning") or {}).get("present"))


def build(app_key):
    r = R.build(app_key)
    if r.get("error"):
        return {"app_key": app_key, "drug": r.get("drug"), "error": r["error"]}
    events = sorted(r["events"], key=lambda e: _dkey(e["date"]))
    boxed_at = approval_boxed(app_key)
    boxed_evts = [e for e in events if e["section"].lower().startswith("boxed")]
    # a boxed-warning RMC entry while approval had none -> ADDED (earliest such date)
    boxed_added = (boxed_evts[0]["date"] if boxed_evts and boxed_at is False else None)
    return {
        "app_key": app_key, "drug": r.get("drug"),
        "n_label_versions": r.get("n_label_versions"),
        "boxed_warning_at_approval": boxed_at,
        "boxed_warning_added": boxed_added,
        "n_changes": len(events),
        "n_safety_changes": sum(1 for e in events if e["category"] == "SAFETY"),
        "latest_change": events[-1]["date"] if events else None,
        "events": [{"date": e["date"], "category": e["category"], "section": e["section"],
                    "doc_id": e.get("doc_id"), "line": e.get("line")} for e in events],
    }


def main():
    apps = sys.argv[1:] or TRIAL
    for app_key in apps:
        r = build(app_key)
        (OUT / f"{app_key}.json").write_text(json.dumps(r, indent=2))
        if r.get("error"):
            print(f"{r['drug']} ({app_key}): {r['error']}", flush=True); continue
        ba = r["boxed_warning_added"]
        flag = f"  ⚑ BOXED WARNING ADDED {ba}" if ba else ""
        print(f"\n{r['drug']} ({app_key})  {r['n_changes']} changes, {r['n_safety_changes']} safety"
              f"  [boxed at approval: {r['boxed_warning_at_approval']}]{flag}", flush=True)
        for e in r["events"][:6]:
            print(f"    {e['date']:>8} [{e['category']:8}] {e['section'][:56]}", flush=True)
        if len(r["events"]) > 6:
            print(f"    … +{len(r['events'])-6} more", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Merge the two post-approval sources into one date-grouped 'Changes since approval'
timeline per drug, and render an HTML preview.

  - efficacy supplements (card['supplements'])  -> rich cited 'what changed' for indication/dosing
  - label RECENT MAJOR CHANGES (data/label_changes/) -> the SAFETY changes (+ any efficacy the label flags)

Usage: python3 build_changes_section.py <APP_KEY>   (writes <app>_changes.html)
"""
import json, sys, re, html
from pathlib import Path

HERE = Path(__file__).parent


def mkey(d):
    if "-" in d:
        y, m, _ = d.split("-"); return (int(y), int(m))
    m, y = d.split("/"); return (int(y), int(m))


def changes_timeline(app_key):
    card = json.load(open(HERE / "data" / "cards_new" / f"{app_key}.json"))
    lc = json.load(open(HERE / "data" / "label_changes" / f"{app_key}.json"))
    approval_boxed = bool((card.get("boxed_warning") or {}).get("present"))

    months = {}
    for s in (card.get("supplements") or []):
        d = s.get("approval_date")
        if not d:
            continue
        desc = s.get("description")
        txt = desc.get("text") if isinstance(desc, dict) else None
        if txt:
            months.setdefault(mkey(d), {"eff_desc": [], "safety": [], "eff_rmc": []})["eff_desc"].append(txt)
    for e in lc.get("events", []):
        k = mkey(e["date"]); months.setdefault(k, {"eff_desc": [], "safety": [], "eff_rmc": []})
        (months[k]["safety"] if e["category"] == "SAFETY" else months[k]["eff_rmc"]).append(e["section"])

    seen_boxed = approval_boxed
    out = []
    for k in sorted(months):
        m = months[k]; y, mo = k
        eff, saf = [], []
        # efficacy: prefer the supplement's cited description
        for t in m["eff_desc"]:
            eff.append(t)
        if not m["eff_desc"]:
            for s in m["eff_rmc"]:
                if s.lower().startswith("indication"): eff.append("Indication / usage updated")
                elif s.lower().startswith("dosage"): eff.append("Dosing updated")
        # safety
        boxed = [s for s in m["safety"] if s.lower().startswith("boxed")]
        if boxed:
            if not seen_boxed:
                saf.append(("added", "Boxed warning added")); seen_boxed = True
            else:
                saf.append(("revised", "Boxed warning revised"))
        if any(s.lower().startswith("contraindication") for s in m["safety"]):
            saf.append(("", "Contraindication updated"))
        warns = [s for s in m["safety"] if "warnings and precautions" in s.lower() or re.search(r"\(5\.", s)]
        if warns:
            saf.append(("", "Warnings & precautions updated"))
        out.append({"date": f"{mo}/{y}", "efficacy": eff, "safety": saf})
    return card["_meta"].get("drug", app_key), approval_boxed, out


def render(app_key):
    drug, approval_boxed, tl = changes_timeline(app_key)
    H = lambda s: html.escape(str(s))
    rows = []
    for e in tl:
        chips = "".join(f'<span class="chip {"add" if k=="added" else "saf"}">{H(t)}</span>' for k, t in e["safety"])
        effs = "".join(f'<div class="eff">{H(t)}</div>' for t in e["efficacy"])
        rows.append(f'<div class="row"><div class="date">{H(e["date"])}</div>'
                    f'<div class="body">{effs}{("<div>"+chips+"</div>") if chips else ""}</div></div>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{H(drug)} — changes since approval</title>
<style>
body{{font-family:-apple-system,Segoe UI,Inter,Arial,sans-serif;background:#eef1f6;margin:0;color:#111827}}
.card{{max-width:760px;margin:30px auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px 28px;box-shadow:0 6px 22px rgba(17,24,39,.08)}}
h2{{font-size:14px;letter-spacing:.04em;text-transform:uppercase;color:#374151;margin:0 0 4px}}
.sub{{font-size:12.5px;color:#6b7280;margin:0 0 16px}}
.row{{display:flex;gap:14px;padding:11px 0;border-top:1px solid #f0f2f5}}
.date{{flex:0 0 62px;font-weight:640;font-size:13px;color:#1f3a8a}}
.body{{flex:1;font-size:13.5px;line-height:1.5}}
.eff{{color:#111827;margin-bottom:4px}}
.chip{{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:999px;margin:2px 6px 2px 0}}
.chip.add{{background:#fee2e2;color:#991b1b;border:1px solid #fecaca}}
.chip.saf{{background:#fef3c7;color:#92400e;border:1px solid #fde68a}}
</style></head><body>
<div class="card">
  <h2>Changes since approval — {H(drug)}</h2>
  <p class="sub">Indication &amp; dosing from efficacy supplements; safety from the label's Recent Major Changes. Boxed warning at approval: <b>{approval_boxed}</b>.</p>
  {''.join(rows)}
</div></body></html>"""


if __name__ == "__main__":
    app = sys.argv[1] if len(sys.argv) > 1 else "BLA761269"
    outp = HERE / f"{app}_changes.html"
    outp.write_text(render(app))
    print(f"wrote {outp}", flush=True)

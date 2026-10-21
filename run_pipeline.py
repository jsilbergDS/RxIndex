#!/usr/bin/env python3
"""End-to-end driver for writing + healing + rendering the RxIndex cards.

Chains the existing stage scripts in order for a given set of app_keys. Rewrites
NOTHING — it just invokes each stage as a subprocess, logs it, and totals the cost.

  build  -> heal -> supplements -> backfill_pmr -> therapeutic_area -> convert
           (+ optional: adcomm, claim_audit)

Resumable: `build` skips any app_key that already has data/cards_single/<APP>.json
(use --force to rebuild). Downstream stages re-run on all given app_keys (idempotent,
but heal/supplements/adcom cost tokens — pass only the new/failed keys to avoid rework).

Usage:
  PYTHONUNBUFFERED=1 python3 run_pipeline.py BLA761269 NDA217424 ...
  python3 run_pipeline.py --file apps_2016_2020.txt          # one app_key per line
  python3 run_pipeline.py --file apps.txt --adcom --audit    # include the optional stages
  python3 run_pipeline.py --file apps.txt --skip supplements,backfill_pmr
Run with the env that has `anthropic` + `paperclip` on PATH (the build/heal read paperclip).
"""
import sys, subprocess, re, time, argparse
from pathlib import Path

HERE = Path(__file__).parent
SINGLE = HERE / "data" / "cards_single"
PY = sys.executable
COST_RE = re.compile(r'(?:total[^$\n]*|cost[^$\n]*)\$([0-9]+\.[0-9]+)', re.I)


def run_stage(name, argv, logdir):
    log = logdir / f"{name}.log"
    shown = " ".join(str(a) for a in argv[:5]) + (" …" if len(argv) > 5 else "")
    print(f"\n=== [{name}] {shown} ===", flush=True)
    t0 = time.time()
    buf = []
    with open(log, "w") as f:
        p = subprocess.Popen([PY, "-u"] + [str(a) for a in argv], cwd=str(HERE),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            f.write(line); f.flush(); buf.append(line)
        p.wait()
    m = COST_RE.findall("".join(buf))
    cost = float(m[-1]) if m else 0.0
    ok = p.returncode == 0
    print(f"    {'ok' if ok else 'FAILED rc=%d' % p.returncode}  {time.time()-t0:.0f}s  "
          f"stage-cost ~${cost:.2f}  log: pipeline_logs/{log.name}", flush=True)
    return ok, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apps", nargs="*")
    ap.add_argument("--file", help="file with one app_key per line (# comments ok)")
    ap.add_argument("--force", action="store_true", help="rebuild even if cards_single/<APP>.json exists")
    ap.add_argument("--skip", default="", help="comma list of stages to skip")
    ap.add_argument("--adcom", action="store_true", help="also run adcomm re-extraction (needs bundle_adcomm_docs follow-up)")
    ap.add_argument("--audit", action="store_true", help="also run claim audit (NB: currently audits data/cards.jsonl)")
    ap.add_argument("--adcom-budget", type=float, default=0.0, help="total $ budget for adcomm (default 2*n)")
    a = ap.parse_args()

    apps = list(a.apps)
    if a.file:
        apps += [l.strip() for l in open(a.file) if l.strip() and not l.startswith("#")]
    apps = sorted(set(apps))
    if not apps:
        sys.exit("no app_keys given (pass them as args or via --file)")
    skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    logdir = HERE / "pipeline_logs"; logdir.mkdir(exist_ok=True)

    to_build = apps if a.force else [k for k in apps if not (SINGLE / f"{k}.json").exists()]
    print(f"pipeline: {len(apps)} app_keys | build {len(to_build)} "
          f"(skipping {len(apps)-len(to_build)} already built){' [--force]' if a.force else ''}", flush=True)

    stages = []
    if to_build and "build" not in skip:
        stages.append(("build", ["run_card_single.py"] + to_build))
    if "heal" not in skip:
        stages.append(("heal", ["run_heal_loop.py"] + apps))
    if "supplements" not in skip:
        stages.append(("supplements", ["supplements_extract.py"] + apps))
    if "backfill_pmr" not in skip:
        stages.append(("backfill_pmr", ["backfill_pmr.py"] + apps))
    if "therapeutic_area" not in skip:
        stages.append(("therapeutic_area", ["run_therapeutic_area.py"] + apps))
    if a.adcom and "adcom" not in skip:
        budget = a.adcom_budget or len(apps) * 2.0
        stages.append(("adcomm", ["run_adcomm.py", str(HERE / "adcomm_out"), str(budget)] + apps))
    if "convert" not in skip:
        stages.append(("convert", ["convert_single_to_app.py"]))       # global: reshapes ALL cards_single
    if "bundle_docs" not in skip:
        stages.append(("bundle_docs", ["bundle_cited_docs.py"]))        # fetch any cited-but-unbundled docs -> data/docs/
    if a.audit and "audit" not in skip:
        stages.append(("claim_audit", ["build_claim_audit.py"]))

    total = 0.0; results = []
    for name, argv in stages:
        ok, cost = run_stage(name, argv, logdir)
        total += cost; results.append((name, ok, cost))
        if not ok and name == "build":
            print("  build failed — stopping (downstream stages need cards_single/)", flush=True)
            break

    built = [k for k in apps if (SINGLE / f"{k}.json").exists()]
    missing = [k for k in apps if k not in built]
    print(f"\n=== PIPELINE DONE  ~${total:.2f} total ===", flush=True)
    for n, ok, c in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {n:18} ~${c:.2f}", flush=True)
    print(f"  cards_single present: {len(built)}/{len(apps)}", flush=True)
    if missing:
        print(f"  NO card for: {missing[:12]}{' …' if len(missing) > 12 else ''}", flush=True)
    print(f"  per-stage logs: {logdir}/", flush=True)


if __name__ == "__main__":
    main()

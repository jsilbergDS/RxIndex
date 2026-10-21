# RxIndex card pipeline — write, heal, render

End-to-end pipeline that turns a list of FDA application keys (e.g. `BLA761269`,
`NDA217424`) into rendered, source-grounded drug cards. Driver: **`run_pipeline.py`**
(chains the existing stage scripts; rewrites none of them).

## Stages (in order)

| # | Stage | Script | CLI | Reads / Writes |
|---|---|---|---|---|
| 1 | **Write** | `run_card_single.py` | `APP …` | agentic Opus build over paperclip docs; runs `verify_citations` (60-char quote-in-doc gate) → `data/cards_single/<APP>.json` |
| 2 | **Heal** | `run_heal_loop.py` | `APP …` (or all) | critic → repair → re-critic, ≤3 rounds, no-progress + reject-corrupt guards; deterministic `unverified_citations` flags the LLM can't suppress → overwrites `cards_single/` |
| 3 | Supplements | `supplements_extract.py` | `APP …` | efficacy (class-2) supplements from Drugs@FDA, cited → `cards_single/` |
| 4 | PMR/PMC | `backfill_pmr.py` | `APP …` | post-marketing requirements → `cards_single/` |
| 5 | Therapeutic area | `run_therapeutic_area.py` | `APP …` | TA normalization → `cards_single/` |
| 6 | **Render** | `convert_single_to_app.py` | *(global)* | reshapes ALL healed `cards_single/` → `data/cards_new/*.json` + `data/cards_recited_new.jsonl` (what the app renders) |
| 7 | **Bundle docs** | `bundle_cited_docs.py` | *(global)* | fetches any cited-but-unbundled doc from paperclip → `data/docs/<id>.txt`, so every citation can open + verify (prevents the `no_local_file` gap) |
| — | *(opt)* AdComm | `run_adcomm.py` | `<out_dir> <budget> APP …` | advisory-committee re-extraction → `adcomm_out/`; needs `bundle_adcomm_docs.py` follow-up |
| — | *(opt)* Audit | `build_claim_audit.py` | *(global)* | grep every claim vs docs → tiers → `claim_audit.json` |

The **write + heal (1–2)** is the "self-healing" core. `convert` (6) does no extraction —
it is a pure reshape of the healed cards, so **the rendered sources are the healed ones.**

## Verification (what "verified" means)
- **Deterministic** (`verify_citations`, `automated_flags.unverified_citations`): the quote's
  leading chars must appear in the cited doc's OCR. This checks **existence**, not support.
- **Semantic** (`PROMPT_CRITIC`, LLM): judges whether a quote *supports* its claim — fallible.
- ⇒ A wrong *inference* on a real quote can still pass. See `AUDIT_METHOD.md` for the audit tiers.

## Usage
```bash
PYTHONUNBUFFERED=1 python3 run_pipeline.py BLA761269 NDA217424        # a few keys
python3 run_pipeline.py --file apps_2016_2020.txt                    # one key/line
python3 run_pipeline.py --file apps.txt --adcom --audit              # include optional stages
python3 run_pipeline.py --file apps.txt --skip supplements,backfill_pmr
```
Run with the env that has `anthropic` + `paperclip` on PATH. Per-stage logs land in
`pipeline_logs/`; a per-stage + total cost estimate prints at the end.

## Behavior & caveats
- **Resumable:** `build` skips any app_key that already has `cards_single/<APP>.json`
  (`--force` to rebuild). Downstream stages **re-run on all given keys** (idempotent, but
  heal/supplements/adcom spend tokens) — pass only new/failed keys to avoid rework.
- **Build failure stops the run** (downstream needs `cards_single/`). Individual per-app
  failures are reported at the end ("NO card for: …").
- **AdComm** uses a different interface and needs a bundling follow-up (`bundle_adcomm_docs.py`)
  before the app shows it — hence optional (`--adcom`).
- **Audit** currently reads `data/cards.jsonl` (the old file), not the new render — repoint
  it before trusting its numbers on newly built cards. Optional (`--audit`).
- **Cost:** measured ≈ **$8–9/card** (build ~$3.4 + heal ~$3.7 dominate; supplements/TA cheap).
  See the cost breakdown discussion / run logs.
- **Not parallel:** stages run sequentially. To parallelize, shard the app_key list across
  several `run_pipeline.py` invocations (as the original `*_b[1-4]` batch logs did).

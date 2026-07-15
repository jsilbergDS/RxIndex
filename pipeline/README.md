# Card-building pipeline

The agents/scripts that build and refine the drug cards. They operate in sequence
on `data/cards.jsonl` (then split to `data/cards/<APP>.json`). Paths inside are
cluster-specific and may need adapting; keys are read from the environment, not
hardcoded.

## Build order (each reads/writes cards.jsonl)

1. **build_cards.py** — assemble base cards from FDA metadata + review data (also
   writes `data/cards/index.md`).
2. **read_cards_llm.py** — LLM extraction of structured fields from the review docs.
3. **augment_cards.py** — general field augmentation.
4. **augment_supplements_paperclip.py** — efficacy-supplement history via paperclip.
5. **augment_adcom_discussion.py** / **merge_adcomm_data.py** — advisory-committee data
   (external tracker; now superseded by the verified re-extraction, below).
6. **augment_therapeutic_area.py** — therapeutic-area normalization.
7. **augment_yes_flags.py** — yes/no approval-shaping flags.

## Refinement / verification (already in repo root)

- **extract_cards_cited.py** → `data/cards_recited.jsonl` — attach verbatim FDA
  quotes + doc/line to each field (the citation source of truth).
- **build_claim_audit.py** → `data/claim_audit.json` — grep every number/phrase
  against the review docs; flag anything unsupported (see `AUDIT_METHOD.md`).
- **critic_semantic.py** → `data/critic_verdicts.json` — semantic critic pass.
- **run_adcomm.py** + **PROMPT_ADCOMM.md** → `adcomm_out/` — VERIFIED advisory-committee
  re-extraction (paperclip-grounded, grep-verified quotes) that replaces the external
  AdComm data in the app.
- **bundle_adcomm_docs.py** — fetch the OCR of AdComm-cited docs into `data/docs/`.

## Note

These are the *reproduction* pipeline — the app itself (`app.py`) only reads the
built `data/`. Secrets (Anthropic key) come from the environment. Not all scripts
run at HF-Space runtime; they document how a card is produced from FDA documents.

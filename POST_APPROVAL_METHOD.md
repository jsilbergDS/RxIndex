# Post-approval label changes — method (running writeup)

Goal: for each drug card, capture what changed in the FDA label **after** initial
approval — safety (boxed warning / contraindications / warnings) **and** efficacy
(indications / dosing) — dated, bucketed, and grounded in a verbatim quote at a
cited line. This complements the existing efficacy-supplement extractor.

Status: **pilot** (10 drugs). Not scaled to 236. Not yet wired into the card UI.
Last updated by hand as work proceeds — keep this current.

---

## Data sources
- **Label text**: paperclip FDA VFS. `all_documents` (source `fda`) → each drug's
  `label/YYYY/...` PDFs; body at `/fda/<doc_id>/content.lines`.
  - Read the FULL doc with line numbers via `paperclip grep -nE "^" /fda/<id>/content.lines`.
    **Do NOT use `paperclip cat`** — it truncates (~1000 chars) and strips line numbers.
- **Submissions / dates / class**: local Drugs@FDA bulk export (NOT a live API),
  `drgusatfda/fresh_20260714/Submissions.txt` (tab-delimited), class map in
  `SubmissionClass_Lookup.txt` (2=EFFICACY, 3=LABELING, 4=MANUF, 21=REMS, ...).

## Coverage (measured, 236-card set)
- 230/236 have ≥1 label doc in paperclip; 145/236 have labels in ≥2 distinct years.

---

## Key decisions & dead-ends (READ before changing the approach)

1. **Submission *class* is NOT the detection signal.** Filtering `SubmissionClassCodeID=="3"`
   (LABELING) MISSES the motivating case: Leqembi's boxed warning was added **7/2023**,
   which is **SUPPL 1, class EFFICACY (2023-07-06)** — the accelerated→traditional
   conversion. Leqembi's first LABELING (class-3) supplement is 11/2024, >1yr later.
   ⇒ Safety changes ride on any submission class. Use `Submissions.txt` only for
   **attribution** (match a change's M/YYYY to a supplement, any class), not detection.

2. **Whole-label version diffing is too noisy** (renumbered/retitled §5 subsections →
   false "new warning"; §4 boundary bugs). Abandoned as the primary method.

3. **Label ordering MUST use the `Revised: M/YYYY` stamp inside the label**, not the URL
   year or the supplement number. Leqembi's Jan-2023 (accelerated, **no** boxed warning)
   and Jul-2023 (traditional, **boxed added**) labels are BOTH url-year 2023 / suppl s000;
   only the internal `Revised:` date separates them. Getting this wrong made me wrongly
   report "no boxed warning was added" — the exact opposite of the truth.

4. **Primary signal = the label's own `RECENT MAJOR CHANGES` (RMC) box.** FDA-mandated in
   Highlights; self-reports `<Section (refs)>  M/YYYY` for each recent major change.
   Rolling ~1-year window, so **union RMC across all label versions** for the full timeline.
   Covers safety (Boxed Warning / Contraindications / Warnings) and efficacy (Indications /
   Dosage). Verquvo correctly yields 0 (no RMC box = no post-approval major change).

---

## Pipeline

**Stage A — timeline (deterministic, no LLM):** `pas_rmc_pilot.py`
1. `label_docs(app_key)`: all label docs (`total_lines>=50`), each read for its
   `Revised: M/YYYY` stamp; sort chronologically by that date.
2. `parse_rmc(blocks)`: pull `<section>  M/YYYY` lines from the RMC box of each version.
3. `categorize(sect)`: bucket by the **section number** in the ref (definitive):
   4→Contraindications, 5→Warnings (SAFETY); 1→Indications, 2→Dosage (EFFICACY);
   "Boxed Warning"→SAFETY. Title-word fallback for entries without a number.
4. Dedup by (section-text, date); output the dated timeline. → `pas_rmc_out/<app>.json`

**Stage B — "what changed" (Opus, cited):** `pas_rmc_desc.py`
- For each RMC event (section S, date D, announced in version V): `get_section()` extracts
  S's text from V (AFTER) and from the immediately-prior version V-1 (BEFORE); Opus states
  the one-line delta with a quote copied from AFTER; quote is grep-verified against AFTER.
- Prompt lives in `pas_rmc_desc.py` (`PROMPT`). Model `claude-opus-4-8`.
- → `pas_rmc_desc_out/<app>.json`

**Stage C — attribution (planned):** match each event's M/YYYY to the `Submissions.txt`
supplement approved on/near that date (any class) to label it with submission #.

**Stage D — card UI (planned):** a dated "Post-approval label changes" timeline on the
card + a sidebar filter (e.g. "boxed warning added after approval").

---

## Validation
- **Leqembi (BLA761269)** verified end-to-end: Jan-2023 label has no `WARNING:` block;
  7/2023 label adds `## WARNING: AMYLOID RELATED IMAGING ABNORMALITIES`. Stage B produced:
  7/2023 boxed **added**; 11/2024 **strengthened** (focal deficits mimic stroke);
  1/2025 **strengthened** ("ARIA can be fatal"). All quotes grep-verified.

## Open issues
- **Recall gap in Stage B**: on Leqembi, 3/11 events returned "no change extracted"
  (`Indications (1)` 7/2023, `Contraindications (4)` 7/2023, `Warnings (5.1)` 11/2024).
  Suspected cause: `get_section()` heading-regex not matching §1/§4 cleanly → empty AFTER →
  describe() skipped. Fix and re-verify before scaling.
- RMC entry formats vary by sponsor (parens vs comma; whole-section `(5)` vs `(5.1)`).
- Not run beyond the 10-drug pilot.

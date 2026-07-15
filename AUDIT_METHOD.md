# Drug card claim audit — method

Every number and every multi-word phrase that appears in `data/cards.jsonl` is
verified against the bundled FDA review documents (`data/docs/*.txt`). Output:
`data/claim_audit.json`. Builder: `build_claim_audit.py`.

The point of this audit is **to catch hallucinations** from the earlier Sonnet
passes that built the cards. If a number or quote can't be tied to a line in
the FDA documents, it goes into a flagged bucket so we can investigate.

## What gets audited

A field is audited iff its dotted path matches one of the patterns in
`AUDIT_FIELDS` in the builder script. `*` matches any list index.

```
pivotal_trials.*.phase                   pivotal_trials.*.design
pivotal_trials.*.endpoint                pivotal_trials.*.trial_name
pivotal_trials.*.n                       pivotal_trials.*.result
key_safety                               target_moa
modality                                 _meta.indication
indication_details.line_of_treatment     indication_details.setting
indication_details.population_restrictions
indication_details.combination           indication_details.prior_therapy_required
supplements.*.description
advisory_committee_detail.linkages.*.concern
advisory_committee_detail.linkages.*.change
advisory_committee_detail.linkages.*.category
advisory_committee_detail.linkages.*.evidence
```

Fields populated from external structured datasets (drugs@fda metadata, AdComm
tracker, etc.) are **not** audited — they don't come from the review docs.

## Claim extraction

For each audited field value, we extract:

1. **One phrase per field** — the whole field value, treated as a unit. We
   tried splitting on `;`/`.` to get sub-clauses; it produced too many false
   matches where significant tokens happened to appear scattered across an
   unrelated section. So: one phrase per field, period.
2. **Numbers** — every numeric token, found via:
   ```
   p\s*[<>=]\s*0?\.\d+        # p<0.05, p=0.001
   \bn\s*=\s*\d{2,}\b          # n=6063
   \b\d+\.\d+%                  # 13.4%
   \b\d+%                       # 28%
   \b\d+/\d+\b                  # 406/3034
   -?\b\d+\.\d{1,4}\b           # 1.02, -0.25
   \b\d{2,3}(?:,\d{3})+\b       # 6,063
   \b\d{3,}\b                   # 6063
   ```
   We skip 1- and 2-digit bare integers — they're too common to be useful
   audit anchors.

## Doc loading

Each `data/docs/<doc_id>.txt` file has line prefixes `L0:`, `L1:`, ... matching
the line numbers paperclip exposes. We parse this into `(line_number, content)`
tuples. Continuation lines (no `L<n>:` prefix) are folded into the previous
line so a single conceptual line is one entry.

## Normalization

```
- NFKD unicode normalize
- lowercase
- unify all dash variants (en-dash, em-dash, hyphen, minus) -> "-"
- unify all apostrophe variants -> "'"
- unify all double-quote variants -> "\""
- collapse runs of whitespace to single space
- strip leading/trailing whitespace
```

## Matching tiers — phrases

| Tier | Rule | Meaning |
|---|---|---|
| **EXACT**      | `claim` is a verbatim substring of some `L<n>:` line in some drug doc | Direct quote, full match |
| **NORMALIZED** | After normalization, the claim is a substring of a normalized line | Real quote, minor cosmetic drift (casing, dashes, whitespace) |
| **UNVERIFIED** | Neither tier matched | Composite synthesis, paraphrase, or hallucination — must be inspected |

There is **deliberately no fuzzy "proximity" tier for prose**. An earlier
version had one that allowed all significant tokens to appear within a
10-line window; in practice it surfaced false positives where the words
happened to co-occur across an unrelated paragraph break.

## Matching tiers — numbers

Numbers behave differently because the same numeric value can appear many
times in a long doc, so we need disambiguation.

| Tier | Rule |
|---|---|
| **EXACT**              | Full numeric token (e.g. `p=0.341` or `13.4%`) appears verbatim in some line |
| **BARE_WITH_CONTEXT**  | Bare numeric value (`0.341` from `p=0.341`, or `13.4` from `13.4%`) appears in a line that ALSO contains at least one other significant token from the same field (>=4-char content word OR another bare numeric value from the same field) |
| **BARE_ALONE**         | Bare value found but no contextual co-occurrence — too ambiguous to assign |
| **UNVERIFIED**         | Bare value not found in any drug doc |

The context-token set for a field is built from:
- All content words >=4 chars from the field value (excluding stopwords).
- All other bare numeric values appearing elsewhere in the same field.

`(?<![\d\.])<bare>(?![\d])` is the word-boundary regex used to find bare values
so that "0.341" does not accidentally match inside "10.3415".

## Output shape

```jsonc
// data/claim_audit.json
{
  "summary": { "EXACT": 912, "NORMALIZED": 8, "BARE_WITH_CONTEXT": 27, "BARE_ALONE": 0, "UNVERIFIED": 319 },
  "audit": {
    "BLA208471": {
      "drug": "Adlyxin",
      "doc_ids": ["...", "...", ...],
      "fields": {
        "pivotal_trials.0.result": {
          "value": "HR 1.02 (95% CI 0.89–1.17); ...",
          "phrases": {
            "HR 1.02 (95% CI 0.89–1.17); ...": { "tier": "UNVERIFIED" }
          },
          "numbers": {
            "1.02":   { "tier": "EXACT", "doc_id": "c4c27fae...", "line": 325, "page": 46, "matched_text": "..." },
            "0.89":   { "tier": "BARE_WITH_CONTEXT", "doc_id": "...", "line": 412, "page": 60, ... },
            "13.4%":  { "tier": "EXACT", ... },
            ...
          }
        },
        ...
      }
    },
    ...
  }
}
```

A field's overall verification status (for UI rendering) should be the **worst**
tier across all of its claims:
```
EXACT < NORMALIZED < BARE_WITH_CONTEXT < BARE_ALONE < UNVERIFIED
```

## Interpreting results

- **All numbers EXACT/BARE_WITH_CONTEXT, phrase UNVERIFIED**: typical for composite
  fields like `pivotal_trials.*.result`. Sonnet synthesized the description from
  multiple parts of the doc; the underlying numbers are real, the phrasing is
  rewritten.
- **A number UNVERIFIED**: most likely hallucinated, or pulled from a source
  document we don't have bundled (e.g. label vs. review doc). Worth investigating
  by hand.
- **Phrase EXACT**: direct quote from the doc.
- **Phrase NORMALIZED**: real quote with cosmetic drift — almost always real.

## Re-run

```
cd flask_hf
PYTHONUNBUFFERED=1 python3 build_claim_audit.py
```

Idempotent. ~3 seconds for 20 cards / 185 docs. Output goes to
`data/claim_audit.json`.

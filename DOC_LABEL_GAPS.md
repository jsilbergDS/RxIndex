# OCR-panel label gaps

From the rendered RxIndex Cards page: **901** distinct citation labels.
Two residual gaps in the `_doc_panel_label` enrichment (see how it's built in the
chat / `build_doc_meta.py`). Regenerate with `find_doc_label_gaps.py`.

## Generic 'Review Document' (3) — mapped, but filename suffix didn't map to a specific type
| doc_id | drug | year | filename | shown as |
|---|---|---|---|---|
| `23c3afca-f102-52b2-8496-5696a2b74922` | Aduhelm | 2021 | `761178Orig1s000MedR_Redacted.pdf` | Aduhelm Review Document · 2021 |
| `a3ab80c4-ea6b-5bd9-9091-5bae67bab42e` | Rylaze | 2021 | `761179s000lbledt.pdf` | Rylaze Review Document · 2021 |
| `d4e61add-d8c9-585b-89c8-eed07983fc34` | Forzinity | — | `unapproved_CRLs.zip#CRL_NDA215244_20250515.pdf` | Forzinity Review Document |

## Raw doc-id fallback (12) — no metadata (not in paperclip / no bundled OCR match)
| doc_id | in doc_meta | bundled .txt |
|---|---|---|
| `338cba4d-f409-54fa-9e1d-d920356df705` | False | False |
| `338fc153-0ad4-5760-85da-0f99cf20b1de` | False | False |
| `6083a517-936c-56d4-91bc-9e65af67955f` | False | False |
| `8ac2243b-a43b-5db6-8915-8b3d7eefd9c7` | False | False |
| `a6c123e3-8140-5790-8759-04835d270c56` | False | False |
| `beb9aa6c-c9ca-5f28-a794-07124c01f076` | False | False |
| `ca664a11-199c-53a2-986c-9f4a24a7b5dd` | False | True |
| `d06a498a-b024-5650-89f3-74b3ac55a0d3` | False | False |
| `d3a55e76-c5f9-555f-8ede-59a3f2d4d2e6` | False | False |
| `d3ac1236-ca25-57c6-b60a-2229745836b4` | False | False |
| `ee903dd8-510e-52af-8572-538c6880eded` | False | False |
| `f637b8c0-e050-52fa-a7c4-b0ca361304cf` | False | False |

## Diagnosis / fixes

**Generic (3)** — all resolvable; filename-suffix parse misses, not missing data:
- `23c3afca` Aduhelm `…MedR_Redacted.pdf` → **Medical Review** (`_Redacted` tail breaks the `MedR` match).
- `a3ab80c4` Rylaze `…lbledt.pdf` → **Label** (`edt` tail breaks the `lbl` match).
- `d4e61add` Forzinity `…CRL_NDA215244_20250515.pdf` → **Complete Response Letter** (2025-05-15),
  from `unapproved_CRLs.zip` — non-standard path, so no `/YYYY/` year matched.
- Fix: in `_infer_doc_type`, match suffix codes as substrings before trailing modifiers
  (`_redacted`, `edt`), and special-case CRL filenames.

**Raw doc-id fallback (12)** — deeper than a label gap: all 12 are absent from `doc_meta`,
and **11 of 12 have no bundled OCR `.txt`** — so those citations cannot open the document
at all (content missing, not just the label). Only `ca664a11` has bundled OCR but was not
returned by paperclip. Fixing these means locating the source docs, not just labeling.

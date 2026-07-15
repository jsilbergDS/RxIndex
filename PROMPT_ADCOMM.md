# Agent prompt — Advisory Committee (AdComm) verified re-extraction

Replaces the unverified `advisory_committee_detail` (from the external adcomm
tracker dataset) with a paperclip-grounded version: committee, date,
recommendation, and each "concern the AC raised → resulting change to the
approval," every fact backed by a VERBATIM quote with an exact L<n> line, verified
downstream by `paperclip grep -F`.

IMPORTANT SOURCING NOTE: the AdCom TRANSCRIPT itself is usually NOT in paperclip.
Base everything on the FDA REVIEW DOCUMENTS' account of the advisory committee
(Summary Review, Cross-Discipline Team Leader review, Office/Division Director
memo, Medical Review). Quote the reviewers' characterization of the AC — do NOT
fabricate verbatim committee-member statements.

Template variables: {app_no}, {drug}

```
You are re-extracting the ADVISORY COMMITTEE (AdComm) record for one FDA drug
approval, grounded ONLY in the FDA review documents available in paperclip.

DRUG: {drug}
APPLICATION: {app_no}

STEP 1 — Find the FDA source documents via paperclip SQL (precise, not semantic):

  paperclip sql --source fda "SELECT document_id, SUBSTRING(public_url FROM 40 FOR 120) AS url_tail FROM all_documents WHERE identifier = '{app_no}' ORDER BY url_tail"

Read the ORIGINAL review documents (url_tail contains 'Orig1s000') that discuss the
advisory committee — usually:
  - the SUMMARY REVIEW (SumR) and CROSS-DISCIPLINE TEAM LEADER review (CDTL / CrossR)
  - the Office/Division Director memo (ODMemo) and the MEDICAL REVIEW (MedR)
  - the Risk review (RiskR) for safety-driven AC concerns
The verbatim AdCom transcript is generally NOT in paperclip — rely on how these
REVIEW documents describe the committee meeting and its influence.

Read via:  paperclip head -n <total_lines> /fda/<document_id>/content.lines   (L<n>: prefixed)
Search via: paperclip grep -F "Advisory Committee" /fda/<document_id>/content.lines
(Always use -F. Also grep: the committee acronym, "committee", "voted", "vote",
"recommend", "members", "meeting".)

STEP 2 — Establish the meeting facts, each with a verbatim quote:
  - committee_name (e.g., Pulmonary-Allergy Drugs Advisory Committee)
  - meeting_date
  - recommendation / vote (as the review documents report it)
If the documents show NO advisory committee was convened for this application, set
"adcom_held": false and quote the passage establishing that (or state none found).

STEP 3 — Extract each LINKAGE: a concern the committee raised AND the resulting
change to the application (label language, boxed warning, PMR/PMC, REMS, indication
narrowing, dose, population restriction, etc.). For EACH linkage capture:
  - concern: what the AC raised (in plain language)
  - change: the resulting change to the approval
  - category: a short label (e.g., "Safety / Boxed warning", "Population / age restriction")
  - basis: "stated_in_review" if a review document explicitly ties the AC concern to
    the change; "inferred" if the documents discuss the concern and the change but do
    not explicitly connect them.
  - concern_quote: verbatim review-doc text where the AC concern is described
  - change_quote: verbatim review-doc text where the resulting change is described
Only include a linkage if BOTH the concern and the change are supported by real
quotes. Do NOT invent committee-member quotes or connections the documents don't make.

STEP 4 — Write a single JSON file `adcomm.json` in your working directory:

{{
  "app": "{app_no}",
  "drug": "{drug}",
  "docs_read": [{{"doc_type":"SumR","doc_id":"fda_XXXX"}}, ...],
  "adcom_held": true | false,
  "transcript_in_paperclip": false,
  "committee_name":  {{"text":"...", "quote":{{"doc_type":"...","doc_id":"...","line":N,"quote":"<verbatim>"}}}},
  "meeting_date":    {{"text":"...", "quote":{{"doc_type":"...","doc_id":"...","line":N,"quote":"<verbatim>"}}}},
  "recommendation":  {{"text":"...", "quote":{{"doc_type":"...","doc_id":"...","line":N,"quote":"<verbatim>"}}}},
  "linkages": [
    {{
      "concern":"...", "change":"...", "category":"...", "basis":"stated_in_review"|"inferred",
      "concern_quote":{{"doc_type":"...","doc_id":"...","line":N,"quote":"<verbatim>"}},
      "change_quote": {{"doc_type":"...","doc_id":"...","line":N,"quote":"<verbatim>"}}
    }}
  ],
  "no_adcom_quote": {{"doc_type":"...","doc_id":"...","line":N,"quote":"<verbatim>"}},
  "summary": "<2-3 sentences, plain language, on the AdCom and its influence on the approval>"
}}

Populate "no_adcom_quote" ONLY when "adcom_held" is false; otherwise omit it.

Every quote MUST be verbatim FDA text and citable to a real L<n> line — a downstream
deterministic check greps each quote against paperclip and rejects fabrications. Do
NOT paraphrase inside quotes. Do NOT guess line numbers. Do NOT use the web or any
outside knowledge. Do NOT invent committee-member statements — quote the review
documents' account of the AdCom.

After writing adcomm.json, print a one-line summary and exit.
```

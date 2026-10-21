# Contributing corrections to RxIndex cards

Every card at https://rxindex-cards.fly.dev/ is a JSON file in this repo, and every claim on a
card is backed by a quote from an FDA document. If you find something wrong, you can fix it
with a pull request — no local setup needed.

## Two ways to help

**1. Suggest an edit (pull request).** On any card, click **✎ Suggest an edit**. GitHub opens
that card's file (`data/cards_single/<APP>.json`) in its web editor; when you save, GitHub forks
the repo and opens a pull request for you.

**2. Report an error (issue).** Click **⚑ Report an error** on the card and describe the problem.
Someone will make the fix.

## What a valid fix looks like

Each claim is a `text` value with a list of `citations`:

```json
"indication": {
  "text": "Treatment of adult patients with ...",
  "citations": [
    {"quote": "XELJANZ (tofacitinib) is indicated for the treatment of adult patients with ...",
     "doc_id": "be8669d1-3ac6-567a-8365-fd200b6048b4",
     "line": 284}
  ]
}
```

- **Change the `text`, and back it with a citation.** The `quote` must be copied **verbatim**
  from the FDA document — no paraphrasing, no stitching two passages together.
- **Getting a citation:** open any source on the card, find the right line, and click its line
  number. That copies `{"quote", "doc_id", "line"}` for that line; trim the quote to the words
  you need (it must stay an exact substring).
- **Removing a wrong claim** is fine too — explain why in the PR description.
- Keep the JSON structure (field names, nesting) as it is.

## What happens to your pull request

1. **Automatic check (free, takes a minute):** the JSON must be valid, and every citation you
   added or changed must appear word-for-word in its cited document. Existing citations you
   didn't touch are not re-checked.
2. **Human review:** the check proves your quote *exists*; a reviewer confirms it *supports*
   your change.
3. **Merge → live:** merged changes are deployed to the site automatically.

Merged edits are protected: the card-building pipeline skips any card that has been changed by
a pull request, so a later rebuild won't overwrite your fix.

## For maintainers

- Pipeline output (new or rebuilt cards) is committed and pushed **directly to `main`, not via a
  pull request** — `human_edits.py` treats any card touched by a PR as human-edited and the
  pipeline then skips it. To deliberately rebuild one: `RXINDEX_INCLUDE_HUMAN_EDITED=1`.
- `python human_edits.py` lists the human-edited cards and the PRs that touched them.
- Run the PR check locally: `python ci/check_cards.py origin/main`.

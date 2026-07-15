#!/usr/bin/env python3
"""Audit every number and every substantive phrase in each drug card against the
bundled FDA review documents. Outputs data/claim_audit.json.

See AUDIT_METHOD.md for the algorithm specification.

Run:
  cd flask_hf && PYTHONUNBUFFERED=1 python3 build_claim_audit.py
"""
import json, re, unicodedata, sys
from pathlib import Path
from tqdm import tqdm

HERE = Path(__file__).parent
DATA = HERE / "data"
CARDS_JSONL  = DATA / "cards.jsonl"
DOCS_INDEX   = DATA / "docs_index.json"
PAGES_META   = DATA / "pages_meta.json"
DOCS_DIR     = DATA / "docs"
OUT_PATH     = DATA / "claim_audit.json"

# --- Which fields are Sonnet-generated and need audit ---
# Field paths use dotted form; '*' matches any list index.
AUDIT_FIELDS = {
    "pivotal_trials.*.phase",
    "pivotal_trials.*.design",
    "pivotal_trials.*.endpoint",
    "pivotal_trials.*.trial_name",
    "pivotal_trials.*.n",
    "pivotal_trials.*.result",
    "key_safety",
    "target_moa",
    "modality",
    "_meta.indication",
    "indication_details.line_of_treatment",
    "indication_details.setting",
    "indication_details.population_restrictions",
    "indication_details.combination",
    "indication_details.prior_therapy_required",
    "supplements.*.description",
    "advisory_committee_detail.linkages.*.concern",
    "advisory_committee_detail.linkages.*.change",
    "advisory_committee_detail.linkages.*.category",
    "advisory_committee_detail.linkages.*.evidence",
}


# --- Text utilities ---

def normalize(s: str) -> str:
    """Lowercase, NFKD, unify dashes/quotes, collapse whitespace."""
    if s is None: return ""
    s = unicodedata.normalize("NFKD", str(s)).lower()
    s = re.sub(r"[‐-―−­]", "-", s)        # any dash -> hyphen
    s = re.sub(r"[‘’ʼ']", "'", s)              # any apostrophe
    s = re.sub(r"[“”„«»\"]", '"', s) # any double quote
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Token patterns for number extraction.
NUMBER_PATTERNS = [
    r"p\s*[<>=]\s*0?\.\d+",        # p<0.05, p=0.001
    r"\bn\s*=\s*\d{2,}\b",          # n=6063 (>=2 digit)
    r"\b\d+\.\d+%",                  # 13.4%
    r"\b\d+%",                       # 28%
    r"\b\d+/\d+\b",                  # 406/3034
    r"-?\b\d+\.\d{1,4}\b",           # 1.02, -0.25, 0.18 (decimal)
    r"\b\d{2,3}(?:,\d{3})+\b",       # 6,063
    r"\b\d{3,}\b",                   # 6063, 1024, etc. (>=3 digit ints)
]
NUMBER_RE = re.compile("|".join(f"(?:{p})" for p in NUMBER_PATTERNS), re.IGNORECASE)

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "to", "for", "on", "at",
    "with", "by", "from", "is", "are", "was", "were", "be", "been", "being",
    "as", "vs", "vs.", "versus", "this", "that", "these", "those", "it", "its",
    "if", "than", "then", "also", "not", "no", "yes",
}


def extract_numbers(text: str) -> list[str]:
    """Return distinct numeric tokens found in `text`, in order of first appearance."""
    seen = set()
    out = []
    for m in NUMBER_RE.finditer(text):
        tok = m.group(0).strip()
        norm = normalize(tok)
        if norm in seen: continue
        seen.add(norm)
        out.append(tok)
    return out


def extract_phrases(text: str) -> list[str]:
    """One phrase per field value. We only check the whole field as a unit — sub-clause
    extraction was too eager and matched scattered tokens across unrelated sections."""
    if not text or not text.strip(): return []
    return [text.strip()]


def field_context_tokens(text: str) -> set[str]:
    """Significant tokens from a field — used to disambiguate which occurrence of
    a number in a doc is the right one (must co-occur with at least one of these)."""
    if not text: return set()
    norm = normalize(text)
    # Pull words >= 4 chars (less ambiguous than short ones) and not stopwords
    words = re.findall(r"[a-z][a-z'-]{3,}", norm)
    sig = {w for w in words if w not in STOPWORDS}
    # Also include OTHER numeric tokens from the field (they help disambiguate)
    for m in NUMBER_RE.finditer(text):
        bare = _bare_number(m.group(0))
        if bare and len(bare) >= 3:
            sig.add(bare)
    return sig


# --- Doc loading ---

def read_doc_lines(doc_id: str) -> list[tuple[int, str]]:
    """Parse the L<n>: prefixed text file → list of (line_number, content)."""
    p = DOCS_DIR / f"{doc_id}.txt"
    if not p.exists(): return []
    text = p.read_text()
    out = []
    cur_n, cur_buf = None, []
    for raw in text.split("\n"):
        m = re.match(r"^L(\d+):\s?(.*)$", raw)
        if m:
            if cur_n is not None:
                out.append((cur_n, "\n".join(cur_buf).strip()))
            cur_n = int(m.group(1))
            cur_buf = [m.group(2)]
        else:
            cur_buf.append(raw)
    if cur_n is not None:
        out.append((cur_n, "\n".join(cur_buf).strip()))
    return out


_DOC_LINES_CACHE: dict[str, list[tuple[int, str]]] = {}
_DOC_NORM_CACHE:  dict[str, list[tuple[int, str]]] = {}

def doc_lines_cached(doc_id: str):
    if doc_id not in _DOC_LINES_CACHE:
        _DOC_LINES_CACHE[doc_id] = read_doc_lines(doc_id)
    return _DOC_LINES_CACHE[doc_id]

def doc_norm_lines_cached(doc_id: str):
    if doc_id not in _DOC_NORM_CACHE:
        _DOC_NORM_CACHE[doc_id] = [(ln, normalize(t)) for ln, t in doc_lines_cached(doc_id)]
    return _DOC_NORM_CACHE[doc_id]


# --- Matching ---

def line_to_page(doc_id: str, line_n: int, pages_meta: dict) -> int:
    info = pages_meta.get(doc_id) or {}
    tp, tl = info.get("total_pages"), info.get("total_lines")
    if not tp or not tl or tl <= 0: return 1
    return max(1, min(int(tp), round(int(line_n) * int(tp) / int(tl))))


def _bare_number(tok: str) -> str | None:
    """Strip 'p<', 'p=', 'n=', leading/trailing %, parens, etc. Return the bare numeric value, or None."""
    m = re.search(r"-?\d+(?:\.\d+)?", tok)
    if not m: return None
    return m.group(0)


def match_phrase_in_doc(phrase: str, doc_id: str) -> dict | None:
    """Phrase matching: T1 EXACT verbatim, T2 NORMALIZED. No fuzzy proximity for prose."""
    norm_phrase = normalize(phrase)
    if not norm_phrase: return None

    # T1: exact substring
    for ln, t in doc_lines_cached(doc_id):
        if phrase in t:
            return {"tier": "EXACT", "line": ln, "matched_text": t[:400]}
    # T2: normalized substring
    for ln, tn in doc_norm_lines_cached(doc_id):
        if norm_phrase in tn:
            return {"tier": "NORMALIZED", "line": ln, "matched_text": tn[:400]}
    return None


def match_number_in_doc(
    token: str,
    doc_id: str,
    *,
    context_tokens: set[str],
) -> dict | None:
    """Number matching — both tiers require word boundaries (so '0.89' doesn't
    match inside '0.891') AND prefer lines with co-occurring field context
    tokens (so a number that appears in many places resolves to the right one).

    T1 EXACT: full token (e.g. 'p=0.341' or '13.4%') appears with numeric boundary AND
              at least one context token co-occurs on the same line. Falls back to
              "no-context" EXACT if nothing else matches.
    T2 BARE_WITH_CONTEXT: bare numeric value found with context co-occurrence.
    T3 BARE_ALONE: bare value found, but no context co-occurrence — ambiguous.
    """
    # T1: full-token regex with numeric boundaries
    # Use (?<!...) (?!...) to prevent substring matches inside larger numbers
    tok_re = re.compile(r"(?<![\d\.])" + re.escape(token) + r"(?![\d])")
    candidates_T1_ctx = []
    candidates_T1_noctx = []
    for ln, t in doc_lines_cached(doc_id):
        if not tok_re.search(t): continue
        tn_norm = normalize(t)
        co = sum(1 for ct in context_tokens if ct and ct in tn_norm)
        if co >= 1:
            candidates_T1_ctx.append((ln, t, co))
        else:
            candidates_T1_noctx.append((ln, t))
    if candidates_T1_ctx:
        candidates_T1_ctx.sort(key=lambda x: -x[2])
        ln, t, co = candidates_T1_ctx[0]
        return {"tier": "EXACT", "line": ln, "matched_text": t[:300],
                "match_kind": f"verbatim_token co={co}"}

    # T2: bare numeric value with context
    bare = _bare_number(token)
    if not bare:
        # No bare value to fall back to — accept context-less T1 if any
        if candidates_T1_noctx:
            ln, t = candidates_T1_noctx[0]
            return {"tier": "EXACT", "line": ln, "matched_text": t[:300],
                    "match_kind": "verbatim_token (no context)"}
        return None
    bare_re = re.compile(r"(?<![\d\.])" + re.escape(bare) + r"(?![\d])")
    candidates_T2 = []
    candidates_T3 = []
    for ln, tn in doc_norm_lines_cached(doc_id):
        if not bare_re.search(tn): continue
        co = sum(1 for ct in context_tokens if ct and ct in tn)
        if co >= 1:
            candidates_T2.append((ln, tn, co))
        else:
            candidates_T3.append((ln, tn))
    if candidates_T2:
        candidates_T2.sort(key=lambda x: -x[2])
        ln, tn, co = candidates_T2[0]
        return {"tier": "BARE_WITH_CONTEXT", "line": ln, "matched_text": tn[:300],
                "match_kind": f"bare:{bare} co={co}"}

    # Last resorts: context-less EXACT, then BARE_ALONE
    if candidates_T1_noctx:
        ln, t = candidates_T1_noctx[0]
        return {"tier": "EXACT", "line": ln, "matched_text": t[:300],
                "match_kind": "verbatim_token (no context)"}
    if candidates_T3:
        ln, tn = candidates_T3[0]
        return {"tier": "BARE_ALONE", "line": ln, "matched_text": tn[:300],
                "match_kind": f"bare:{bare} (ambiguous)"}
    return None


TIER_RANK = {
    "EXACT": 0,
    "NORMALIZED": 1,
    "BARE_WITH_CONTEXT": 2,
    "BARE_ALONE": 3,
}


def match_claim(
    claim: str,
    drug_doc_ids: list[str],
    pages_meta: dict,
    *,
    is_number: bool = False,
    context_tokens: set[str] | None = None,
) -> dict:
    """Match `claim` across drug's docs. Best (lowest-rank) match wins.
    Returns {tier, doc_id, line, page, ...} or {tier: 'UNVERIFIED'}."""
    if not claim or not claim.strip():
        return {"tier": "UNVERIFIED", "reason": "empty claim"}
    best = None
    best_rank = 99
    for doc_id in drug_doc_ids:
        if is_number:
            result = match_number_in_doc(claim, doc_id, context_tokens=context_tokens or set())
        else:
            result = match_phrase_in_doc(claim, doc_id)
        if not result: continue
        rank = TIER_RANK.get(result["tier"], 99)
        if rank < best_rank:
            result["doc_id"] = doc_id
            result["page"] = line_to_page(doc_id, result["line"], pages_meta)
            best = result
            best_rank = rank
            if rank == 0: break
    return best or {"tier": "UNVERIFIED"}


# --- Walking the card ---

def field_path_matches(path: str, pattern: str) -> bool:
    """`pattern` uses '*' to match any list index."""
    pp = pattern.split(".")
    pa = path.split(".")
    if len(pp) != len(pa): return False
    for s_pat, s_act in zip(pp, pa):
        if s_pat == "*":
            if not s_act.isdigit(): return False
        elif s_pat != s_act:
            return False
    return True


def walk_card_strings(card, prefix=""):
    """Yield (field_path, value_string) for every string-valued leaf."""
    if isinstance(card, dict):
        for k, v in card.items():
            yield from walk_card_strings(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(card, list):
        for i, v in enumerate(card):
            yield from walk_card_strings(v, f"{prefix}.{i}")
    else:
        if card is None: return
        s = str(card).strip()
        if not s: return
        yield prefix, s


def is_audit_field(path: str) -> bool:
    return any(field_path_matches(path, pat) for pat in AUDIT_FIELDS)


# --- Main build ---

def main():
    cards = [json.loads(l) for l in open(CARDS_JSONL)]
    docs_index = json.load(open(DOCS_INDEX))
    pages_meta = json.load(open(PAGES_META))
    print(f"Loaded {len(cards)} cards, {len(docs_index)} drug-doc lists, "
          f"{len(pages_meta)} doc meta entries", flush=True)

    audit = {}
    summary = {t: 0 for t in ["EXACT", "NORMALIZED", "BARE_WITH_CONTEXT", "BARE_ALONE", "UNVERIFIED"]}

    for card in tqdm(cards, desc="Cards"):
        meta = card.get("_meta", {})
        appno = meta.get("appno"); typ = meta.get("type")
        if not appno or not typ: continue
        app_key = f"{typ}{appno.zfill(6)}"
        drug_docs = docs_index.get(app_key, [])
        if not drug_docs:
            print(f"  [skip] {app_key} ({meta.get('drug')}): no docs in index", flush=True)
            continue
        drug_doc_ids = [d["document_id"] for d in drug_docs]

        card_audit = {}
        for path, val in walk_card_strings(card):
            if not is_audit_field(path): continue

            ctx = field_context_tokens(val)
            field_entry = {"value": val, "phrases": {}, "numbers": {}}
            for phrase in extract_phrases(val):
                if phrase in field_entry["phrases"]: continue
                result = match_claim(phrase, drug_doc_ids, pages_meta, is_number=False)
                field_entry["phrases"][phrase] = result
                summary[result["tier"]] += 1
            for number in extract_numbers(val):
                if number in field_entry["numbers"]: continue
                result = match_claim(number, drug_doc_ids, pages_meta, is_number=True, context_tokens=ctx)
                field_entry["numbers"][number] = result
                summary[result["tier"]] += 1
            if field_entry["phrases"] or field_entry["numbers"]:
                card_audit[path] = field_entry

        audit[app_key] = {
            "drug": meta.get("drug"),
            "doc_ids": drug_doc_ids,
            "fields": card_audit,
        }

    # Write output
    with open(OUT_PATH, "w") as f:
        json.dump({"summary": summary, "audit": audit}, f, indent=2, ensure_ascii=False)

    total = sum(summary.values())
    print(f"\nTotal claims checked: {total}", flush=True)
    for tier, n in summary.items():
        pct = (100.0 * n / total) if total else 0
        print(f"  {tier:12s} {n:5d}  ({pct:5.1f}%)", flush=True)
    print(f"\nSaved: {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()

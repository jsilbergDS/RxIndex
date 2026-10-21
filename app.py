#!/usr/bin/env python3
"""Drug cards web app with embedded per-drug Sonnet chat.

For each 2024 NME we extracted into cards_2024_test.jsonl, render the
existing card AND show:
  - the list of FDA review documents linked from paperclip
  - a chat box that talks to Sonnet 4.6 with tool-use access to those docs

Tools the chat has:
  list_docs()            -> [{type, document_id, public_url}, ...]
  read_doc(document_id)  -> full OCR'd content (from in-memory cache)

Sessions are kept in memory keyed by (browser-session, app_no).

Launch:
  PYTHONUNBUFFERED=1 python3 app.py
Then visit http://localhost:8503 (or proxied via OnDemand).
"""
import os, sys, json, time, uuid, threading, re, subprocess
from flask import Flask, request, jsonify, send_from_directory, send_file, Response
import anthropic
from pathlib import Path

# Paths are relative to this file — bundled data/ ships with the app.
HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DOCS_DIR = DATA_DIR / "docs"
CARDS_JSONL = DATA_DIR / "cards.jsonl"
DOCS_INDEX_PATH = DATA_DIR / "docs_index.json"
PAGES_META_PATH = DATA_DIR / "pages_meta.json"
PDF_CACHE_DIR = Path(os.environ.get("PDF_CACHE_DIR", str(DATA_DIR / "pdfs_cache")))
PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
MODEL = "claude-sonnet-4-6"
PORT = int(os.environ.get("PORT", 7860))

# Spending controls (configurable via env)
DAILY_USD_BUDGET = float(os.environ.get("DAILY_USD_BUDGET", "50.0"))

app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_host=1, x_proto=1)
api_key = os.environ.get("ANTHROPIC_API_KEY")
# Optional: the app serves cards + citations with NO key. Chat needs it (set it as a
# HF Space secret). If unset, the app still boots and chat returns a friendly notice.
client = anthropic.Anthropic(api_key=api_key) if api_key else None

# --- Optional password gate (cookie-based, password-only UX) ---
# Set APP_PASSWORD as a Space secret to require login.
# Leave APP_PASSWORD unset to disable auth (dev mode).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
# Chat is OFF by default (code preserved). Set CHAT_ENABLED=1 to re-enable the per-drug chat.
CHAT_ENABLED = os.environ.get("CHAT_ENABLED", "").strip() == "1"
# GitHub repo backing the card content — enables per-card "Suggest an edit" (PR via the
# GitHub web editor) and "Report an error" (issue) links. Set GITHUB_REPO="" to hide them.
GITHUB_REPO = os.environ.get("GITHUB_REPO", "jsilbergDS/RxIndex").strip()
import secrets as _secrets, hashlib as _hashlib
# Stable secret key so login sessions survive Space restarts (a random key per boot
# would silently log everyone out). Tied to the password when no explicit key is set.
app.config["SECRET_KEY"] = (
    os.environ.get("FLASK_SECRET_KEY")
    or (_hashlib.sha256(("rxindex::" + APP_PASSWORD).encode()).hexdigest()
        if APP_PASSWORD else _secrets.token_hex(32)))
# HF serves the app inside an iframe on huggingface.co (cross-site). A default
# SameSite=Lax cookie is dropped there, so login "succeeds" then bounces back to
# /login forever. SameSite=None + Secure lets the session cookie survive the iframe.
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
from flask import session, redirect, url_for, make_response

LOGIN_PAGE_HTML = """<!doctype html><html><head>
<meta charset='utf-8'><title>Sign in — FDA Drug Cards</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;background:#f3f4f6;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .box{background:#fff;padding:32px 40px;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,0.08);max-width:380px;width:90%}
  h1{margin:0 0 6px 0;font-size:18px;font-weight:700;color:#1f2937}
  .sub{font-size:13px;color:#6b7280;margin-bottom:20px}
  input[type=password]{width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;box-sizing:border-box;margin-bottom:14px}
  input[type=password]:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,0.15)}
  button{width:100%;padding:10px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#1d4ed8}
  .err{color:#b91c1c;font-size:13px;margin-top:-6px;margin-bottom:10px}
</style></head><body>
<form class="box" method="post" action="login">
  <h1>FDA Drug Cards Explorer</h1>
  <div class="sub">This site is password-protected.</div>
  __ERR__
  <input type="password" name="password" placeholder="Password" autofocus required>
  <button type="submit">Sign in</button>
</form>
</body></html>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect("/")
    err = ""
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect("/")
        err = "<div class='err'>Incorrect password.</div>"
    return LOGIN_PAGE_HTML.replace("__ERR__", err)

@app.route("/logout")
def logout():
    session.pop("authed", None)
    return redirect("/login")

@app.before_request
def _auth_gate():
    if not APP_PASSWORD:
        return  # auth disabled
    if request.path in ("/login", "/logout"): return  # public
    if session.get("authed"): return  # OK
    return redirect("/login")

# Pre-built doc index (replaces paperclip SQL discovery)
with open(DOCS_INDEX_PATH) as _f: DOCS_INDEX = json.load(_f)
with open(PAGES_META_PATH) as _f: PAGES_META = json.load(_f)

# doc_id -> {url, drug} for every bundled OCR doc (built by build_doc_meta.py from
# paperclip). Used to label the OCR panel when a citation doc_id isn't in PAGES_META.
_DOC_META_PATH = DATA_DIR / "doc_meta.json"
DOC_META = json.load(open(_DOC_META_PATH)) if _DOC_META_PATH.exists() else {}


def _doc_panel_label(doc_id):
    """'<Drug> <DocType> · <Year>' for the OCR panel, from PAGES_META or DOC_META."""
    info = PAGES_META.get(doc_id, {}) or PAGES_META.get("fda_" + doc_id, {})
    url = info.get("public_url") or info.get("filename")
    if url:
        m = re.search(r"([^/\s|]+\.pdf)", url)
        return _doc_display(url) + (f" — {m.group(1)}" if m else "")
    dm = DOC_META.get(doc_id)
    if not dm:
        return _doc_label(doc_id)
    fname = dm.get("fname") or ""
    typ = _friendly(_infer_doc_type(fname, fname) if fname else None)
    drug, yr = dm.get("drug"), dm.get("year")
    lab = f"{drug} {typ}" if drug else typ
    lab = f"{lab} · {yr}" if yr else lab
    return f"{lab} — {fname}" if fname else lab   # name the actual FDA file

# Optional: claim audit (built by build_claim_audit.py). If present, used to wrap
# verified claims in clickable .qlink chips pointing at the source PDF page.
CLAIM_AUDIT_PATH = DATA_DIR / "claim_audit.json"
CLAIM_AUDIT: dict = {}
if CLAIM_AUDIT_PATH.exists():
    try:
        _ca = json.load(open(CLAIM_AUDIT_PATH))
        CLAIM_AUDIT = _ca.get("audit", {})
        print(f"[startup] loaded claim audit: {len(CLAIM_AUDIT)} drugs", flush=True)
    except Exception as _e:
        print(f"[startup] failed to load claim audit: {_e}", flush=True)

# Optional: re-extracted cards with per-claim line citations (built by
# extract_cards_cited.py with Opus 4.8). For drugs that have an entry here,
# render fields using the cited text + chip-per-citation.
CITED_PATH = DATA_DIR / "cards_recited.jsonl"
CITED_CARDS: dict = {}
if CITED_PATH.exists():
    for _line in open(CITED_PATH):
        _r = json.loads(_line)
        CITED_CARDS[_r["app_key"]] = _r["card"]
    print(f"[startup] loaded cited cards: {len(CITED_CARDS)} drugs", flush=True)

# New single-agent pilot cards (design B): merge their cited overlay too.
CITED_NEW_PATH = DATA_DIR / "cards_recited_new.jsonl"
if CITED_NEW_PATH.exists():
    for _line in open(CITED_NEW_PATH):
        _line = _line.strip()
        if not _line:
            continue
        _r = json.loads(_line)
        CITED_CARDS[_r["app_key"]] = _r["card"]
    print(f"[startup] merged new cited cards -> {len(CITED_CARDS)} total", flush=True)

# Verified AdComm re-extraction (run_adcomm.py) -> replaces the external, unverified
# advisory_committee_detail. Keyed by app_key (type+appno). Each quote is grep-verified.
ADCOMM_DIR = HERE / "adcomm_out"
ADCOMM_VERIFIED: dict = {}
if ADCOMM_DIR.exists():
    for _p in ADCOMM_DIR.glob("*.json"):
        try:
            ADCOMM_VERIFIED[_p.stem] = json.loads(_p.read_text())
        except Exception:
            pass
    print(f"[startup] loaded verified AdComm: {len(ADCOMM_VERIFIED)} drugs", flush=True)

def canon_docid(d):
    """Canonical doc_id used for the bundled OCR filenames + /doc route: strip a
    leading 'fda_' so 'fda_<uuid>' and '<uuid>' resolve to the same source."""
    return d[4:] if d and d.startswith("fda_") else d

PAPERCLIP = os.environ.get("PAPERCLIP_BIN", "/home/users/jsilberg/.local/bin/paperclip")

def _paperclip_ocr(doc_id):
    """Fetch a doc's current paperclip OCR (L<n>: prefixed). Source of truth for the
    agents' citations. Returns None if paperclip is unavailable (e.g. on HF)."""
    cd = canon_docid(doc_id); body = cd.replace("-", "")
    for v in (cd, "fda_" + body[:12], body, doc_id):
        try:
            r = subprocess.run([PAPERCLIP, "head", "-n", "8000", f"/fda/{v}/content.lines"],
                               capture_output=True, text=True, timeout=45)
        except Exception:
            continue
        if re.search(r"^L\d+:", r.stdout, re.MULTILINE):
            return r.stdout
    return None

# doc_id -> {total_pages, total_lines, public_url, filename}
for _doc_id, _info in PAGES_META.items():
    _info["filename"] = (_info.get("public_url", "") or "").rsplit("/", 1)[-1]


def doc_short_id(doc_id: str) -> str:
    """First 8 chars of UUID — used as a compact citation key in chat text."""
    return doc_id[:8]


# Reverse lookup: short id -> full doc id (collisions handled by sticking
# with first registered — UUID8 collisions are astronomically unlikely here)
SHORT_TO_FULL = {}
for _doc_id in PAGES_META:
    SHORT_TO_FULL.setdefault(doc_short_id(_doc_id), _doc_id)


def line_to_page(doc_id: str, line_n: int) -> int:
    """Linear-interp line number to PDF page number (1-indexed)."""
    info = PAGES_META.get(doc_id)
    if not info: return 1
    tp = info.get("total_pages") or 1
    tl = info.get("total_lines") or 1
    if tl <= 0: return 1
    return max(1, min(int(tp), round(int(line_n) * int(tp) / int(tl))))


def citation_to_pdf(doc_id_or_short: str, line_n) -> dict | None:
    """Resolve a citation to {url, page, filename, doc_id} for the PDF viewer."""
    doc_id = SHORT_TO_FULL.get(doc_id_or_short) or (
        doc_id_or_short if doc_id_or_short in PAGES_META else None)
    if not doc_id: return None
    info = PAGES_META[doc_id]
    try: line_i = int(line_n)
    except (ValueError, TypeError): line_i = 1
    return {
        "url": f"pdfs/{info['filename']}",
        "page": line_to_page(doc_id, line_i),
        "filename": info["filename"],
        "doc_id": doc_id,
    }

# --- Load cards data ---
CARDS = []
# The app shows ONLY the new single-agent pilot cards (design B). Set
# SHOW_OLD_CARDS=1 to also include the original 20 (data/cards/).
if os.environ.get("SHOW_OLD_CARDS") == "1":
    CARDS_DIR = DATA_DIR / "cards"
    if CARDS_DIR.exists() and any(CARDS_DIR.glob("*.json")):
        for _p in sorted(CARDS_DIR.glob("*.json")):
            try:
                CARDS.append(json.loads(_p.read_text()))
            except Exception:
                pass
    elif CARDS_JSONL.exists():
        with open(CARDS_JSONL) as f:
            for line in f:
                CARDS.append(json.loads(line))
    print(f"[startup] SHOW_OLD_CARDS=1 -> loaded {len(CARDS)} old cards", flush=True)

# New single-agent pilot cards (design B), value shape converted for the app.
CARDS_NEW_DIR = DATA_DIR / "cards_new"
if CARDS_NEW_DIR.exists():
    _n0 = len(CARDS)
    for _p in sorted(CARDS_NEW_DIR.glob("*.json")):
        try:
            CARDS.append(json.loads(_p.read_text()))
        except Exception:
            pass
    print(f"[startup] loaded {len(CARDS) - _n0} NEW pilot cards from data/cards_new/", flush=True)

# --- Resolve each card's app -> paperclip docs by direct SQL query ---
# Query paperclip for every doc whose URL/source_file contains the app digits.
# Skip label and appletter (not review docs); keep everything else.
import re as _re
UUID_RE = _re.compile(r'^[0-9a-f-]{30,}$')
SKIP_PATH_SEGMENTS = ('/label/', '/appletter/')  # not reviews

def _infer_doc_type(public_url, source_file):
    bn = (public_url or source_file or '').rsplit('/', 1)[-1].lower()
    if not bn.endswith('.pdf'): return None
    stem = bn[:-4]
    # Heuristic mapping of common FDA review-doc suffixes
    suffixes = [
        ('multidiscipliner', 'MultidisciplineR'),
        ('integratedr', 'IntegratedR'),
        ('medrev', 'MedR'), ('medreview', 'MedR'), ('medredt', 'MedR'), ('medr', 'MedR'),
        ('sumr', 'SumR'),
        ('clinpharmr', 'ClinPharmR'),
        ('pharmr', 'PharmR'),
        ('biopharmr', 'BiopharmR'),
        ('statr', 'StatR'),
        ('chemr', 'ChemR'),
        ('micror', 'MicroR'), ('microbior', 'MicroR'), ('microbr', 'MicroR'),
        ('riskr', 'RiskR'),
        ('crossr', 'CrossR'),
        ('namer', 'NameR'),
        ('oelist', 'OEList'),
        ('admincorres', 'AdminCorres'),
        ('admindocs', 'AdminDocs'),
        ('approv', 'Approv'),
        ('lbl', 'Lbl'),
        ('rems', 'REMS'),
        ('otheractionltrs', 'OtherActionLtrs'),
        ('ltr', 'AppLetter'),
        ('otherr', 'OtherR'),
        ('memo', 'Memo'),
        ('odmemo', 'ODMemo'),
    ]
    for suf, label in suffixes:
        if stem.endswith(suf): return label
    return 'Other'

# Human-readable names for the terse doc-type codes above.
_FRIENDLY = {
    "MultidisciplineR": "Multidiscipline Review", "IntegratedR": "Integrated Review",
    "MedR": "Medical Review", "SumR": "Summary Review",
    "ClinPharmR": "Clinical Pharmacology Review", "PharmR": "Pharmacology Review",
    "BiopharmR": "Biopharmaceutics Review", "StatR": "Statistical Review",
    "ChemR": "Chemistry Review", "MicroR": "Microbiology Review",
    "RiskR": "Risk Assessment Review", "CrossR": "Cross-Discipline Review",
    "NameR": "Proprietary Name Review", "OEList": "Establishment List",
    "AdminCorres": "Administrative Correspondence", "AdminDocs": "Administrative Docs",
    "Approv": "Approval Memo", "Lbl": "Label", "REMS": "REMS",
    "AppLetter": "Approval Letter", "OtherActionLtrs": "Other Action Letters",
    "OtherR": "Other Review", "Memo": "Memo", "ODMemo": "Orphan Drug Memo",
    "Other": "Review Document",
}
def _friendly(code):
    return _FRIENDLY.get(code, code or "Review Document")

def _doc_display(url_or_name):
    """Friendly doc-type label. For Labels (which change post-approval) append the
    year from the path so it's clear the card cites the ORIGINAL-approval label."""
    m = re.search(r"([^/\s|]+\.pdf)", url_or_name or "")
    code = _infer_doc_type(m.group(1), m.group(1)) if m else None
    label = _friendly(code)
    # append the drugsatfda-path year (approval / supplement year) for ALL doc types
    y = re.search(r"/((?:19|20)\d\d)/", url_or_name or "")
    if y:
        label += f" · {y.group(1)}"
    return label

def discover_docs_for(app_key: str):
    """app_key like NDA217424 -> list of {type, document_id, public_url, source_file} from pre-built index."""
    return DOCS_INDEX.get(app_key, [])

def _cited_docs_for(key, value_card):
    """Docs the card CITES -> [{document_id, type, public_url, source_file}]. Grounds
    the chat on exactly the documents behind the card's citations (the new-pipeline
    cards have no DOCS_INDEX entry, so this is their doc list)."""
    seen = {}
    def w(o):
        if isinstance(o, dict):
            for c in o.get("citations", []) or []:
                did = canon_docid(c.get("doc_id") or "")
                if did and did not in seen:
                    info = PAGES_META.get(did, {}) or PAGES_META.get("fda_" + did, {})
                    seen[did] = {"document_id": did, "type": "",
                                 "public_url": info.get("public_url", ""), "source_file": ""}
            for v in o.values():
                w(v)
        elif isinstance(o, list):
            for v in o:
                w(v)
    w(value_card)
    w(CITED_CARDS.get(key) or {})
    return list(seen.values())

DRUG_DOCS = {}
for card in CARDS:
    meta = card.get("_meta", {})
    appno = meta.get("appno"); typ = meta.get("type")
    if not appno or not typ: continue
    key = f"{typ}{appno.zfill(6)}"
    idx = discover_docs_for(key)
    DRUG_DOCS[key] = idx if idx else _cited_docs_for(key, card)

# --- In-memory caches ---
DOC_TEXT_CACHE = {}   # document_id -> str
DOC_CACHE_LOCK = threading.Lock()
SESSIONS = {}         # session_id -> {app_key: [{"role": ..., "content": ...}, ...]}

def fetch_doc_text(doc_id: str) -> str:
    with DOC_CACHE_LOCK:
        if doc_id in DOC_TEXT_CACHE:
            return DOC_TEXT_CACHE[doc_id]
    # Local file read replaces paperclip's `cat --full`
    p = DOCS_DIR / f"{doc_id}.txt"
    text = p.read_text() if p.exists() else f"[doc {doc_id} not found in bundle]"
    with DOC_CACHE_LOCK:
        DOC_TEXT_CACHE[doc_id] = text
    return text


# --- PDF proxy + viewer ---

# Map of approved filenames -> public URL (so we only download files we ship)
_ALLOWED_PDFS = {info["filename"]: info["public_url"] for info in PAGES_META.values()}

import re as _re_pdf
_FNAME_RE = _re_pdf.compile(r'^[A-Za-z0-9._,-]+\.pdf$')

@app.route("/pdfs/<path:filename>")
def serve_pdf(filename):
    """Serve FDA PDFs with caching proxy. Cache → download from known public URL → 404."""
    if not _FNAME_RE.match(filename): return "Invalid filename", 400
    if filename not in _ALLOWED_PDFS: return "Unknown PDF", 404
    cached = PDF_CACHE_DIR / filename
    if cached.exists() and cached.stat().st_size > 1000:
        return send_file(str(cached), mimetype="application/pdf")
    # Download from the exact known URL (no year guessing — we already have it)
    import urllib.request
    url = _ALLOWED_PDFS[filename]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if len(data) < 1000 or data[:5] != b"%PDF-":
            return "Downloaded file is not a valid PDF", 502
        cached.write_bytes(data)
        print(f"[pdf cached] {filename} ({len(data):,}b) from {url}", flush=True)
        return send_file(str(cached), mimetype="application/pdf")
    except Exception as e:
        print(f"[pdf fetch fail] {filename}: {e}", flush=True)
        return f"Failed to fetch from FDA: {e}", 502


@app.route("/pdfview")
def pdfview():
    """PDF.js viewer — renders ±2 pages around target, highlights search text."""
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>PDF Viewer</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#525659;overflow:hidden;height:100vh;display:flex;flex-direction:column;}
#controls{background:#323639;padding:6px 12px;display:flex;align-items:center;gap:8px;color:#ccc;font-size:12px;font-family:sans-serif;flex-shrink:0;}
#controls span{color:#fff;font-weight:600;}
#viewer-container{flex:1;overflow:auto;text-align:center;}
.page-wrapper{position:relative;display:inline-block;margin:8px auto;}
canvas{display:block;box-shadow:0 2px 8px rgba(0,0,0,0.3);}
.text-layer{position:absolute;top:0;left:0;right:0;bottom:0;overflow:hidden;pointer-events:none;}
.highlight-overlay{position:absolute;background:rgba(255,255,0,0.4);border:2px solid rgba(255,180,0,0.8);pointer-events:none;border-radius:2px;}
</style>
</head><body>
<div id="controls"><span id="info">Loading...</span></div>
<div id="viewer-container"></div>
<script type="module">
import * as pdfjsLib from 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.min.mjs';
pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.worker.min.mjs';
const params=new URLSearchParams(location.search);
const url=params.get('url');
const fallbackPage=parseInt(params.get('page'))||1;
const search=(params.get('search')||'');
const info=document.getElementById('info');
const norm=t=>(t||'').toLowerCase().replace(/\s+/g,' ').trim();
info.textContent='Loading PDF...';
const pdf=await pdfjsLib.getDocument(url).promise;
const totalPages=pdf.numPages;

// The cited PAGE from line-interpolation is unreliable, so LOCATE the page that
// actually contains the cited text. Needles, most specific first: full quote,
// numeric tokens, then long words.
const needles=[];
if(search.trim()) needles.push(search.trim().slice(0,60));
(search.match(/\\d+(?:[.,]\\d+)?%?|\\d+\\/\\d+/g)||[]).forEach(x=>needles.push(x));
(search.match(/[A-Za-z][A-Za-z-]{5,}/g)||[]).forEach(x=>needles.push(x));

// Cache each page's normalized text once.
info.textContent='Locating citation…';
const pageTexts=[];
for(let p=1;p<=totalPages;p++){
  const tc=await (await pdf.getPage(p)).getTextContent();
  pageTexts[p]=norm(tc.items.map(i=>i.str).join(' '));
}
let showPage=0, located=false;
for(const nd of needles){ const n=norm(nd); if(!n||n.length<3) continue;
  for(let p=1;p<=totalPages;p++){ if(pageTexts[p].includes(n)){showPage=p; located=true; break;} }
  if(showPage) break;
}
if(!showPage) showPage=Math.min(totalPages,Math.max(1,fallbackPage));

// Highlight tokens: numbers in the citation + the located needle if short.
const hlTokens=[];
(search.match(/\\d+(?:[.,]\\d+)?%?|\\d+\\/\\d+/g)||[]).forEach(x=>hlTokens.push(norm(x)));
if(search.trim() && search.trim().length<=24) hlTokens.push(norm(search));

const container=document.getElementById('viewer-container');
const scale=1.4;
const startPage=Math.max(1,showPage-1);
const endPage=Math.min(totalPages,showPage+1);
info.textContent='Page '+showPage+' of '+totalPages+(located?'':' (approx — text not found)');
for(let p=startPage;p<=endPage;p++){
  const pg=await pdf.getPage(p);
  const viewport=pg.getViewport({scale});
  const wrapper=document.createElement('div');
  wrapper.className='page-wrapper';wrapper.id='page-'+p;
  wrapper.style.width=viewport.width+'px';wrapper.style.height=viewport.height+'px';
  container.appendChild(wrapper);
  const canvas=document.createElement('canvas');
  canvas.width=viewport.width;canvas.height=viewport.height;
  wrapper.appendChild(canvas);
  await pg.render({canvasContext:canvas.getContext('2d'),viewport}).promise;
  if(hlTokens.length && p===showPage){
    const tc=await pg.getTextContent();
    const textLayer=document.createElement('div');textLayer.className='text-layer';
    wrapper.appendChild(textLayer);
    for(const item of tc.items){
      if(!item.str)continue;
      const it=norm(item.str);
      if(hlTokens.some(t=>t&&it.includes(t))){
        const [a,b,c,d,tx,ty]=item.transform;
        const fontSize=Math.sqrt(a*a+b*b);
        const x=tx*scale;
        const y=viewport.height-(ty*scale);
        const w=(item.width||fontSize*item.str.length*0.6)*scale;
        const h=fontSize*scale;
        const hl=document.createElement('div');hl.className='highlight-overlay';
        hl.style.left=x+'px';hl.style.top=(y-h)+'px';
        hl.style.width=w+'px';hl.style.height=(h*1.2)+'px';
        textLayer.appendChild(hl);
      }
    }
  }
}
const target=document.getElementById('page-'+showPage);
if(target)target.scrollIntoView({behavior:'auto',block:'start'});
</script></body></html>"""


@app.route("/doc/<doc_id>")
def doc_view(doc_id):
    """OCR viewer — renders the bundled paperclip OCR for a doc with the cited line
    highlighted. Uses the exact line number (no page interpolation / PDF search)."""
    target = request.args.get("line", type=int)
    q = request.args.get("q", "")
    qn = re.sub(r"\s+", " ", q).strip().lower()[:60] if q else ""

    def _parse(raw):
        out = []
        for m in re.finditer(r"^L(\d+):\s?(.*?)(?=^L\d+:|\Z)", raw, re.MULTILINE | re.DOTALL):
            out.append((int(m.group(1)), m.group(2).rstrip("\n")))
        return out

    def _find(rows_):
        if not qn:
            return None
        matches = [n for n, t in rows_ if qn in re.sub(r"\s+", " ", t).lower()]
        if not matches:
            return None
        # When the quote appears on multiple lines, anchor to the occurrence NEAREST
        # the cited line (we have the exact line) rather than the first match — this
        # prevents landing on a stray earlier occurrence.
        if target is not None:
            return min(matches, key=lambda n: abs(n - target))
        return matches[0]

    # 1) bundled OCR (matches the cards_recited citations' line numbers)
    parsed = None
    for c in (doc_id, canon_docid(doc_id), "fda_" + doc_id):
        f = DOCS_DIR / f"{c}.txt"
        if f.exists():
            parsed = _parse(f.read_text(errors="replace")); break
    anchored = False
    if parsed is not None:
        hit = _find(parsed)
        if hit is not None:
            target = hit; anchored = True
    # 2) not bundled (new single-agent cards), or the quote isn't in the bundle:
    #    fetch that doc from paperclip and render it, anchoring by quote if found.
    #    Adopt the paperclip OCR even when the quote can't be located, so the doc
    #    still opens (at the cited line) rather than 404-ing.
    if parsed is None or (q and not anchored):
        raw = _paperclip_ocr(doc_id)
        if raw:
            pp = _parse(raw)
            if parsed is None:
                parsed = pp
            hit = _find(pp)
            if hit is not None:
                parsed = pp; target = hit; anchored = True
    if parsed is None:
        return ("Document OCR not available for this source.", 404)
    rows = []
    for n, t in parsed:
        cls = "ln hl" if (target is not None and n == target) else "ln"
        rows.append('<div class="' + cls + '" id="L' + str(n) + '"><div class="n" data-n="' + str(n) + '" title="Copy a citation for this line" onclick="cp(this)">L' + str(n) + '</div><div class="t">' + H(t) + '</div></div>')
    body = "".join(rows)
    jump = ('<script>var t=document.getElementById("L' + str(target) + '"); if(t) t.scrollIntoView({block:"center"});</script>') if target is not None else ""
    # Click a line number -> copy {"quote","doc_id","line"} for a card edit / issue.
    copy_js = ('<script>var DOC=' + json.dumps(doc_id) + ';function cp(el){var q=el.nextElementSibling.textContent.replace(/\\s+/g," ").trim();'
               'var j=JSON.stringify({quote:q,doc_id:DOC,line:+el.dataset.n});'
               'navigator.clipboard.writeText(j).then(function(){el.classList.add("ok");setTimeout(function(){el.classList.remove("ok")},1200)});}</script>')
    _doclabel = _doc_panel_label(doc_id) or "FDA review OCR"
    hdr = (H(_doclabel) + (" · line " + str(target) if target is not None else "") + ("" if (anchored or not q) else " · (line approx)")
           + ' <span class="tip">· click a line number to copy a citation (trim the quote to the words you need)</span>')
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>' + H(doc_id[:8]) + ' OCR</title>'
        '<style>'
        'body{font-family:ui-monospace,Menlo,monospace;margin:0;padding:12px;color:#222;font-size:13px;line-height:1.5;background:#fff}'
        '.hdr{position:sticky;top:0;background:#fff;border-bottom:1px solid #ccc;padding:6px 0;margin-bottom:8px;font-family:sans-serif;font-size:12px;color:#555}'
        '.ln{display:flex;gap:10px;padding:1px 0}'
        '.n{color:#aaa;min-width:44px;text-align:right;user-select:none;cursor:pointer}'
        '.n:hover{color:#2563eb}.n.ok{color:#16a34a;font-weight:bold}.tip{color:#999}'
        '.t{white-space:pre-wrap;flex:1}'
        '.hl{background:#fff3a8;border-left:3px solid #e6a800;padding-left:6px;margin-left:-9px}'
        '</style></head><body><div class="hdr">' + hdr + '</div>' + body + jump + copy_js + '</body></html>'
    )


# --- Citation resolver endpoint (frontend calls this when rendering chat replies) ---
@app.route("/api/resolve_citations", methods=["POST"])
def resolve_citations():
    body = request.get_json(force=True)
    items = body.get("citations", [])
    out = []
    for c in items:
        info = citation_to_pdf(c.get("doc"), c.get("line"))
        out.append(info)
    return jsonify({"resolved": out})


# --- Tool definitions for Sonnet ---
def make_tools_for(app_key: str):
    return [
        {
            "name": "list_docs",
            "description": (
                "List the FDA review documents available for this drug. Returns "
                "an array of {document_id, type, public_url}. Use these document_ids "
                "with read_doc to fetch full content."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_doc",
            "description": (
                "Read the complete OCR'd text of one FDA review document. Returns "
                "the full content (may be tens of thousands of tokens). Use only "
                "document_ids returned by list_docs."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "UUID-shaped document_id from list_docs",
                    }
                },
                "required": ["document_id"],
            },
        },
    ]

def handle_tool_call(app_key: str, name: str, inp: dict) -> str:
    docs = DRUG_DOCS.get(app_key, [])
    if name == "list_docs":
        # Augment each entry with a SHORT id Sonnet should use in citations.
        # Shape stays compatible with prior callers; adds doc_short_id field.
        listed = []
        for d in docs:
            did = d["document_id"]
            listed.append({
                **d,
                "type": d.get("type") or _doc_label(did),
                "doc_short_id": doc_short_id(did),
                "total_pages": PAGES_META.get(did, {}).get("total_pages"),
                "total_lines": PAGES_META.get(did, {}).get("total_lines"),
            })
        return json.dumps(listed, indent=2)
    if name == "read_doc":
        did = inp.get("document_id", "")
        allowed_ids = {d["document_id"] for d in docs}
        if did not in allowed_ids:
            return json.dumps({"error": f"document_id {did} not in this drug's allowed list", "allowed": list(allowed_ids)})
        try:
            text = fetch_doc_text(did)
            meta = PAGES_META.get(did, {})
            header = (
                f"# CITATION INFO for this document\n"
                f"# doc_short_id = {doc_short_id(did)}\n"
                f"# total_lines = {meta.get('total_lines')}\n"
                f"# total_pages = {meta.get('total_pages')}\n"
                f"# When you quote ANY text or number from below, you MUST append\n"
                f"#   [doc={doc_short_id(did)} line=<the L-number where it appears>]\n"
                f"# Every line in the body below starts with L<n>: — use that exact number.\n\n"
            )
            return header + text
        except Exception as e:
            return json.dumps({"error": f"failed to read {did}: {e}"})
    return json.dumps({"error": f"unknown tool {name}"})

# --- Chat endpoint ---
# --- Daily $ budget tracking (resets at UTC midnight) ---
import datetime as _dt
_BUDGET_STATE = {"date": "", "spent_usd": 0.0}
_BUDGET_LOCK = threading.Lock()

def budget_check_and_reserve(est_cost_usd: float = 0.05):
    """Atomically check + reserve budget. Returns (ok, msg)."""
    with _BUDGET_LOCK:
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        if _BUDGET_STATE["date"] != today:
            _BUDGET_STATE["date"] = today
            _BUDGET_STATE["spent_usd"] = 0.0
        if _BUDGET_STATE["spent_usd"] + est_cost_usd > DAILY_USD_BUDGET:
            return False, (
                f"Daily Anthropic API budget of ${DAILY_USD_BUDGET:.0f} reached "
                f"(${_BUDGET_STATE['spent_usd']:.2f} used). Resets at UTC midnight."
            )
        _BUDGET_STATE["spent_usd"] += est_cost_usd
        return True, ""

def budget_settle(reserved: float, actual: float):
    """Reconcile reservation with actual cost after the call completes."""
    with _BUDGET_LOCK:
        _BUDGET_STATE["spent_usd"] += (actual - reserved)

def budget_status():
    with _BUDGET_LOCK:
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        spent = _BUDGET_STATE["spent_usd"] if _BUDGET_STATE["date"] == today else 0.0
        return {"daily_cap_usd": DAILY_USD_BUDGET, "spent_usd": round(spent, 4)}


SYSTEM_PROMPT = (
    "You are an FDA-review assistant embedded in a drug card. The user is "
    "asking about ONE specific drug approval. You have two tools to access "
    "the OCR'd FDA review documents for this drug: list_docs() and "
    "read_doc(document_id). Call list_docs first to see what's available. "
    "Read documents on demand to answer questions. Be concise.\n\n"
    "Style: never use emojis. No 'Hi there!', no 'Great question!', no "
    "preamble or sign-off. Skip pleasantries — answer directly. Match the "
    "tone of an FDA reviewer writing a memo, not a chatbot.\n\n"
    "=== CITATIONS ARE REQUIRED ===\n"
    "Every factual claim, every quoted phrase, and every number MUST be "
    "followed by a citation tag of the exact form:\n"
    "    [doc=<doc_short_id> line=<line_number>]\n"
    "where <doc_short_id> is the 8-char id you got from list_docs and "
    "<line_number> is the L-prefix on the line where the quote/number "
    "actually appears in read_doc output (each line starts with L<n>:).\n\n"
    "Examples:\n"
    "  The primary endpoint was \"clinical response at the test-of-cure visit\" "
    "[doc=c4c27fae line=412].\n"
    "  Treatment difference was 0.2% (95% CI -2.6% to 2.9%) [doc=c4c27fae line=435].\n\n"
    "RULES:\n"
    "1. ALWAYS include the citation tag immediately after the claim/quote/number.\n"
    "2. If you cannot find the exact line in any document, say "
    "\"I could not locate that in the review documents\" — do NOT guess "
    "and do NOT cite a line you didn't actually read.\n"
    "3. Prefer verbatim quotes over paraphrase. Always quote the exact text "
    "you can point at.\n"
    "4. The line number must be the line in read_doc output where the "
    "evidence appears — if a claim spans multiple lines, cite the first.\n"
    "5. No citation tag = no claim. If you have nothing to cite, say so.\n"
)

@app.route("/api/chat", methods=["POST"])
def chat():
    if not CHAT_ENABLED:
        return jsonify({"reply": "Chat is disabled on this deployment.", "disabled": True}), 403
    body = request.get_json(force=True)
    session_id = body.get("session_id") or str(uuid.uuid4())
    app_key = body["app_key"]
    user_msg = body["message"]

    if client is None:
        return jsonify({"reply": "Chat is unavailable on this deployment (no API key configured).",
                        "session_id": session_id, "budget": budget_status(),
                        "cost_usd": 0.0, "tool_calls": []}), 503

    if app_key not in DRUG_DOCS:
        return jsonify({"error": f"unknown app_key {app_key}"}), 400

    # Hard budget gate — reject before spending anything if cap is hit
    ok, msg = budget_check_and_reserve(est_cost_usd=0.05)
    if not ok:
        return jsonify({"reply": f"⚠️ {msg}", "session_id": session_id,
                        "budget": budget_status(), "cost_usd": 0.0, "tool_calls": []}), 429

    sessions = SESSIONS.setdefault(session_id, {})
    history = sessions.setdefault(app_key, [])
    history.append({"role": "user", "content": user_msg})

    tools = make_tools_for(app_key)
    final_text = ""
    cost = 0.0
    tool_call_log = []
    safety = 0
    while True:
        safety += 1
        if safety > 20:
            final_text = (final_text or "") + "\n\n[tool-call loop guard: stopping]"
            break
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=history,
        )
        # accumulate cost (input + output * pricing)
        cost += (
            resp.usage.input_tokens / 1e6 * 3
            + resp.usage.output_tokens / 1e6 * 15
        )
        # Add assistant message to history with original content blocks
        history.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

        if resp.stop_reason != "tool_use":
            # Extract text blocks
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    final_text += b.text
            break

        # Process tool_use blocks; produce tool_result blocks
        tool_results = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                result = handle_tool_call(app_key, b.name, b.input or {})
                tool_call_log.append({"name": b.name, "input": b.input, "result_chars": len(result)})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": result,
                })
        history.append({"role": "user", "content": tool_results})

    # Reconcile reserved budget with actual cost
    budget_settle(reserved=0.05, actual=cost)
    return jsonify({
        "session_id": session_id,
        "reply": final_text,
        "cost_usd": round(cost, 4),
        "tool_calls": tool_call_log,
        "budget": budget_status(),
    })

# --- HTML rendering ---
import html as html_lib
def H(s):
    return html_lib.escape(str(s)) if s is not None else ""


# --- Audit chip annotation ---
# Wraps verified claims in clickable .qlink chips pointing at /pdfs/<file>?page=<N>&search=<text>
# Tier ordering: EXACT < NORMALIZED < BARE_WITH_CONTEXT (verified) < UNVERIFIED
_VERIFIED = {"EXACT", "NORMALIZED", "BARE_WITH_CONTEXT"}

def _chip(text_html: str, match: dict, search_text: str) -> str:
    """Build a single .qlink span from a match dict {tier, doc_id, line, page, ...}."""
    doc_id = match.get("doc_id")
    if not doc_id: return text_html
    info = PAGES_META.get(doc_id, {})
    fname = info.get("filename") or (info.get("public_url", "") or "").rsplit("/", 1)[-1]
    if not fname: return text_html
    page = match.get("page", 1)
    tier = match.get("tier", "")
    tier_class = tier.lower().replace("_", "-")
    # data-pdf is a single-quoted attribute holding JSON; escape any single quotes in search text
    payload = json.dumps({"url": f"pdfs/{fname}", "page": page, "s": search_text}, ensure_ascii=False)
    payload_attr = html_lib.escape(payload, quote=True)
    title = f"Source: {fname} p.{page} ({tier})"
    return f'<span class="qlink tier-{tier_class}" data-pdf="{payload_attr}" title="{H(title)}">{text_html}</span>'


# ---- reference-style rendering: superscript numbered refs + per-card source list ----
_REF_CTX = threading.local()
def _card_refs():
    if not hasattr(_REF_CTX, "refs"):
        _REF_CTX.refs = []
    return _REF_CTX.refs

_NUM_RE = re.compile(
    r"p\s*[<>=]\s*0?\.\d+|\bn\s*=\s*\d{2,}\b|\d+\.\d+\s*%|\d+\s*%|\d+/\d+|-?\d+\.\d{1,4}|\d{2,3}(?:,\d{3})+|\d{3,}",
    re.I)
# Confidence-interval levels are boilerplate ("95% CI"), not data claims to cite.
_CI_LEVELS = {"95%", "97.5%", "90%", "99%", "2.5%"}

def _ref_label(fname):
    try:
        return _doc_display(fname) or fname
    except Exception:
        return fname

def _ref_marker(match, search_text):
    """Register a citation on the current card and return a clickable superscript
    number. data-pdf carries BOTH the OCR anchor (doc_id+line, exact) and the PDF
    anchor (url+page+search, approximate); the click handler opens whichever the
    source toggle selects."""
    doc_id = canon_docid(match.get("doc_id"))
    line = match.get("line")
    if not doc_id:
        return ""
    info = PAGES_META.get(doc_id, {}) or PAGES_META.get("fda_" + doc_id, {})
    fname = info.get("filename") or (info.get("public_url", "") or "").rsplit("/", 1)[-1]
    page = match.get("page") or 1
    label = _doc_panel_label(doc_id)
    refs = _card_refs()
    n = None
    for i, rr in enumerate(refs):
        if rr.get("doc_id") == doc_id and rr.get("line") == line:
            n = i + 1
            break
    if n is None:
        refs.append({"doc_id": doc_id, "line": line, "fname": fname, "page": page,
                     "label": label, "search": search_text})
        n = len(refs)
    payload = json.dumps({"doc_id": doc_id, "line": line,
                          "url": (f"pdfs/{fname}" if fname else ""),
                          "page": page, "s": search_text, "label": label}, ensure_ascii=False)
    pa = html_lib.escape(payload, quote=True)
    return (f'<sup class="refmark"><a class="qlink" data-pdf="{pa}" '
            f'title="Source {n}: {H(label)} · line {line}">{n}</a></sup>')

def _unverified_marker():
    return ('<sup class="unvmark" title="No source found in the review documents '
            'for this figure">unverified</sup>')

def _md_inline(s):
    """HTML-escape, then render inline markdown bold (**...**) that the FDA OCR text
    carries, so the card doesn't show literal asterisks."""
    s = H(s or "")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s

def _render_field_referenced(text, cite_spans, flag_uncited=True):
    """Render `text` plain, a superscript ref marker after each cited span, and an
    'unverified' flag after each number not covered by a citation."""
    text = text or ""
    if not text:
        return ""
    covered = [(s, e) for s, e, _, _ in cite_spans]
    def is_cov(a, b):
        return any(s <= a and b <= e for s, e in covered)
    inserts = {}
    for s, e, m, q in cite_spans:
        mk = _ref_marker(m, q)
        if mk:
            inserts.setdefault(e, []).append(mk)
    if flag_uncited:
        for mo in _NUM_RE.finditer(text):
            s0, e0 = mo.start(), mo.end()
            tok = mo.group(0).strip()
            if tok in _CI_LEVELS:
                continue  # CI-level boilerplate (usually "95% CI") — not a data claim
            if (s0 > 0 and text[s0 - 1].isalpha()) or (e0 < len(text) and text[e0].isalpha()):
                continue  # digits embedded in an identifier (T790M, L858R, gp100, v1.1, EFC6019)
            if not is_cov(s0, e0):
                inserts.setdefault(e0, []).append(_unverified_marker())
    if not inserts:
        return _md_inline(text)
    out, pos = [], 0
    for p in sorted(inserts):
        out.append(_md_inline(text[pos:p]))
        out.extend(inserts[p])
        pos = p
    out.append(_md_inline(text[pos:]))
    return "".join(out)

def _source_list_html(refs):
    items = []
    for rr in refs:
        payload = json.dumps({"doc_id": rr.get("doc_id"), "line": rr.get("line"),
                              "url": (f"pdfs/{rr['fname']}" if rr.get("fname") else ""),
                              "page": rr.get("page"), "s": rr.get("search", ""),
                              "label": rr.get("label")}, ensure_ascii=False)
        pa = html_lib.escape(payload, quote=True)
        items.append(f'<li><a class="qlink" data-pdf="{pa}">{H(rr["label"])} · line {rr.get("line")}</a></li>')
    return f'<div class="section-label">SOURCES ({len(refs)})</div><ol class="refs">{"".join(items)}</ol>'


def render_cited(field) -> str | None:
    """Render a cited field {text, citations[{quote, doc_id, line}]}: place an inline
    superscript reference marker at the point in the field text matched by each
    citation (its quote, or the longest distinctive token). Landing is handled by
    the citation's doc_id+line (exact), so placement need not be perfect. Returns
    None if `field` is not a cited dict."""
    if not (isinstance(field, dict) and "text" in field and "citations" in field):
        return None
    text = field["text"] or ""
    cites = field["citations"] or []
    if not text: return ""
    if not cites: return H(text)
    text_lc = text.lower()
    spans, placed = [], set()
    def _cm(c):
        return {"doc_id": c.get("doc_id"), "line": c.get("line"),
                "page": line_to_page(c.get("doc_id", ""), c.get("line", 1))}
    # (a) whole-quote match — prose-style citations (indication, MOA, etc.)
    for c in cites:
        if not c.get("doc_id"):
            continue
        q = (c.get("quote") or "").strip()
        if not q:
            continue
        idx = text_lc.find(q.lower())
        if idx >= 0:
            spans.append((idx, idx + len(q), _cm(c), q))
            placed.add((c.get("doc_id"), c.get("line")))
    # (b) per-number attribution — mark EACH standalone number in the prose with the
    #     citation whose quote contains that same number. This handles table-format
    #     quotes (e.g. an AE table "ARIA-E\t24\t2") that can't whole-match the prose
    #     "24% vs 2%", so every rate now gets its own clickable superscript.
    for mo in re.finditer(r"\d+(?:\.\d+)?", text):
        n = mo.group(0)
        s0, e0 = mo.start(), mo.end()
        if (s0 > 0 and text[s0 - 1].isalpha()) or (e0 < len(text) and text[e0].isalpha()):
            continue  # digit embedded in an identifier
        for c in cites:
            if not c.get("doc_id"):
                continue
            if re.search(r"(?<!\d)" + re.escape(n) + r"(?!\d)", c.get("quote") or ""):
                # Search text = the citation's FULL quote (not the bare number), so
                # the doc viewer anchors on the source row, not the first stray "25".
                spans.append((s0, e0, _cm(c), c.get("quote") or n))
                placed.add((c.get("doc_id"), c.get("line")))
                break
    spans.sort(key=lambda x: (x[0], -x[1]))
    kept, last_end = [], 0
    for s, e, m, q in spans:
        if s < last_end: continue
        kept.append((s, e, m, q)); last_end = e
    out = _render_field_referenced(text, kept, flag_uncited=False)
    # Trailing markers for any citation that couldn't be placed inline — so NO
    # citation is silently dropped.
    seen = set(placed)
    for c in cites:
        key = (c.get("doc_id"), c.get("line"))
        if not c.get("doc_id") or key in seen:
            continue
        seen.add(key)
        m = {"doc_id": c.get("doc_id"), "line": c.get("line"),
             "page": line_to_page(c.get("doc_id", ""), c.get("line", 1))}
        mk = _ref_marker(m, re.sub(r"\s+", " ", (c.get("quote") or "")).strip())
        if mk:
            out += mk
    return out


def line_to_page(doc_id: str, line_n) -> int:
    info = PAGES_META.get(doc_id, {})
    tp, tl = info.get("total_pages"), info.get("total_lines")
    if not tp or not tl or tl <= 0: return 1
    try: line_i = int(line_n)
    except (ValueError, TypeError): line_i = 1
    return max(1, min(int(tp), round(line_i * int(tp) / int(tl))))


def annotate(app_key: str, field_path: str, value) -> str:
    """Return HTML for `value` annotated with audit chips. Strategy:
    - If the whole-field phrase verified -> wrap the entire value in one chip.
    - Otherwise -> wrap each verified NUMBER occurrence in-place as its own chip.
    - Anything not verified renders as plain escaped text.
    """
    if value is None or value == "": return ""
    s = str(value)
    entry = (CLAIM_AUDIT.get(app_key) or {}).get("fields", {}).get(field_path)
    if not entry:
        return _md_inline(s)  # unaudited / external-metadata field -> markdown, no flags

    cite_spans = []
    flag_uncited = False
    # Best whole-phrase match (EXACT/NORMALIZED) covers the whole field -> sourced.
    phrase_match = None
    for _phrase, m in (entry.get("phrases") or {}).items():
        if m.get("tier") in _VERIFIED and m.get("doc_id"):
            phrase_match = m
            break
    if phrase_match:
        pm = dict(phrase_match)
        pm.setdefault("page", line_to_page(pm.get("doc_id", ""), pm.get("line", 1)))
        cite_spans.append((0, len(s), pm, s[:80]))
    else:
        # Number-level audit: cite the verified numbers, flag ones that weren't.
        flag_uncited = True
        used = set()
        for num_text, m in (entry.get("numbers") or {}).items():
            if m.get("tier") not in _VERIFIED or not m.get("doc_id") or num_text in used:
                continue
            idx = s.find(num_text)
            if idx < 0:
                continue
            used.add(num_text)
            pm = dict(m)
            pm.setdefault("page", line_to_page(pm.get("doc_id", ""), pm.get("line", 1)))
            cite_spans.append((idx, idx + len(num_text), pm, num_text))
    return _render_field_referenced(s, cite_spans, flag_uncited=flag_uncited)


def pill(label, active, color="#0a7d2f"):
    bg = color if active else "#e5e7eb"
    fg = "#fff" if active else "#9ca3af"
    return f'<span class="pill" style="background:{bg};color:{fg}">{label}</span>'

def render_yes_detail(headline, detail_text):
    """Wrap a headline with an expandable detail block when detail_text is present."""
    if not detail_text:
        return headline
    return (
        f'<details class="yes-detail"><summary>{headline}</summary>'
        f'<div class="yes-detail-body">{H(detail_text)}</div></details>'
    )

def _cite_of(q):
    """Superscript ref marker for a verified quote dict {doc_type,doc_id,line,quote}."""
    if not (isinstance(q, dict) and q.get("doc_id")):
        return ""
    m = {"doc_id": q.get("doc_id"), "line": q.get("line"),
         "page": line_to_page(canon_docid(q.get("doc_id", "")), q.get("line", 1))}
    return _ref_marker(m, re.sub(r"\s+", " ", (q.get("quote") or "")).strip())

def _cited_field(obj):
    """text + trailing citation for a {text, quote} object."""
    if not isinstance(obj, dict):
        return H(str(obj or ""))
    return H(obj.get("text") or "") + _cite_of(obj.get("quote"))

def _render_adcom_verified(v):
    """Verified AdComm (from run_adcomm.py) — committee/date/vote + concern->change
    linkages, each with a citation superscript that opens the source OCR line."""
    rec = v.get("recommendation") or {}
    head = "Yes" + (" — " + _cited_field(rec) if rec.get("text") else "")
    chunks = []
    meet = []
    for k in ("committee_name", "meeting_date"):
        o = v.get(k) or {}
        if o.get("text"):
            meet.append(_cited_field(o))
    if meet:
        chunks.append("<div class='adcom-meet'>Meeting: " + " · ".join(meet) + "</div>")
    if v.get("summary"):
        chunks.append("<div class='adcom-rec'><b>Summary:</b> " + H(v["summary"]) + "</div>")
    links = v.get("linkages") or []
    if links:
        chunks.append(f"<details class='adcom-linkages'><summary>{len(links)} verified change{'s' if len(links)!=1 else ''} traced to AC input (click to expand)</summary>")
        for lk in links:
            basis = lk.get("basis", "")
            bcls = "stated" if basis == "stated_in_review" else "inferred"
            chunks.append(
                "<div class='adcom-pair'>"
                f"<div class='adcom-cat'>{H(lk.get('category',''))} <span class='adcom-basis {bcls}'>{H(basis or 'inferred')}</span></div>"
                f"<div class='adcom-concern'><b>AC raised:</b> {H(lk.get('concern',''))}{_cite_of(lk.get('concern_quote'))}</div>"
                f"<div class='adcom-change'><b>Resulting change:</b> {H(lk.get('change',''))}{_cite_of(lk.get('change_quote'))}</div>"
                "</div>"
            )
        chunks.append("</details>")
    body = "<div class='adcom-detail'>" + "".join(chunks) + "</div>" if chunks else ""
    return head + body

def render_adcom(advcmte, detail, verified=None):
    """Compose AdCom display. Prefers the VERIFIED re-extraction (with citations);
    falls back to the external unverified detail."""
    if verified and verified.get("adcom_held"):
        return _render_adcom_verified(verified)
    if not (advcmte or {}).get('convened'): return 'No'
    parts = ['Yes']
    vote = (advcmte or {}).get('vote')
    if vote: parts.append('— ' + H(vote))
    if not detail: return ' '.join(parts)
    # Detail from AdComm Explorer dataset
    chunks = []
    meet_bits = []
    if detail.get('committee_name'): meet_bits.append(H(detail['committee_name']))
    if detail.get('meeting_date'): meet_bits.append(H(detail['meeting_date']))
    if meet_bits:
        chunks.append(f"<div class='adcom-meet'>Meeting: {' · '.join(meet_bits)}</div>")
    if detail.get('overall_recommendation'):
        chunks.append(f"<div class='adcom-rec'><b>Recommendation:</b> {H(detail['overall_recommendation'])}</div>")
    if detail.get('discussion_summary'):
        chunks.append(f"<div class='adcom-rec'><b>Discussion:</b> {H(detail['discussion_summary'])}</div>")
    links = detail.get('linkages') or []
    if links:
        chunks.append(f"<details class='adcom-linkages'><summary>{len(links)} change{'s' if len(links)!=1 else ''} traced to AC input (click to expand)</summary>")
        for lk in links:
            chunks.append(
                "<div class='adcom-pair'>"
                f"<div class='adcom-concern'><b>AC raised:</b> {H(lk.get('concern',''))}</div>"
                f"<div class='adcom-change'><b>Resulting change:</b> {H(lk.get('change',''))}</div>"
                f"<div class='adcom-cat'>{H(lk.get('category',''))}</div>"
                + (f"<div class='adcom-evidence'><i>{H(lk.get('evidence',''))[:600]}</i></div>" if lk.get('evidence') else '')
                + "</div>"
            )
        chunks.append("</details>")
    head = ' '.join(parts)
    body = "<div class='adcom-detail'>" + "".join(chunks) + "</div>" if chunks else ''
    return head + body

def render_approval_program(ap):
    """Render the referenced approval-program section: each granted program with
    its cited detail (surrogate endpoint / confirmatory trial / basis), + a cited
    pathway summary. Each detail is a {text, citations} leaf rendered via render_cited
    so the superscripts open the exact source line."""
    if not isinstance(ap, dict) or not ap:
        return ""
    specs = [
        ("accelerated_approval", "Accelerated Approval",
         [("surrogate_endpoint", "Surrogate endpoint"), ("confirmatory_requirement", "Confirmatory trial required")]),
        ("breakthrough_therapy", "Breakthrough Therapy", [("basis", "Basis")]),
        ("fast_track", "Fast Track", [("basis", "Basis")]),
        ("orphan_drug", "Orphan Drug", [("orphan_indication", "Orphan indication")]),
        ("priority_review", "Priority Review", [("basis", "Basis")]),
        ("qidp", "QIDP", [("basis", "Basis")]),
        ("rmat", "RMAT", [("basis", "Basis")]),
    ]
    rows = []
    for key, label, detailkeys in specs:
        v = ap.get(key)
        if not (isinstance(v, dict) and v.get("present")):
            continue
        det_html = ""
        for dk, dlabel in detailkeys:
            dv = v.get(dk)
            if isinstance(dv, dict) and dv.get("text"):
                det_html += f'<div class="ap-detail"><span class="ap-k">{H(dlabel)}</span> <span class="ap-v">{render_cited(dv)}</span></div>'
        rows.append(f'<div class="ap-prog"><span class="ap-badge">{H(label)}</span>{det_html}</div>')
    summ = ap.get("summary")
    summ_html = ""
    if isinstance(summ, dict) and summ.get("text"):
        summ_html = f'<div class="ap-summary">{render_cited(summ)}</div>'
    if not rows and not summ_html:
        return '<div class="ap-none">No expedited-program designations stated in the review.</div>'
    return f'<div class="approval-program">{"".join(rows)}{summ_html}</div>'


def render_cited_single(field):
    """Like render_cited but appends ONE trailing citation marker per distinct
    citation instead of marking every number. For single-statement fields (PMR/PMC
    requirements, supplement descriptions) that often begin with an FDA identifier
    number ('4384-3: ...') where per-number superscripts are noisy and wrong."""
    if not (isinstance(field, dict) and "text" in field and "citations" in field):
        return None
    text = field.get("text") or ""
    cites = field.get("citations") or []
    if not text:
        return ""
    out = _md_inline(text)
    seen, marks = set(), ""
    for c in cites:
        if not c.get("doc_id"):
            continue
        key = (c.get("doc_id"), c.get("line"))
        if key in seen:
            continue
        seen.add(key)
        cm = {"doc_id": c.get("doc_id"), "line": c.get("line"),
              "page": line_to_page(c.get("doc_id", ""), c.get("line", 1))}
        marks += _ref_marker(cm, c.get("quote") or "")
    return out + marks


def render_post_marketing(pm):
    """Render the PMR/PMC list (post-marketing requirements & commitments) from the
    approval letter, each requirement cited via render_cited."""
    if pm is None:
        return ('<div class="pm-none">Post-marketing requirements not yet extracted for this card. '
                'The approval letter was not parsed for PMRs/PMCs — this is a coverage gap, '
                '<b>not</b> a statement that none exist.</div>')
    if not pm:
        return ('<div class="pm-none">None — the approval letter for this application states no '
                'post-marketing requirements or commitments.</div>')
    def _items(lst):
        out = ""
        for x in lst:
            req = x.get("requirement")
            out += f'<li>{render_cited_single(req) if isinstance(req, dict) else H(str(req))}</li>'
        return out
    pmrs = [x for x in pm if (x.get("type") or "").upper() == "PMR"]
    pmcs = [x for x in pm if (x.get("type") or "").upper() == "PMC"]
    parts = ""
    if pmrs:
        parts += f'<div class="pm-group"><span class="pm-badge pmr">PMR · required ({len(pmrs)})</span><ul class="pm-list">{_items(pmrs)}</ul></div>'
    if pmcs:
        parts += f'<div class="pm-group"><span class="pm-badge pmc">PMC · committed ({len(pmcs)})</span><ul class="pm-list">{_items(pmcs)}</ul></div>'
    return f'<div class="post-marketing">{parts}</div>' if parts else '<div class="pm-none">None stated in the approval letter.</div>'


def render_crl(crl):
    """One-line prior-CRL history: Yes (date) — basis, or No."""
    if not isinstance(crl, dict) or crl.get("prior_crl") is None:
        return "—"
    if not crl.get("prior_crl"):
        return "No prior Complete Response Letter"
    date = crl.get("date"); basis = crl.get("basis")
    dtxt = (date.get("text") if isinstance(date, dict) else date) or ""
    head = "Prior CRL" + (f" · {H(dtxt)}" if dtxt else "")
    body = render_cited(basis) if isinstance(basis, dict) and basis.get("text") else ""
    return render_yes_detail(head, None) if not body else (
        f'<details class="yes-detail"><summary>{head}</summary><div class="yes-detail-body">{body}</div></details>')


_DOC_LABEL_CACHE = {}
def _doc_label(doc_id):
    """Doc-type label from the precomputed cache (filled once at startup); falls
    back to the short id. NEVER queries paperclip at render time."""
    return _DOC_LABEL_CACHE.get(doc_id, doc_id[:8] + "…")

def _precompute_doc_labels():
    """Populate _DOC_LABEL_CACHE {doc_id -> doc-type label}. Prefers the bundled
    data/doc_labels.json (shipped in the repo; no paperclip needed on HF); falls
    back to a batched paperclip lookup when the bundle is absent (local dev)."""
    bundle = DATA_DIR / "doc_labels.json"
    if bundle.exists():
        try:
            _DOC_LABEL_CACHE.update(json.load(open(bundle)))
            print(f"[startup] loaded {len(_DOC_LABEL_CACHE)} doc labels from bundle", flush=True)
            return
        except Exception as e:
            print(f"[startup] doc_labels.json load failed ({e}); falling back to paperclip", flush=True)
    ids = set()
    def _grab(o):
        if isinstance(o, dict):
            for c in o.get("citations", []) or []:
                if c.get("doc_id"):
                    ids.add(c["doc_id"])
            for v in o.values():
                _grab(v)
        elif isinstance(o, list):
            for v in o:
                _grab(v)
    for _c in CITED_CARDS.values():
        _grab(_c)
    for _c in CARDS:
        _grab(_c)
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 80):
        inlist = "','".join(ids[i:i + 80])
        try:
            out = subprocess.run([PAPERCLIP, "sql", "--source", "fda",
                f"SELECT document_id, SUBSTRING(public_url FROM 45 FOR 48) AS u FROM documents WHERE document_id IN ('{inlist}')"],
                capture_output=True, text=True, timeout=60).stdout
            for ln in out.split("\n"):
                parts = [p.strip() for p in ln.split("|")]
                if len(parts) == 2 and re.match(r"^[0-9a-f-]{30,}$", parts[0]):
                    _DOC_LABEL_CACHE[parts[0]] = (_doc_display(parts[1]) if ".pdf" in parts[1].lower() else parts[0][:8] + "…")
        except Exception:
            pass
    print(f"[startup] precomputed {len(_DOC_LABEL_CACHE)} doc labels", flush=True)

# Load at IMPORT time: gunicorn (Fly) imports app:app and never runs the __main__ block,
# so labels loaded only there left every source chip as a short id on the live site.
_precompute_doc_labels()


def _chg_sortkey(d):
    d = str(d or "")
    m = re.search(r"(\d{4})-(\d{2})", d)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{1,2})/(\d{4})", d)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    return (0, 0)


def _safety_change_rows(app_key):
    """Safety changes since approval (from data/label_changes/), as card-style rows —
    one per date, a single badge, a concise line. Matches the efficacy-supplement rows."""
    p = DATA_DIR / "label_changes" / f"{app_key}.json"
    if not p.exists():
        return []
    try:
        lc = json.load(open(p))
    except Exception:
        return []
    months = {}
    for e in lc.get("events", []):
        if e.get("category") == "SAFETY":
            months.setdefault(e["date"], []).append(e)
    seen_boxed = bool(lc.get("boxed_warning_at_approval"))
    rows = []
    for date, evts in months.items():
        sections = [e.get("section", "") for e in evts]
        boxed = any(s.lower().startswith("boxed") for s in sections)
        parts = []
        if boxed:
            parts.append("Boxed warning added" if not seen_boxed else "Boxed warning revised")
            seen_boxed = True
        if any(s.lower().startswith("contra") for s in sections):
            parts.append("contraindication updated")
        if any(("warnings and precautions" in s.lower()) or re.search(r"\(5\.", s) for s in sections):
            parts.append("warnings updated")
        if not parts:
            parts.append("safety labeling updated")
        cls = "boxed" if boxed else "safety"
        badge = "BOXED WARNING" if boxed else "SAFETY"
        desc = "; ".join(parts); desc = desc[0].upper() + desc[1:]
        # source: open the label OCR at the "RECENT MAJOR CHANGES" line for this change
        src = next((e for e in evts if e.get("section", "").lower().startswith("boxed")), evts[0])
        link = ""
        if src.get("doc_id"):
            payload = json.dumps({"doc_id": src["doc_id"], "line": src.get("line") or 1,
                                  "url": "", "page": 1, "s": ""})
            link = (f'<span class="supp-links"><a class="qlink" '
                    f'data-pdf="{html_lib.escape(payload, quote=True)}">label</a></span>')
        rows.append((_chg_sortkey(date),
                     f'<div class="supp {cls}"><div class="supp-head">'
                     f'<span class="supp-date">{H(date)}</span> '
                     f'<span class="pm-badge {cls}">{badge}</span>{link}</div>'
                     f'<div class="supp-desc">{H(desc)}</div></div>'))
    return rows


def _report_error_link(app_key, drug):
    """Per-card links to GitHub: 'Suggest an edit' opens the card's source file
    (data/cards_single/<APP>.json) in GitHub's web editor, which forks + opens a PR for
    the contributor; 'Report an error' opens a pre-filled issue. Omitted if GITHUB_REPO
    is unset."""
    if not GITHUB_REPO:
        return ""
    import urllib.parse
    title = f"Card correction: {drug} ({app_key})"
    body = (f"**Card:** `{app_key}` — {drug}\n\n"
            f"**Field / claim with the error:** \n\n"
            f"**What's wrong:** \n\n"
            f"**Correct value:** \n\n"
            f"**FDA source** — click a line number in the card's source viewer to copy a citation, "
            f"or give the verbatim quote + doc_id + line: \n")
    q = urllib.parse.urlencode({"title": title, "labels": "card-correction", "body": body})
    edit = f"https://github.com/{GITHUB_REPO}/edit/main/data/cards_single/{urllib.parse.quote(app_key)}.json"
    return (f'<span class="report-err">'
            f'<a target="_blank" rel="noopener" href="{H(edit)}">✎ Suggest an edit</a> · '
            f'<a target="_blank" rel="noopener" '
            f'href="https://github.com/{GITHUB_REPO}/issues/new?{H(q)}">⚑ Report an error</a></span>')


def _src_link(src):
    if not src or not src.get("doc_id"):
        return ""
    payload = json.dumps({"doc_id": src["doc_id"], "line": src.get("line") or 1, "url": "", "page": 1, "s": ""})
    return f'<a class="qlink" data-pdf="{html_lib.escape(payload, quote=True)}">label</a>'


def _render_changes_section(app_key):
    """Unified 'Changes since approval' from data/changes/<app>.json (Submissions.txt spine
    + paperclip content). Describable rows in full; blank labeling supplements collapsed."""
    p = DATA_DIR / "changes" / f"{app_key}.json"
    if not p.exists():
        return None
    try:
        data = json.load(open(p))
    except Exception:
        return None
    rows = []; blanks = []
    for e in data.get("entries", []):
        sk = _chg_sortkey(e.get("date")); emitted = False
        supp = e.get("efficacy_supp")
        if supp and isinstance(supp.get("description"), dict) and supp["description"].get("text"):
            links = [f'<a href="{H(supp[k])}" target="_blank">{lab}</a>'
                     for k, lab in (('review_url', 'review'), ('letter_url', 'letter'), ('label_url', 'label')) if supp.get(k)]
            rows.append((sk, f'<div class="supp"><div class="supp-head">'
                             f'<span class="supp-date">{H(e["date"])}</span> '
                             f'<span class="supp-sn">S-{H(str(supp.get("submission_number")))}</span> '
                             f'<span class="pm-badge pmr">EFFICACY</span> '
                             f'<span class="supp-links">{" · ".join(links)}</span></div>'
                             f'<div class="supp-desc">{render_cited_single(supp["description"])}</div></div>'))
            emitted = True
        elif e.get("efficacy_note"):
            rows.append((sk, f'<div class="supp"><div class="supp-head">'
                             f'<span class="supp-date">{H(e["date"])}</span> '
                             f'<span class="pm-badge pmr">EFFICACY</span> '
                             f'<span class="supp-links">{_src_link(e.get("eff_src"))}</span></div>'
                             f'<div class="supp-desc">{H(e["efficacy_note"])}</div></div>'))
            emitted = True
        saf = e.get("safety_summary")
        if saf:
            cls = "boxed" if saf.lower().startswith("boxed") else "safety"
            badge = "BOXED WARNING" if cls == "boxed" else "SAFETY"
            rows.append((sk, f'<div class="supp {cls}"><div class="supp-head">'
                             f'<span class="supp-date">{H(e["date"])}</span> '
                             f'<span class="pm-badge {cls}">{badge}</span> '
                             f'<span class="supp-links">{_src_link(e.get("safety_src"))}</span></div>'
                             f'<div class="supp-desc">{H(saf[0].upper() + saf[1:])}</div></div>'))
            emitted = True
        if e.get("rems") and not emitted:
            rows.append((sk, f'<div class="supp"><div class="supp-head">'
                             f'<span class="supp-date">{H(e["date"])}</span> '
                             f'<span class="pm-badge pmc">REMS</span></div>'
                             f'<div class="supp-desc">REMS supplement approved</div></div>'))
            emitted = True
        if not emitted:
            blanks.append(e)
    rows.sort(key=lambda x: x[0])
    out = "".join(h for _, h in rows)
    if blanks:
        # Minor labeling supplements (nothing in the label's Recent Major Changes) — one
        # plain-language line with the count + year span, not a list of S-numbers.
        appno = re.sub(r"\D", "", app_key)
        daf = ("https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
               f"?event=overview.process&ApplNo={appno}")
        n = sum(max(1, len(e.get("suppl_docs") or [])) for e in blanks)
        yrs = sorted({k[0] for k in (_chg_sortkey(e.get("date")) for e in blanks) if k[0]})
        span = (f" ({yrs[0]})" if len(yrs) == 1 else f" ({yrs[0]}–{yrs[-1]})") if yrs else ""
        what = f'{n} minor labeling update{"s" if n != 1 else ""}{span}'
        lead = ("No major label changes since approval — " if not rows else "Also ")
        out += (f'<div class="supp minor"><div class="supp-desc">{lead}{what}; '
                f"none flagged in the label's Recent Major Changes. "
                f'<a href="{H(daf)}" target="_blank" rel="noopener">Drugs@FDA history</a></div></div>')
    return out or None


def render_card_html(idx, r):
    meta = r.get("_meta", {})
    appno = meta.get("appno") or ""
    typ = meta.get("type") or ""
    app_key = f"{typ}{appno.zfill(6)}" if appno and typ else ""
    _REF_CTX.refs = []  # reset per-card reference registry (built as fields render below)
    # If we have a re-extracted cited version for this drug, use its text+citations
    # for the audited fields. Falls back to annotate() (grep-based) for drugs without.
    cited = CITED_CARDS.get(app_key)

    def cited_or_annotate(field_path, fallback_value):
        """Try cited rendering first; fall back to annotate() if no cited data for this field."""
        if cited:
            # Field paths in cited cards mirror cards.jsonl shape minus the _meta prefix
            # For pivotal_trials.0.result etc, walk into the cited dict
            parts = field_path.split(".")
            cur = cited
            for p in parts:
                if p == "_meta": continue  # cited.indication, not cited._meta.indication
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
                    cur = cur[int(p)]
                else:
                    cur = None; break
            html = render_cited(cur) if cur is not None else None
            if html is not None:
                return html
        return annotate(app_key, field_path, fallback_value)
    drug = meta.get("drug", "")
    active = meta.get("active", "")
    sponsor = meta.get("sponsor", "")
    indication = meta.get("indication", "")
    tta = meta.get("tta_months")
    exped = meta.get("expedited") or {}
    first_cycle = r.get("first_cycle_approval")
    trials = r.get("pivotal_trials") or []
    advcmte = r.get("advisory_committee") or {}
    rems = r.get("rems") or {}
    boxed = r.get("boxed_warning") or {}

    fc_tag = "Yes" if first_cycle is True else ("No" if first_cycle is False else "—")
    tta_tag = f"{tta} months" if tta else "—"

    trials_html = ""
    for i, t in enumerate(trials[:4]):
        nct = t.get("nct")
        # Only a real NCT id becomes a link; "Not stated in reviewed documents"/"null"/etc.
        # are not registry ids (NCT#s are external and often absent from FDA docs) -> omit.
        nct_num = nct.strip() if (isinstance(nct, str) and re.match(r'NCT\d{3,}', nct.strip())) else None
        nct_link = (
            f'<a href="https://clinicaltrials.gov/study/{H(nct_num)}" target="_blank">{H(nct_num)}</a>'
            if nct_num
            else ""
        )
        fp = f"pivotal_trials.{i}"
        tn_html = cited_or_annotate(f"{fp}.trial_name", t.get('trial_name') or 'Unnamed trial')
        n_val = t.get('n')
        n_html = cited_or_annotate(f"{fp}.n", str(n_val)) if n_val else ''
        # The extracted n value may already start with "N="/"n=" (new cards give a
        # descriptive value like "N=462 (drug 309, placebo 153)"). Strip it so the
        # template's "N=" prefix isn't doubled ("N=N=462").
        n_html = re.sub(r'^\s*[Nn]\s*=\s*', '', n_html)
        nbit_html = f' · N={n_html}' if n_html else ''
        ep_short_html = cited_or_annotate(f"{fp}.endpoint", (t.get('endpoint') or '')[:120])
        trials_html += f"""
        <details class="trial">
          <summary><b>{tn_html}</b>{nbit_html} · {ep_short_html}</summary>
          <div class="trial-body">
            <div class="row"><span class="k">Phase</span><span class="v"><b>{cited_or_annotate(f'{fp}.phase', t.get('phase'))}</b></span></div>
            <div class="row"><span class="k">Design</span><span class="v">{cited_or_annotate(f'{fp}.design', t.get('design'))}</span></div>
            <div class="row"><span class="k">Population</span><span class="v">{cited_or_annotate(f'{fp}.population', t.get('population'))}</span></div>
            <div class="row"><span class="k">Endpoint</span><span class="v"><b>{cited_or_annotate(f'{fp}.endpoint', t.get('endpoint'))}</b></span></div>
            <div class="row"><span class="k">Trial</span><span class="v">{tn_html} {('('+nct_link+')') if nct_link else ''}{nbit_html}</span></div>
            <div class="row"><span class="k">Result</span><span class="v"><b>{cited_or_annotate(f'{fp}.result', t.get('result'))}</b></span></div>
          </div>
        </details>"""
    if not trials_html:
        trials_html = '<div class="trial empty">no pivotal trials extracted</div>'

    # Indication details chips (parsed structure)
    ind_det = r.get('indication_details') or {}
    ind_chips_html = ""
    if isinstance(ind_det, dict) and not ind_det.get('_parse_error'):
        for key, label in [('line_of_treatment','Line'),('setting','Setting'),('population_restrictions','Population'),('combination','Combination'),('prior_therapy_required','Prior therapy')]:
            val = ind_det.get(key)
            if val and val.strip().lower() not in ('null','none','not specified','not applicable','n/a',''):
                ind_chips_html += f'<div class="ind-chip"><span class="ind-chip-k">{label}</span><span class="ind-chip-v">{annotate(app_key, f"indication_details.{key}", val)}</span></div>'

    # Supplements — EFFICACY (indication-expansion) history. None = not extracted;
    # [] = deterministically none (Drugs@FDA); list = the expansions, each with a
    # cited "what changed" description.
    supps = r.get('supplements')
    supps_html = _render_changes_section(app_key)
    _skip_legacy = supps_html is not None
    _safety = [] if _skip_legacy else _safety_change_rows(app_key)
    _eff = []
    for s_i, s in enumerate([] if _skip_legacy else (supps or [])[:25]):
        date_fmt = s.get('approval_date') or ''
        sn = s.get('submission_number')
        desc = s.get('description')
        desc_html = (render_cited_single(desc) if isinstance(desc, dict) and desc.get('text')
                     else '<i>description not extracted</i>')
        links = []
        for k, lab in (('review_url', 'review'), ('letter_url', 'letter'), ('label_url', 'label')):
            if s.get(k):
                links.append(f'<a href="{H(s[k])}" target="_blank">{lab}</a>')
        link_str = ' · '.join(links)
        _eff.append((_chg_sortkey(date_fmt),
            f'<div class="supp">'
            f'<div class="supp-head"><span class="supp-date">{H(date_fmt)}</span> '
            f'<span class="supp-sn">S-{H(str(sn))}</span> '
            f'<span class="pm-badge pmr">EFFICACY</span> '
            f'<span class="supp-links">{link_str}</span></div>'
            f'<div class="supp-desc">{desc_html}</div>'
            f'</div>'))
    if not _skip_legacy:
        if _eff or _safety:
            _all = _eff + _safety
            _all.sort(key=lambda x: x[0])
            supps_html = "".join(h for _, h in _all)
        elif supps is None:
            supps_html = ('<div class="supp empty">Post-approval changes not yet extracted '
                          'for this card — <b>not</b> a statement that none exist.</div>')
        else:
            supps_html = ('<div class="supp empty">No changes recorded after the original approval '
                          '(per Drugs@FDA / the label).</div>')

    pills = (
        pill('BTD', exped.get('btd')) +
        pill('FT', exped.get('ft')) +
        pill('PR', exped.get('pr')) +
        pill('Orph', exped.get('orph')) +
        pill('AA', exped.get('accelerated')) +
        pill('QIDP', exped.get('qidp'))
    )

    # Source documents = the docs the card actually CITES (the app's old DRUG_DOCS
    # discovery returns nothing for new-pipeline cards). Distinct doc_ids, each
    # clickable to open the OCR at a cited line.
    _seen_docs = {}
    def _collect_docs(o):
        if isinstance(o, dict):
            for c in o.get("citations", []) or []:
                did = c.get("doc_id")
                if did:
                    _seen_docs.setdefault(did, c.get("line"))
            for v in o.values():
                _collect_docs(v)
        elif isinstance(o, list):
            for v in o:
                _collect_docs(v)
    _collect_docs(CITED_CARDS.get(app_key) or {})   # clinical fields (overlay)
    _collect_docs(r)                                 # approval_program / post_marketing / crl_history citations
    docs_html = ""
    if not _seen_docs:
        docs_html = '<div class="doc-missing">No cited source documents.</div>'
    else:
        for did in _seen_docs:
            # REVIEW DOCUMENTS lists whole source docs — open at the TOP (line 1 / page 1),
            # not at any one cited line. No doc-id hash shown; the type label is enough.
            payload = json.dumps({"doc_id": did, "line": 1, "page": 1, "s": ""})
            pa = html_lib.escape(payload, quote=True)
            docs_html += (
                f'<div class="doc-row"><a class="qlink" data-pdf="{pa}">'
                f'<span class="doc-type">{H(_doc_label(did))}</span></a></div>'
            )

    card_body = f"""
    <div class="card" data-app-key="{H(app_key)}" data-drug="{H(drug)}">
      <div class="header-row">
        <span class="tab active">{H('Regular' if typ=='NDA' else 'Regular (BLA)')}</span>
        <span class="metric">First-cycle: <b>{fc_tag}</b></span>
        <span class="metric">TTA: <b>{tta_tag}</b></span>
        {_report_error_link(app_key, drug)}
      </div>
      <div class="title">{H(drug)} <span class="active-mol">({H(active)})</span></div>
      <div class="sponsor">{H(sponsor)} · {H(typ)} {H(appno)}</div>

      <div class="section-label">APPROVED INDICATION (verbatim)</div>
      <div class="indication">"{cited_or_annotate('_meta.indication', indication)}"</div>
      <div class="ind-chips-wrap">{ind_chips_html}</div>

      <div class="section-label">CHANGES SINCE APPROVAL</div>
      {supps_html}

      <div class="section-label">EXPEDITED PROGRAMS</div>
      <div class="pills">{pills}</div>
      <div class="legend">BTD · Fast Track · Priority Review · Orphan · Accelerated Approval · QIDP</div>
      {(f'<div class="section-label">APPROVAL PROGRAM (referenced)</div>' + render_approval_program(r.get('approval_program'))) if r.get('approval_program') else ''}

      <div class="section-label">PIVOTAL EVIDENCE</div>
      {trials_html}

      <div class="section-label">APPROVAL-SHAPING FACTORS</div>
      <div class="row"><span class="k">Prior CRL</span><span class="v">{render_crl(r.get('crl_history'))}</span></div>
      <div class="row"><span class="k">Companion Dx</span><span class="v">{render_yes_detail('Yes' if r.get('companion_dx') is True else 'No' if r.get('companion_dx') is False else '—', r.get('companion_dx_detail'))}</span></div>
      <div class="row"><span class="k">Advisory Cmte</span><span class="v">{render_adcom(advcmte, r.get('advisory_committee_detail'), ADCOMM_VERIFIED.get(app_key))}</span></div>
      <div class="row"><span class="k">REMS</span><span class="v">{render_yes_detail(('Yes' + (' — '+H(rems.get('type')) if rems.get('type') else '') if rems.get('present') else 'No'), rems.get('detail') if rems.get('present') else None)}</span></div>
      <div class="row"><span class="k">Boxed Warning</span><span class="v">{render_yes_detail(('Yes' + (' — '+H(boxed.get('content')) if boxed.get('content') else '') if boxed.get('present') else 'No'), boxed.get('full_text') if boxed.get('present') else None)}</span></div>
      <div class="row"><span class="k">Key Safety</span><span class="v">{cited_or_annotate('key_safety', r.get('key_safety',''))}</span></div>

      <div class="section-label">DRUG PROFILE</div>
      <div class="row"><span class="k">Target / MOA</span><span class="v"><b>{cited_or_annotate('target_moa', r.get('target_moa',''))}</b></span></div>
      <div class="row"><span class="k">Modality</span><span class="v"><b>{cited_or_annotate('modality', r.get('modality',''))}</b></span></div>

      <div class="section-label">POST-MARKETING REQUIREMENTS (PMR / PMC)</div>
      {render_post_marketing(r.get('post_marketing'))}
    """
    _refs = _card_refs()
    sources_html = _source_list_html(_refs) if _refs else ""
    return card_body + sources_html + f"""
      <div class="section-label">REVIEW DOCUMENTS (paperclip)</div>
      <div class="docs">{docs_html}</div>
    </div>"""

def render_chat_html(r):
    if not CHAT_ENABLED:
        return ""
    meta = r.get("_meta", {})
    appno = meta.get("appno") or ""
    typ = meta.get("type") or ""
    app_key = f"{typ}{appno.zfill(6)}" if appno and typ else ""
    drug = meta.get("drug", "")
    if not app_key:
        return ""
    return f"""
    <div class="chat" data-app-key="{H(app_key)}" data-drug="{H(drug)}">
      <div class="chat-log"></div>
      <div class="chat-input-row">
        <input class="chat-input" type="text" placeholder="Ask Sonnet about {H(drug)}'s FDA review…" />
        <button class="chat-send">Send</button>
      </div>
      <div class="chat-meta"></div>
    </div>"""

STYLE = """
<style>
html, body { height: 100%; margin: 0; }
body { font-family: -apple-system, system-ui, sans-serif; background: #f3f4f6; display: flex; flex-direction: column; overflow: hidden; }
header { background: #1f2937; color: white; padding: 10px 16px; flex: 0 0 auto; }
header h1 { margin: 0; font-size: 16px; font-weight: 600; }
header .sub { font-size: 11px; color: #9ca3af; margin-top: 2px; }
.layout { display: flex; flex: 1 1 auto; min-height: 0; }
/* Left: drug list */
.sidebar { width: 240px; flex: 0 0 240px; background: white; border-right: 1px solid #e5e7eb; display: flex; flex-direction: column; min-height: 0; }
.filter-bar { padding: 8px; border-bottom: 1px solid #e5e7eb; display: flex; flex-direction: column; gap: 6px; }
.filter-bar input, .filter-bar select { width: 100%; padding: 5px 8px; border: 1px solid #d1d5db; border-radius: 4px; font-size: 13px; box-sizing: border-box; background: white; }
.filter-bar .f-check { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #374151; cursor: pointer; }
.filter-bar .f-check input { width: auto; margin: 0; }
.filter-count { font-size: 11px; color: #6b7280; padding: 2px 4px; text-align: right; }
.drug-list { flex: 1 1 auto; overflow-y: auto; }
.drug-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #f3f4f6; font-size: 13px; }
.drug-item:hover { background: #f9fafb; }
.drug-item.active { background: #dbeafe; font-weight: 600; }
.drug-item .meta { font-size: 10px; color: #9ca3af; font-weight: normal; }
.drug-item .meta-row { display: flex; gap: 6px; align-items: baseline; margin-top: 3px; }
.drug-item .ta-pill { font-size: 10px; background: #dbeafe; color: #1d4ed8; padding: 1px 6px; border-radius: 3px; font-weight: 500; }
.drug-item.hidden { display: none; }
/* Center: card */
.center { flex: 1 1 auto; overflow-y: auto; padding: 16px; min-width: 0; }
.card { background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 20px; font-size: 16px; line-height: 1.55; display: none; }
.card.active { display: block; }
.card.error { border-color: #ef4444; }
.header-row { display: flex; gap: 12px; align-items: center; margin-bottom: 6px; font-size: 12px; color: #6b7280; }
.tab { background: #1f2937; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.metric { font-size: 11px; }
.title { font-size: 24px; font-weight: 700; margin: 6px 0; }
.active-mol { font-weight: 400; color: #6b7280; font-size: 16px; }
.sponsor { color: #6b7280; font-size: 14px; margin-bottom: 10px; }
.section-label { font-size: 13px; font-weight: 700; letter-spacing: 0.05em; color: #6b7280; margin: 20px 0 8px 0; text-transform: uppercase; }
.indication { font-style: italic; color: #374151; padding: 10px 12px; background: #f9fafb; border-left: 3px solid #d1d5db; font-size: 15px; }
.pills { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0; }
.pill { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.legend { font-size: 10px; color: #9ca3af; margin-top: 2px; }
.approval-program { display: flex; flex-direction: column; gap: 8px; margin: 6px 0; }
.ap-prog { border-left: 3px solid #0a7d2f; padding: 6px 10px; background: #f6fbf7; border-radius: 4px; }
.ap-badge { display: inline-block; font-size: 11px; font-weight: 700; color: #0a7d2f; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 3px; }
.ap-detail { font-size: 13px; margin: 2px 0; line-height: 1.4; }
.ap-detail .ap-k { font-weight: 600; color: #374151; margin-right: 4px; }
.ap-summary { font-size: 13px; color: #374151; font-style: italic; margin-top: 4px; padding: 4px 0; border-top: 1px dashed #d1d5db; }
.ap-none { font-size: 13px; color: #9ca3af; }
.post-marketing { display: flex; flex-direction: column; gap: 8px; margin: 6px 0; }
.pm-group { border-left: 3px solid #b45309; padding: 4px 10px; background: #fffbeb; border-radius: 4px; }
.pm-badge { display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 3px; }
.pm-badge.pmr { color: #b45309; }
.pm-badge.pmc { color: #6b7280; }
.pm-list { margin: 2px 0 2px 16px; padding: 0; }
.pm-list li { font-size: 13px; line-height: 1.4; margin: 3px 0; }
.pm-none { font-size: 13px; color: #9ca3af; }
.trial { background: #f9fafb; padding: 10px 12px; border-radius: 4px; margin: 8px 0; font-size: 15px; border-left: 3px solid #93c5fd; }
.trial summary { cursor: pointer; outline: none; list-style: none; padding-right: 6px; }
.trial summary::-webkit-details-marker { display: none; }
.trial summary::before { content: "▶ "; color: #2563eb; font-size: 13px; margin-right: 6px; }
.trial[open] summary::before { content: "▼ "; }
.trial summary:hover { color: #2563eb; }
.trial[open] summary { margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px dashed #d1d5db; }
.trial.empty { color: #9ca3af; font-style: italic; }
.ind-chips-wrap { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.ind-chip { display: inline-flex; gap: 4px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 4px; padding: 2px 6px; font-size: 11px; align-items: baseline; }
.ind-chip-k { color: #1d4ed8; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; font-size: 9px; }
.ind-chip-v { color: #1f2937; }
.supp { padding: 10px 12px; margin: 8px 0; background: #fffbeb; border-left: 3px solid #fbbf24; border-radius: 4px; font-size: 15px; }
.supp.empty { color: #9ca3af; font-style: italic; border-left-color: #e5e7eb; background: #f9fafb; }
.supp-head { display: flex; gap: 8px; align-items: baseline; margin-bottom: 5px; font-size: 13px; color: #92400e; }
.supp-date { font-family: ui-monospace, monospace; }
.supp-sn { font-weight: 600; }
.supp-links { margin-left: auto; }
.supp-links a { color: #2563eb; margin-left: 6px; }
.supp-desc { color: #1f2937; line-height: 1.4; }
.supp.safety { background: #fff7ed; border-left-color: #fb923c; }
.supp.safety .supp-head { color: #9a3412; }
.pm-badge.safety { color: #c2410c; }
.supp.boxed { background: #fef2f2; border-left-color: #dc2626; }
.supp.boxed .supp-head { color: #991b1b; }
.pm-badge.boxed { color: #b91c1c; }
.supp.minor { background: #f9fafb; border-left-color: #e5e7eb; color: #6b7280; font-style: italic; font-size: 13px; padding: 7px 12px; }
.supp.minor .nopdf { color: #9ca3af; font-style: normal; font-size: 11.5px; }
.report-err { margin-left: auto; font-size: 11.5px; color: #9ca3af; }
.report-err a { color: #9ca3af; text-decoration: none; }
.report-err a:hover { color: #dc2626; text-decoration: underline; }
.adcom-detail { margin-top: 6px; font-size: 14px; }
.adcom-meet { color: #6b7280; font-size: 13px; }
.adcom-rec { margin-top: 5px; }
.adcom-linkages { margin-top: 4px; }
.adcom-linkages summary { cursor: pointer; color: #2563eb; font-size: 14px; padding: 6px 0; font-weight: 600; }
.adcom-pair { background: #fffbeb; border-left: 3px solid #fbbf24; padding: 6px 8px; margin: 4px 0; border-radius: 4px; }
.adcom-concern { color: #be123c; font-size: 14px; }
.adcom-change { color: #1d4ed8; font-size: 14px; margin-top: 5px; }
.adcom-cat { display: inline-block; background: #fef3c7; color: #92400e; padding: 1px 6px; border-radius: 3px; font-size: 10px; margin-top: 3px; }
.adcom-evidence { color: #6b7280; font-size: 11px; margin-top: 4px; padding-left: 6px; border-left: 2px solid #e5e7eb; }
.yes-detail { display: inline; }
.yes-detail summary { cursor: pointer; display: inline; list-style: none; }
.yes-detail summary::-webkit-details-marker { display: none; }
.yes-detail summary::after { content: " ▼ show details"; font-size: 13px; font-weight: 600; color: #2563eb; margin-left: 6px; }
.yes-detail[open] summary::after { content: " ▲ hide"; }
.yes-detail summary:hover::after { text-decoration: underline; }
.yes-detail-body { display: block; margin-top: 10px; padding: 12px 14px; background: #fff8e1; border-left: 3px solid #f59e0b; border-radius: 4px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; color: #1f2937; }
.row { display: flex; gap: 10px; margin: 6px 0; }
.k { color: #6b7280; min-width: 140px; font-size: 14px; font-weight: 600; }
.v { flex: 1; font-size: 15px; }
.docs { font-size: 11px; }
.doc-row { padding: 3px 0; border-bottom: 1px dashed #e5e7eb; }
.doc-type { font-weight: 600; color: #1f2937; }
.doc-id { color: #9ca3af; font-family: ui-monospace, monospace; }
.doc-url { color: #2563eb; text-decoration: none; margin-left: 4px; }
.doc-url:hover { text-decoration: underline; }
.doc-missing { color: #9ca3af; font-style: italic; }
.empty-state { color: #9ca3af; text-align: center; margin-top: 80px; font-size: 14px; }
/* Right: chat */
.chat-pane { width: 420px; flex: 0 0 420px; background: white; border-left: 1px solid #e5e7eb; display: flex; flex-direction: column; min-height: 0; }
.chat-header { padding: 10px 12px; border-bottom: 1px solid #e5e7eb; font-size: 12px; font-weight: 600; color: #1f2937; flex: 0 0 auto; }
.chat-header .sub { color: #9ca3af; font-weight: normal; font-size: 10px; margin-top: 2px; }
.chat { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
.chat.hidden { display: none; }
.chat-log { flex: 1 1 auto; overflow-y: auto; padding: 10px 14px; }
.chat-msg { padding: 8px 12px; margin: 6px 0; border-radius: 6px; font-size: 14px; line-height: 1.5; }
.chat-msg.user { background: #dbeafe; white-space: pre-wrap; }
.chat-msg.assistant { background: #f3f4f6; }
.chat-msg.error { background: #fee2e2; white-space: pre-wrap; }
.chat-msg.assistant p { margin: 0.4em 0; }
.chat-msg.assistant p:first-child { margin-top: 0; }
.chat-msg.assistant p:last-child { margin-bottom: 0; }
.chat-msg.assistant ul, .chat-msg.assistant ol { margin: 0.4em 0; padding-left: 1.5em; }
.chat-msg.assistant li { margin: 0.2em 0; }
.chat-msg.assistant code { background: #e5e7eb; padding: 1px 4px; border-radius: 3px; font-size: 12px; font-family: ui-monospace, monospace; }
.chat-msg.assistant pre { background: #1f2937; color: #f3f4f6; padding: 8px 10px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
.chat-msg.assistant pre code { background: none; padding: 0; color: inherit; }
.chat-msg.assistant strong { font-weight: 700; }
.chat-msg.assistant em { font-style: italic; }
.chat-msg.assistant blockquote { border-left: 3px solid #d1d5db; margin: 0.4em 0; padding-left: 10px; color: #4b5563; }
.chat-msg.assistant h1, .chat-msg.assistant h2, .chat-msg.assistant h3 { margin: 0.5em 0 0.3em; font-weight: 700; }
.chat-msg.assistant h1 { font-size: 16px; }
.chat-msg.assistant h2 { font-size: 15px; }
.chat-msg.assistant h3 { font-size: 14px; }
.chat-msg.assistant table { border-collapse: collapse; margin: 0.4em 0; font-size: 13px; }
.chat-msg.assistant th, .chat-msg.assistant td { border: 1px solid #d1d5db; padding: 4px 8px; }
.chat-msg.assistant th { background: #e5e7eb; }
.chat-input-row { display: flex; gap: 4px; padding: 8px 12px; border-top: 1px solid #e5e7eb; flex: 0 0 auto; }
.chat-input { flex: 1; padding: 6px 8px; border: 1px solid #d1d5db; border-radius: 4px; font-size: 13px; }
.chat-send { background: #2563eb; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; }
.chat-send:disabled { background: #9ca3af; cursor: wait; }
.chat-meta { font-size: 10px; color: #9ca3af; padding: 0 12px 8px; flex: 0 0 auto; }

/* Clickable audit chips */
.qlink { cursor: pointer; border-radius: 3px; padding: 0 3px; transition: background 0.1s; }
.qlink.tier-exact             { background: #dcfce7; color: #166534; border-bottom: 1px solid #86efac; }
.qlink.tier-normalized        { background: #ecfccb; color: #4d7c0f; border-bottom: 1px solid #bef264; }
.qlink.tier-bare-with-context { background: #fef3c7; color: #92400e; border-bottom: 1px solid #fcd34d; }
.qlink:hover { filter: brightness(0.92); }
#src-toggle{position:fixed;top:8px;right:12px;z-index:1000;background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:4px 8px;font-family:-apple-system,sans-serif;font-size:12px;box-shadow:0 1px 4px rgba(0,0,0,.12);display:flex;align-items:center;gap:6px}
#src-toggle span{color:#475569}
#src-toggle button{border:1px solid #cbd5e1;background:#f1f5f9;color:#334155;border-radius:5px;padding:2px 8px;cursor:pointer;font-size:12px}
#src-toggle button.on{background:#2563eb;color:#fff;border-color:#2563eb}
.adcom-basis{font-size:10px;font-weight:700;padding:1px 5px;border-radius:3px;text-transform:uppercase;letter-spacing:.03em}
.adcom-basis.stated{background:#dcfce7;color:#166534}
.adcom-basis.inferred{background:#fef3c7;color:#92400e}
.refmark { font-size: 0.72em; line-height: 0; }
.refmark a.qlink { color:#2563eb; font-weight:700; background:none; padding:0 1px; text-decoration:none; border:none; }
.refmark a.qlink:hover { text-decoration:underline; filter:none; }
.unvmark { font-size:0.62em; color:#b91c1c; background:#fee2e2; border-radius:3px; padding:0 3px; margin-left:2px; font-weight:700; vertical-align:super; }
ol.refs { margin:4px 0 2px 0; padding-left:22px; font-size:12px; color:#475569; }
ol.refs li { margin:1px 0; }
ol.refs a.qlink { color:#2563eb; background:none; padding:0; border:none; }
ol.refs a.qlink:hover { text-decoration:underline; filter:none; }

/* Slide-in PDF panel — fixed overlay on the right, doesn't disturb layout */
.pdf-panel {
  position: fixed; top: 56px; right: 0; bottom: 0; width: 0;
  background: #fff; border-left: 1px solid #e5e7eb;
  box-shadow: -4px 0 12px rgba(0,0,0,0.08);
  display: flex; flex-direction: column; overflow: hidden;
  transition: width 0.18s ease; z-index: 50;
}
.pdf-panel.open { width: 50vw; min-width: 480px; max-width: 900px; }
.pdf-hdr { display: flex; align-items: center; justify-content: space-between;
           padding: 8px 12px; background: #f3f4f6; border-bottom: 1px solid #e5e7eb;
           flex: 0 0 auto; }
.pdf-hdr-title { font-size: 12px; font-weight: 600; color: #374151;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.pdf-close { background: none; border: none; font-size: 20px; cursor: pointer;
             color: #6b7280; padding: 0 6px; line-height: 1; }
.pdf-close:hover { color: #111827; }
.pdf-frame { flex: 1 1 auto; border: none; width: 100%; }
</style>
"""

SCRIPT = r"""
<script>
function uuidv4() { return crypto.randomUUID ? crypto.randomUUID() : 'sess-'+Math.random().toString(36).slice(2)+Date.now(); }
const SESSION = uuidv4();

function renderMarkdown(text) {
  if (typeof marked === 'undefined') return text;
  try { return marked.parse(text, { breaks: true, gfm: true }); }
  catch(e) { return text; }
}
function addMsg(log, role, text) {
  const d = document.createElement('div'); d.className = 'chat-msg '+role;
  if (role === 'assistant') { d.innerHTML = renderMarkdown(text); }
  else { d.textContent = text; }
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}

// Hook up each chat's send button
document.querySelectorAll('.chat').forEach(chat => {
  const appKey = chat.dataset.appKey;
  const input = chat.querySelector('.chat-input');
  const btn   = chat.querySelector('.chat-send');
  const log   = chat.querySelector('.chat-log');
  const meta  = chat.querySelector('.chat-meta');
  async function send() {
    const text = input.value.trim(); if (!text) return;
    input.value = ''; btn.disabled = true;
    addMsg(log, 'user', text);
    try {
      const r = await fetch('api/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: SESSION, app_key: appKey, message: text})
      });
      const j = await r.json();
      if (j.error) addMsg(log, 'error', 'ERROR: '+j.error);
      else { addMsg(log, 'assistant', j.reply || '(empty reply)');
             meta.textContent = ''; }
    } catch(e) { addMsg(log, 'error', 'fetch error: '+e); }
    btn.disabled = false; input.focus();
  }
  btn.addEventListener('click', send);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
});

// Drug selection + search
const items = Array.from(document.querySelectorAll('.drug-item'));
const cards = Array.from(document.querySelectorAll('.card'));
const chats = Array.from(document.querySelectorAll('.chat'));
const emptyState = document.querySelector('.empty-state');
const chatHeaderName = document.querySelector('.chat-header .name');
const chatHeaderSub  = document.querySelector('.chat-header .sub');
function selectDrug(appKey) {
  items.forEach(it => it.classList.toggle('active', it.dataset.appKey === appKey));
  cards.forEach(c => c.classList.toggle('active', c.dataset.appKey === appKey));
  chats.forEach(c => c.classList.toggle('hidden', c.dataset.appKey !== appKey));
  const chosen = items.find(it => it.dataset.appKey === appKey);
  if (chosen) {
    if (chatHeaderName) chatHeaderName.textContent = chosen.querySelector('.name').textContent;
    if (chatHeaderSub) chatHeaderSub.textContent = chosen.querySelector('.meta').textContent;
    if (emptyState) emptyState.style.display = 'none';
  }
}
items.forEach(it => it.addEventListener('click', () => selectDrug(it.dataset.appKey)));
// Combined search / TA / year filter
const fSearch = document.getElementById('f-search');
const fTA = document.getElementById('f-ta');
const fYear = document.getElementById('f-year');
const fCount = document.getElementById('filter-count');
const fExp = document.getElementById('f-exp');
const fCrl = document.getElementById('f-crl');
const fAdcom = document.getElementById('f-adcom');
const fBoxed = document.getElementById('f-boxed');
const fPmr = document.getElementById('f-pmr');
const nowYear = new Date().getFullYear();
function applyFilters() {
  const q = fSearch.value.trim().toLowerCase();
  const ta = fTA.value;
  const yspan = parseInt(fYear.value || '0', 10);
  const exp = fExp.value;
  const crlOnly = fCrl.checked;
  const adcomOnly = fAdcom.checked;
  const boxedOnly = fBoxed.checked;
  const pmrOnly = fPmr.checked;
  let n = 0;
  items.forEach(it => {
    const text = it.textContent.toLowerCase();
    const itTA = it.dataset.ta || '';
    const itYear = parseInt(it.dataset.year || '0', 10);
    let ok = true;
    if (q && !text.includes(q)) ok = false;
    if (ta && itTA !== ta) ok = false;
    if (yspan && (!itYear || itYear < nowYear - yspan)) ok = false;
    if (exp && !(it.dataset.exp || '').split('|').includes(exp)) ok = false;
    if (crlOnly && it.dataset.crl !== '1') ok = false;
    if (adcomOnly && it.dataset.adcom !== '1') ok = false;
    if (boxedOnly && it.dataset.boxed !== '1') ok = false;
    if (pmrOnly && it.dataset.pmr !== '1') ok = false;
    it.classList.toggle('hidden', !ok);
    if (ok) n += 1;
  });
  fCount.textContent = n + ' drug' + (n === 1 ? '' : 's');
}
[fSearch, fTA, fYear, fExp, fCrl, fAdcom, fBoxed, fPmr].forEach(el => {
  el.addEventListener('input', applyFilters);
  el.addEventListener('change', applyFilters);
});
// Auto-select first
if (items.length > 0) selectDrug(items[0].dataset.appKey);

// --- PDF panel click handler (audit chips) ---
function openPdf(url, page, title, search) {
  const panel  = document.getElementById('pdf-panel');
  const frame  = document.getElementById('pdf-frame');
  const titleEl = document.getElementById('pdf-title');
  let src = 'pdfview?url=' + encodeURIComponent(url) + '&page=' + page;
  if (search) src += '&search=' + encodeURIComponent(search);
  frame.src = src;
  titleEl.textContent = title || 'FDA source document';
  panel.classList.add('open');
}
function closePdf() {
  document.getElementById('pdf-panel').classList.remove('open');
  document.getElementById('pdf-frame').src = '';
}
function openDoc(docId, line, q, label) {
  const panel  = document.getElementById('pdf-panel');
  const frame  = document.getElementById('pdf-frame');
  const titleEl = document.getElementById('pdf-title');
  let src = 'doc/' + encodeURIComponent(docId);
  const qs = [];
  if (line != null) qs.push('line=' + line);
  if (q) qs.push('q=' + encodeURIComponent(q));
  if (qs.length) src += '?' + qs.join('&');
  frame.src = src;
  if (titleEl) titleEl.textContent = (label || 'FDA review OCR')
      + (line != null ? (' · line ' + line) : '') + ' · OCR';
  panel.classList.add('open');
}
// Citations always open the paperclip OCR at the exact cited line.
document.body.addEventListener('click', (e) => {
  const el = e.target.closest('.qlink');
  if (!el) return;
  e.preventDefault();
  e.stopPropagation();
  let d;
  try { d = JSON.parse(el.dataset.pdf); } catch (err) { return; }
  if (!d || !d.doc_id) return;
  // Always open the paperclip OCR at the cited line (verified line-accurate).
  openDoc(d.doc_id, d.line, d.s, d.label);
});
</script>
"""

def render_drug_list_item(r):
    meta = r.get("_meta", {})
    appno = meta.get("appno") or ""
    typ = meta.get("type") or ""
    app_key = f"{typ}{appno.zfill(6)}" if appno and typ else ""
    drug = meta.get("drug", "(unnamed)")
    active = meta.get("active") or ""
    ta = r.get("therapeutic_area") or ""
    # Approval year from approval_date "M/D/YYYY"
    apdate = meta.get("approval_date") or ""
    year = ""
    m_yr = _re.search(r"(\d{4})", apdate)
    if m_yr: year = m_yr.group(1)
    if not app_key: return ""
    def _tv(x): return x.get("value", x) if isinstance(x, dict) else x
    exp = meta.get("expedited") or {}
    exp_codes = "|".join(k for k, v in exp.items() if v)
    crl = "1" if _tv((r.get("crl_history") or {}).get("prior_crl")) else "0"
    adcom = "1" if _tv((r.get("advisory_committee") or {}).get("convened")) else "0"
    boxed = "1" if (r.get("boxed_warning") or {}).get("present") is True else "0"
    pm = r.get("post_marketing"); pmr = "1" if isinstance(pm, list) and pm else "0"
    return (
        f'<div class="drug-item" data-app-key="{H(app_key)}" data-ta="{H(ta)}" data-year="{H(year)}" '
        f'data-exp="{H(exp_codes)}" data-crl="{crl}" data-adcom="{adcom}" data-boxed="{boxed}" data-pmr="{pmr}">'
        f'<div class="name">{H(drug)} <span class="meta">· {H(year)}</span></div>'
        f'<div class="meta-row">'
        f'<span class="ta-pill">{H(ta) if ta else ""}</span> '
        f'<span class="meta">{H(typ)} {H(appno)}</span>'
        f'</div>'
        f'</div>'
    )

@app.route("/")
def index():
    # Sort cards by drug name for the menu
    sortable = sorted(CARDS, key=lambda r: (r.get("_meta", {}).get("drug") or "").lower())
    drug_list_html = "\n".join(render_drug_list_item(r) for r in sortable)
    cards_html     = "\n".join(render_card_html(i, r) for i, r in enumerate(sortable))
    chats_html     = "\n".join(render_chat_html(r) for r in sortable)
    chat_pane_html = (('<aside class="chat-pane">'
                       '<div class="chat-header"><div class="name">Chat</div>'
                       '<div class="sub">Select a drug to begin</div></div>'
                       + chats_html + '</aside>') if CHAT_ENABLED else "")
    # Build TA dropdown options from cards
    tas = sorted({(r.get('therapeutic_area') or '').strip() for r in sortable if (r.get('therapeutic_area') or '').strip()})
    ta_options_html = "\n".join(f'<option value="{H(t)}">{H(t)}</option>' for t in tas)
    return f"""<!doctype html><html><head>
<meta charset="utf-8"><title>RxIndex Cards</title>
<script src="https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js"></script>
{STYLE}
</head><body>
<header>
  <h1>RxIndex Cards</h1>
  <div class="sub">{len(CARDS)} FDA drug approvals</div>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="filter-bar">
      <input id="f-search" type="text" placeholder="Search drugs…" />
      <select id="f-ta">
        <option value="">All therapeutic areas</option>
        {ta_options_html}
      </select>
      <select id="f-year">
        <option value="">All years</option>
        <option value="5">Last 5 years</option>
        <option value="10">Last 10 years</option>
        <option value="15">Last 15 years</option>
      </select>
      <select id="f-exp">
        <option value="">All expedited programs</option>
        <option value="btd">Breakthrough Therapy</option>
        <option value="pr">Priority Review</option>
        <option value="orph">Orphan Drug</option>
        <option value="ft">Fast Track</option>
        <option value="accelerated">Accelerated Approval</option>
        <option value="qidp">QIDP</option>
      </select>
      <label class="f-check"><input type="checkbox" id="f-crl"/> Had a CRL (approved on a later cycle)</label>
      <label class="f-check"><input type="checkbox" id="f-adcom"/> Advisory committee convened</label>
      <label class="f-check"><input type="checkbox" id="f-boxed"/> Boxed warning</label>
      <label class="f-check"><input type="checkbox" id="f-pmr"/> Post-marketing requirement</label>
      <div class="filter-count" id="filter-count">{len([r for r in sortable if (r.get('_meta',{}).get('appno'))])} drugs</div>
    </div>
    <div class="drug-list">{drug_list_html}</div>
  </aside>
  <main class="center">
    <div class="empty-state">Select a drug from the list to see its card.</div>
    {cards_html}
  </main>
  {chat_pane_html}
  <aside class="pdf-panel" id="pdf-panel">
    <div class="pdf-hdr">
      <span class="pdf-hdr-title" id="pdf-title">FDA source document</span>
      <button class="pdf-close" onclick="closePdf()" title="Close">&times;</button>
    </div>
    <iframe class="pdf-frame" id="pdf-frame"></iframe>
  </aside>
</div>
{SCRIPT}
</body></html>"""

if __name__ == "__main__":
    print(f"[startup] {len(CARDS)} cards loaded")   # doc labels already loaded at import
    app.run(host="0.0.0.0", port=PORT, threaded=True)

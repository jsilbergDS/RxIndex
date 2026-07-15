---
title: FDA Drug Cards Explorer
emoji: 💊
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# FDA Drug Cards Explorer (Flask)

Interactive Flask explorer for FDA-approved NMEs with chat grounded in OCR'd FDA review documents.

## Architecture

- **Build-time** (Sherlock): paperclip pulls OCR'd FDA review docs into `data/docs/*.txt`, indexed by `data/docs_index.json`. Cards in `data/cards.jsonl`.
- **Runtime** (HF Spaces, Docker SDK): app reads only from `data/`. Chat calls Sonnet 4.6 with `list_docs` / `read_doc` tools that read local files. No paperclip dependency.

## Secrets

- `ANTHROPIC_API_KEY` (required)
- `DAILY_USD_BUDGET` (optional, default `50.0`)

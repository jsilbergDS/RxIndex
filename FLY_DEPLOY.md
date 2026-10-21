# Fly.io deploy — RxIndex Cards (static viewer, chat OFF)

**Status: LIVE (public, no password) at https://rxindex-cards.fly.dev/**

> ⚠️ **fly.io only updates when you run `flyctl deploy`.** Editing `app.py` locally, or
> restarting the local dev server, does **not** touch the live app. After ANY change to
> `app.py` / `data/` that you want on fly.io, run (from this dir):
> ```bash
> export PATH="$HOME/.fly/bin:$PATH"
> flyctl deploy
> ```
> Verify it took: `curl -s https://rxindex-cards.fly.dev/ | grep -c id=\"f-pmr\"` (etc.).

A read-only card viewer. With chat disabled (`CHAT_ENABLED` unset) the app needs **no
Anthropic key** and **never calls paperclip** — citations open from the bundled
`data/docs/` OCR. Modeled on the working `INDexplorer/crl_deploy/` fly setup.

## Files (all in this dir; build context = this dir, so no data is duplicated)
- `fly.toml` — app `rxindex-cards`, region `sjc`, port 8080, gunicorn, 1 GB machine, scale-to-zero.
- `Dockerfile.fly` — bakes `app.py` + `data/` + `adcomm_out/`; runs `gunicorn app:app`.
- `.dockerignore` — drops `data/ocr` (168M, unused), `data/pdfs_cache` (re-downloadable),
  and build artifacts → image data ≈ `data/docs` (199M) + cards/meta (~15M).

## Deploy
```bash
cd /oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/primary_endpoint_project/flask_hf
flyctl auth login                 # once
flyctl launch --no-deploy --copy-config --name rxindex-cards   # first time: registers the app, keeps fly.toml
flyctl deploy                     # build + ship
```
`flyctl launch` (interactive) must be run by you — it needs your Fly login.

## Optional
- **Password gate:** `flyctl secrets set APP_PASSWORD=yourpass`
- **Re-enable chat later:** `flyctl secrets set CHAT_ENABLED=1 ANTHROPIC_API_KEY=sk-...`
  (chat also needs paperclip, which isn't in the image — so chat-on is HF/local only for now.)

## Notes
- The image is HF-independent; the existing `Dockerfile` (HF, port 7860) is untouched.
- If the doc-view memory cache pushes RAM, bump `memory` in `fly.toml` to `2048mb`.

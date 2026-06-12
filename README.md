# stacks-catalog

Self-hosted Open Library catalog index for The Hexadecagon Library.

A monthly GitHub Action streams the official Open Library data dumps
(editions + works + authors), distills them into a compact SQLite index
(per-subject shelving, full-text search), and publishes it as gzipped parts
on a GitHub Release. The zero-dependency Node server (Railway) downloads the
index onto its volume at boot and serves it.

## Pieces
- `.github/workflows/build-index.yml` — monthly builder (also run manually from the Actions tab)
- `scripts/build_index.py` — streaming dump → SQLite pipeline (stdlib only)
- `server.js` — zero-dependency catalog API (Node >= 22.5, `node:sqlite`)

## API
`/health` · `/version` · `/meta` · `/room?si=0..63&depth=0..` ·
`/search?q=&limit=` · `/work/OL45883W` · `/random`

All responses are CORS-open JSON in the exact doc shape the front-end expects.

## Railway setup
1. New service from this repo.
2. Attach a volume mounted at `/data` (the index is ~2–4 GB).
3. Variables: `GITHUB_REPO=<owner>/stacks-catalog`.
4. Deploy. First boot downloads the latest release; later boots reuse the
   volume and only re-download when a newer snapshot is released.

Data: Open Library / Internet Archive, via their monthly public dumps —
exactly the bulk-access route their API guidelines ask for.

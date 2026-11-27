# ChayanDesk — Public Sector Recruitment Portal

Static, high-speed public-sector recruitment registry built with **Astro**, **Tailwind CSS v4**, **strict TypeScript**, and an automated **Python + Google GenAI (`google-genai`)** daily collection pipeline.

## Data Store & Database Architecture

In accordance with `AGENTS.md` and `.agents/rules/agent-pipeline.md`:

- **Primary Job Store (`src/data/jobs/*.json`)**: Each recruitment notice is stored as an individual JSON document validated at build time by Astro Content Collections (`src/content.config.ts`) using Zod. This Git-backed JSON store serves as a zero-latency, zero-cost static database deployed directly to CDN edge nodes—no external SQL server is required.
- **Deduplication Index (`scripts/seen_urls.json`)**: Tracks already-processed official notice URLs and outcomes (`added`, `skipped`, `pending_admin`) so daily pipeline runs never re-spend Gemini API quota on unchanged announcements.
- **Admin Pending-PDF Queue (`scripts/pending_pdfs.json` & `scripts/pending_pdfs.md`)**: When an official `.gov.in` host blocks automated downloads (`403` / `robots.txt` / SSL timeout / SPA shell), the pipeline records the notice and official links here and notifies the admin via GitHub Issue instead of guessing or copying third-party text.
- **FreeJobAlert API Adapter (`scripts/freejobalert_api.py`)**: Modernized implementation of `dpnkrpl/jobful-api` endpoints (`latest-notifications`, `government-jobs`, `bank-jobs`, `railway-jobs`, `engineering-jobs`, `police-defence-jobs`, `teaching-jobs`, `state:XX`) with `robots.txt` compliance and outbound official link extraction (plus optional external `JOBFUL_API_URL` support).
- **Source Registry (`sources.json`)**: Configures `freejobalert_api`, `html_list`, `rss_feed`, and `api_json` feeds with `robots.txt` compliance and a minimum 2-second per-domain request delay.

## Environment Variables

Copy `.env.example` to `.env` for local usage:

- `PUBLIC_ADS_ENABLED`: Set to `"false"` before ad network approval (hides all `<AdSlot />` containers cleanly with `CLS = 0`).
- `GEMINI_API_KEY`: Required when running `scripts/fetch_jobs.py` to extract structured notices.
- `GEMINI_MODEL`: Optional override (defaults to `gemini-2.5-flash` with fallback models for transient `503`/`429` spikes).
- `JOBFUL_API_URL`: Optional URL of a hosted `dpnkrpl/jobful-api` service; when unset, `scripts/freejobalert_api.py` queries FreeJobAlert directly with `robots.txt` compliance.
- `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT`: Optional credentials for the `usajobs-open-api` source in `sources.json`.

## Local Commands

```bash
# Frontend checks and build
npm install
npx astro check
npm run build
npm run dev

# FreeJobAlert API JSON inspection (dpnkrpl/jobful-api compatible)
.\.venv\Scripts\python.exe scripts/freejobalert_api.py --endpoint latest-notifications --limit 5

# Daily scraper pipeline (dry-run or live)
.\.venv\Scripts\python.exe scripts/fetch_jobs.py --dry-run --source freejobalert-latest --limit 4
.\.venv\Scripts\python.exe scripts/fetch_jobs.py

# Local Admin Control Center (http://127.0.0.1:8787/)
.\.venv\Scripts\python.exe scripts/admin_server.py

# Admin manual PDF ingestion (when a pending official PDF is downloaded)
.\.venv\Scripts\python.exe scripts/fetch_jobs.py --pdf path/to/official.pdf --pending-id <id>
# Or drop <pending_id>.pdf into inbox_pdfs/ and push or run fetch_jobs.py
```

## Deployment & Automation

1. **Static Hosting (Cloudflare Pages, Vercel, Netlify, or GitHub Pages)**:
   - Build command: `npm run build`
   - Output directory: `dist`
   - Set `PUBLIC_ADS_ENABLED=false` (or `true` after ad approval) in the host's environment variables.
2. **Automated Daily Sync (`.github/workflows/daily-sync.yml`)**:
   - Runs daily at `06:00 UTC`, on `workflow_dispatch`, and automatically whenever you push a PDF into `inbox_pdfs/`.
   - Add `GEMINI_API_KEY` under **GitHub Repository Settings -> Secrets and variables -> Actions**.
   - Opens/updates a GitHub Issue `[Admin Action] Official PDFs Needed for Verification` whenever official PDFs require manual download, and closes it once ingested.

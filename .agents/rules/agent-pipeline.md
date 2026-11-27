---
trigger: glob
globs: "scripts/**/*.py, .github/workflows/*.yml, sources.json"
description: "Zero-hallucination scraping, Gemini extraction, deduplication, and GitHub Actions automation rules."
---

# Daily Job Scraper & AI Extraction Rules

## Zero-Hallucination Policy
- NEVER invent, estimate, or guess missing deadlines, salaries, age limits, or fees. If a field is not explicitly present in the source HTML or PDF, output `null` (rendered as "See Official Notification" on the frontend).
- Always store `source_url` (exact official notice page) and `official_pdf_url` (if available) plus `last_verified_at` (ISO 8601 UTC timestamp).

## Extraction & Value-Add Requirements
- Use the official `google-genai` Python SDK with structured JSON output enforced via Pydantic schemas.
- Skip URLs already recorded in `scripts/seen_urls.json` to conserve free-tier API quota.
- Never dump raw copied paragraphs from source sites. Synthesize facts into structured arrays: `eligibility_bullets`, `important_dates`, `fee_structure`, `vacancy_breakdown`, and `how_to_apply_steps`.
- Write each validated job as an individual JSON file in `src/data/jobs/<slug>.json` so Astro Content Collections can validate and build statically.

## Scraper Hygiene
- Respect `robots.txt` and enforce a minimum 2-second delay between requests to the same domain.
- Set a transparent `User-Agent` header identifying the bot.
- Catch and log per-source errors cleanly so one unreachable government website never crashes the entire daily GitHub Actions run.
# Government Job Portal — Core Architecture Rules

## Tech Stack (Strict)
- Framework: Astro (latest stable, static output mode by default).
- Styling: Tailwind CSS v4 configured via the official `@tailwindcss/vite` plugin in `astro.config.mjs` and imported via `@import "tailwindcss";` in `src/styles/global.css`. NEVER use the deprecated `@astrojs/tailwind` integration.
- Language: Strict TypeScript (`tsconfig.json` strict mode).
- Interactivity: Zero client-side JS frameworks (no React, Vue, or Svelte unless explicitly requested). Use standard HTML, CSS, and tiny vanilla `<script>` blocks only for search/filtering.
- Data Store: Astro Content Collections backed by JSON files in `src/data/jobs/` validated with Zod schemas.

## Performance & SEO Invariants
- Target 98–100 Google Lighthouse scores on Performance, Accessibility, Best Practices, and SEO.
- Never load external icon packs or heavy font families. Use system UI fonts (`Inter` only if self-hosted via `@fontsource`, otherwise clean system sans-serif + monospace for dates/codes).
- Every Job Detail page MUST output valid Schema.org `JobPosting` JSON-LD in `<head>`.
- Every page MUST include canonical URLs, unique `<title>`, `<meta name="description">`, and OpenGraph tags.
- Auto-generate `sitemap.xml` via `@astrojs/sitemap` and an RSS feed via `@astrojs/rss`.

## AdSense & Compliance Rules
- Reserve explicit `min-height` on all ad slot containers (`<AdSlot />`) to guarantee zero Cumulative Layout Shift (CLS = 0).
- Render `<AdSlot />` components conditionally behind an environment flag (`PUBLIC_ADS_ENABLED=false` during development) so no blank boxes appear before ad approval.
- NEVER place ads directly adjacent to "Apply Now" or "Official PDF" buttons.
- Every job page must display a clear "Not affiliated with any government agency" notice and direct links to the official source URL.
---
trigger: glob
globs: "*.astro, *.css, *.ts"
description: "ChayanDesk Command Desk editorial design system, light-theme tokens, and strict anti-AI UI constraints."
---

# ChayanDesk Command Desk — Editorial Light Design System

## Brand & Visual Philosophy
Design like an authoritative, flagship Indian Government Employment Gazette (editorial clarity of Mint / Employment News, utility speed of Sarkari Result—never a generic SaaS dashboard and never a 3-column 1990s link dump). Every viewport must communicate: "100% Verified Government & PSU Recruitment Only."

## Strict Anti-AI & Anti-Clutter Bans
- Light theme ONLY (no dark mode toggle, no dark hero banners).
- NO SaaS KPI metric cards at the top of the homepage (do not waste above-the-fold space on giant stat boxes).
- NO 3-equal-column Sarkari Result link-wall layout on the homepage; use the Dual-Intent 68% Main Stream / 32% Live Action Rail architecture.
- NO gradients, glassmorphism, floating drop-shadow cards (`shadow-md`/`shadow-lg`), oversized rounded corners (`rounded-xl`/`rounded-2xl`), stock illustrations, or decorative emojis in navigation chrome.
- Use sharp editorial borders (`1px solid #E2E8F0`) with `rounded-[3px]` corners and flat, high-contrast surfaces.

## Color Tokens (Define in Tailwind v4 `@theme` inside `src/styles/global.css`)
- Page Canvas: `#F9FAFB`
- Card / Table Surface: `#FFFFFF`
- Brand Sovereign Navy (Trust bar, primary headers, active tabs): `#0B2545` (hover `#163A63`)
- Gazette Crimson (Urgent deadlines, Result Out, Admit Card alerts): `#B91C1C` (surface `#FEF2F2`, border `#FECACA`)
- Reserve Emerald (Salary badges & 7th CPC pay levels): `#047857` (surface `#ECFDF5`, border `#A7F3D0`)
- Primary Ink: `#0F172A` | Secondary Ink: `#475569`
- Gazette Link Blue: `#0B57D0` | Visited Link Purple: `#5B21B6` (mandatory on notice links so repeat visitors spot already-checked posts)
- Hairline Dividers: `#E2E8F0` | Structural Borders: `#CBD5E1`

## Typography & Scannability
- Clean sans-serif UI (`IBM Plex Sans` or system sans) + monospace (`IBM Plex Mono` or system mono) with `font-variant-numeric: tabular-nums` on every salary figure, vacancy count, date, and countdown.
- Prioritize F-pattern horizontal row scanning: Post Title + Verified Source on left, Salary/Pay Level + Deadline + Save Bookmark anchored cleanly on right.
- Keep all `<AdSlot />` units labeled `"Advertisement"`, fixed-height (CLS = 0), and separated by at least `32px` from any "Apply Online" or "Download PDF" button.
#!/usr/bin/env python3
"""FreeJobAlert structured API adapter (modernized from dpnkrpl/jobful-api).

Supports category endpoints, state-wise endpoints, optional external jobful-api JSON
endpoints (JOBFUL_API_URL), and outbound official link extraction from /articles/ pages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

BASE_URL = "https://www.freejobalert.com"

CATEGORY_ENDPOINTS: dict[str, str] = {
    "latest-notifications": f"{BASE_URL}/latest-notifications/",
    "government-jobs": f"{BASE_URL}/government-jobs/",
    "other-all-india-exam": f"{BASE_URL}/government-jobs/",
    "bank-jobs": f"{BASE_URL}/bank-jobs/",
    "teaching-jobs": f"{BASE_URL}/teaching-jobs/",
    "engineering-jobs": f"{BASE_URL}/engineering-jobs/",
    "railway-jobs": f"{BASE_URL}/railway-jobs/",
    "defence-jobs": f"{BASE_URL}/police-defence-jobs/",
    "police-defence-jobs": f"{BASE_URL}/police-defence-jobs/",
}

# State mapping adapted from dpnkrpl/jobful-api/data/freeJobAlertStateMap.json
STATE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "AP": ("Andhra Pradesh", f"{BASE_URL}/ap-government-jobs/"),
    "AS": ("Assam", f"{BASE_URL}/assam-government-jobs/"),
    "BR": ("Bihar", f"{BASE_URL}/bihar-government-jobs/"),
    "CG": ("Chhattisgarh", f"{BASE_URL}/chhattisgarh-government-jobs/"),
    "DL": ("Delhi", f"{BASE_URL}/delhi-government-jobs/"),
    "GJ": ("Gujarat", f"{BASE_URL}/gujarat-government-jobs/"),
    "HP": ("Himachal Pradesh", f"{BASE_URL}/hp-government-jobs/"),
    "HR": ("Haryana", f"{BASE_URL}/haryana-government-jobs/"),
    "JH": ("Jharkhand", f"{BASE_URL}/jharkhand-government-jobs/"),
    "JK": ("Jammu & Kashmir", f"{BASE_URL}/jk-government-jobs/"),
    "KA": ("Karnataka", f"{BASE_URL}/karnataka-government-jobs/"),
    "KL": ("Kerala", f"{BASE_URL}/kerala-government-jobs/"),
    "MH": ("Maharashtra", f"{BASE_URL}/maharashtra-government-jobs/"),
    "MP": ("Madhya Pradesh", f"{BASE_URL}/mp-government-jobs/"),
    "OD": ("Odisha", f"{BASE_URL}/odisha-government-jobs/"),
    "OR": ("Odisha", f"{BASE_URL}/odisha-government-jobs/"),
    "PB": ("Punjab", f"{BASE_URL}/punjab-government-jobs/"),
    "RJ": ("Rajasthan", f"{BASE_URL}/rajasthan-government-jobs/"),
    "TN": ("Tamil Nadu", f"{BASE_URL}/tn-government-jobs/"),
    "TS": ("Telangana", f"{BASE_URL}/telangana-government-jobs/"),
    "UK": ("Uttarakhand", f"{BASE_URL}/uttarakhand-government-jobs/"),
    "UP": ("Uttar Pradesh", f"{BASE_URL}/up-government-jobs/"),
    "WB": ("West Bengal", f"{BASE_URL}/wb-government-jobs/"),
}

NON_RECRUITMENT_TERMS = (
    "admit card",
    "hall ticket",
    "answer key",
    "result",
    "merit list",
    "cut off",
    "cutoff",
    "syllabus",
    "exam date",
    "city intimation",
    "interview schedule",
    "score card",
    "scorecard",
    "selection list",
)

NOISE_DOMAINS = (
    "freejobalert.com",
    "whatsapp.com",
    "t.me",
    "telegram.me",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "linkedin.com",
    "news.google.com",
    "play.google.com",
    "apple.com",
)


@dataclass
class FreeJobAlertRow:
    """Structured notification record matching jobful-api fields."""

    post_date: Optional[str]
    post_board: str
    post_name: str
    qualification: str
    advt_no: Optional[str]
    last_date: Optional[str]
    detail_url: str


@dataclass
class OfficialLinksResult:
    """Outbound official links extracted from a FreeJobAlert detail article."""

    official_pdf_url: Optional[str] = None
    official_apply_url: Optional[str] = None
    official_website_url: Optional[str] = None


def parse_indian_date(raw: str) -> Optional[str]:
    """Parse DD/MM/YYYY, DD-MM-YYYY, or YYYY-MM-DD strings into YYYY-MM-DD."""
    cleaned = raw.strip()
    if not cleaned or cleaned in {"-", "–", "—", "N/A", "NA"}:
        return None
    # Pick first date match if text contains notes like "02-12-2026 (Extended)"
    match = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4}|\d{4}-\d{2}-\d{2})", cleaned)
    if not match:
        return None
    token = match.group(1)
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(token, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def resolve_endpoint_url(endpoint: str) -> str:
    """Resolve category slug, state:XX code, or direct URL."""
    ep = endpoint.strip()
    if ep.startswith("http://") or ep.startswith("https://"):
        return ep
    if ep.lower().startswith("state:"):
        code = ep.split(":", 1)[1].strip().upper()
        if code in STATE_ENDPOINTS:
            return STATE_ENDPOINTS[code][1]
        raise ValueError(f"Unknown FreeJobAlert state code: {code}")
    key = ep.lower().strip("/")
    if key in CATEGORY_ENDPOINTS:
        return CATEGORY_ENDPOINTS[key]
    raise ValueError(f"Unknown FreeJobAlert endpoint: {endpoint}")


def is_recruitment_row(post_name: str) -> bool:
    """Filter out admit cards, results, answer keys, and exam date alerts."""
    lowered = post_name.lower()
    return not any(term in lowered for term in NON_RECRUITMENT_TERMS)


def _find_col(header_cells: list[str], *candidates: str) -> Optional[int]:
    """Find column index matching any candidate header substring."""
    for cand in candidates:
        for idx, hdr in enumerate(header_cells):
            if cand in hdr:
                return idx
    return None


def parse_freejobalert_tables(
    html: str,
    base_url: str = BASE_URL,
    *,
    today_utc: Optional[date] = None,
    include_expired: bool = False,
) -> list[FreeJobAlertRow]:
    """Parse both 7-column (/latest-notifications/) and 9-column (state pages) FreeJobAlert tables."""
    soup = BeautifulSoup(html, "html.parser")
    ref_today = today_utc or date.today()
    rows_out: list[FreeJobAlertRow] = []
    seen_urls: set[str] = set()

    for table in soup.find_all("table"):
        tr_list = table.find_all("tr")
        if len(tr_list) < 2:
            continue
        header_cells = [c.get_text(" ", strip=True).lower() for c in tr_list[0].find_all(["th", "td"])]
        if not header_cells:
            continue

        board_idx = _find_col(header_cells, "recruitment board", "board")
        last_idx = _find_col(header_cells, "last date")
        if board_idx is None or last_idx is None:
            continue

        date_idx = _find_col(header_cells, "post date")
        name_idx = _find_col(header_cells, "exam / post name", "post name", "job title")
        qual_idx = _find_col(header_cells, "qualification")
        advt_idx = _find_col(header_cells, "advt no", "advt")
        link_idx = _find_col(header_cells, "more information", "details", "link")
        if name_idx is None:
            continue

        for tr in tr_list[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= max(board_idx, last_idx, name_idx):
                continue

            post_date_raw = cells[date_idx].get_text(" ", strip=True) if date_idx is not None and date_idx < len(cells) else ""
            post_board = cells[board_idx].get_text(" ", strip=True)
            post_name = re.sub(r"\s+", " ", cells[name_idx].get_text(" ", strip=True))
            qualification = (
                re.sub(r"\s+", " ", cells[qual_idx].get_text(" ", strip=True))
                if qual_idx is not None and qual_idx < len(cells)
                else "See Official Notification"
            )
            advt_raw = cells[advt_idx].get_text(" ", strip=True) if advt_idx is not None and advt_idx < len(cells) else ""
            last_date_raw = cells[last_idx].get_text(" ", strip=True)

            link_cell = cells[link_idx] if link_idx is not None and link_idx < len(cells) else cells[-1]
            link_tag = link_cell.find("a", href=True)

            if not link_tag or not post_name or not post_board:
                continue
            if not is_recruitment_row(post_name):
                continue

            detail_url = urljoin(base_url, str(link_tag["href"]).strip())
            if detail_url in seen_urls or not detail_url.startswith(("http://", "https://")):
                continue

            parsed_last_date = parse_indian_date(last_date_raw)
            if not include_expired and parsed_last_date:
                try:
                    if date.fromisoformat(parsed_last_date) < ref_today:
                        continue
                except ValueError:
                    pass

            advt_no = None if advt_raw in {"", "-", "–", "—", "N/A"} else advt_raw
            seen_urls.add(detail_url)
            rows_out.append(
                FreeJobAlertRow(
                    post_date=parse_indian_date(post_date_raw),
                    post_board=post_board,
                    post_name=post_name,
                    qualification=qualification or "See Official Notification",
                    advt_no=advt_no,
                    last_date=parsed_last_date,
                    detail_url=detail_url,
                )
            )

    return rows_out


def fetch_from_jobful_json_api(api_base_url: str, endpoint: str, http_get: Any) -> list[FreeJobAlertRow]:
    """Query an external hosted jobful-api compatible JSON service if configured."""
    base = api_base_url.rstrip("/")
    path = endpoint.lstrip("/")
    url = f"{base}/freejobalert/{path}" if not path.startswith("freejobalert/") else f"{base}/{path}"
    resp = http_get(url, check_robots=False)
    payload = resp.json()
    if not isinstance(payload, list):
        return []

    rows_out: list[FreeJobAlertRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        title = str(item.get("postName") or item.get("title") or "").strip()
        board = str(item.get("postBoard") or "Government Recruitment").strip()
        if not link.startswith(("http://", "https://")) or not title:
            continue
        if not is_recruitment_row(title):
            continue
        advt_raw = str(item.get("advtNo") or "").strip()
        rows_out.append(
            FreeJobAlertRow(
                post_date=parse_indian_date(str(item.get("postDate") or "")),
                post_board=board,
                post_name=title,
                qualification=str(item.get("qualification") or "See Official Notification").strip(),
                advt_no=None if advt_raw in {"", "-", "–", "N/A"} else advt_raw,
                last_date=parse_indian_date(str(item.get("lastDate") or "")),
                detail_url=link,
            )
        )
    return rows_out


def _is_noise_outbound(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return not host or any(domain in host for domain in NOISE_DOMAINS)


def extract_official_links_from_article(html: str, page_url: str) -> OfficialLinksResult:
    """Extract outbound official PDF, Apply Online, and agency website links from a detail article."""
    soup = BeautifulSoup(html, "html.parser")
    result = OfficialLinksResult()

    pdf_candidates: list[tuple[int, str]] = []
    apply_candidates: list[tuple[int, str]] = []
    website_candidates: list[tuple[int, str]] = []

    for tr in soup.find_all("tr"):
        row_text = tr.get_text(" ", strip=True).lower()
        for anchor in tr.find_all("a", href=True):
            href = urljoin(page_url, str(anchor["href"]).strip())
            if not href.startswith(("http://", "https://")) or _is_noise_outbound(href):
                continue
            anchor_text = anchor.get_text(" ", strip=True).lower()
            combined = f"{row_text} {anchor_text} {href.lower()}"

            if ".pdf" in href.lower() or "notification" in row_text or "advertisement" in row_text:
                score = 1
                if ".pdf" in href.lower():
                    score += 4
                if "english" in combined:
                    score += 3
                if "hindi" in combined and "english" not in combined:
                    score -= 2
                if any(ext in href.lower() for ext in (".gov.in", ".nic.in", ".ac.in", ".bank.in", ".org.in")):
                    score += 2
                pdf_candidates.append((score, href))
            elif "apply" in row_text or "apply" in anchor_text or "registration" in combined:
                score = 2
                if "login" in combined or "apply" in href.lower() or "reg" in href.lower():
                    score += 2
                apply_candidates.append((score, href))
            elif "official website" in row_text or "website" in anchor_text:
                website_candidates.append((1, href))

    # Fallback scan across all links if the table structure varies
    if not pdf_candidates and not apply_candidates:
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, str(anchor["href"]).strip())
            if not href.startswith(("http://", "https://")) or _is_noise_outbound(href):
                continue
            if ".pdf" in href.lower():
                pdf_candidates.append((3, href))
            elif any(d in href.lower() for d in (".gov.in", ".nic.in", ".ac.in", ".bank.in")):
                website_candidates.append((1, href))

    if pdf_candidates:
        pdf_candidates.sort(key=lambda item: item[0], reverse=True)
        result.official_pdf_url = pdf_candidates[0][1]
    if apply_candidates:
        apply_candidates.sort(key=lambda item: item[0], reverse=True)
        result.official_apply_url = apply_candidates[0][1]
    if website_candidates:
        website_candidates.sort(key=lambda item: item[0], reverse=True)
        result.official_website_url = website_candidates[0][1]

    if not result.official_apply_url:
        result.official_apply_url = result.official_website_url or result.official_pdf_url

    return result


def fetch_freejobalert_endpoint(
    endpoint: str,
    http_client: Any,
    *,
    limit: int = 10,
    api_base_url: Optional[str] = None,
    today_utc: Optional[date] = None,
) -> list[FreeJobAlertRow]:
    """Fetch structured FreeJobAlert rows via external jobful-api URL or direct compliant fetch."""
    if api_base_url:
        rows = fetch_from_jobful_json_api(api_base_url, endpoint, http_client.get)
    else:
        target_url = resolve_endpoint_url(endpoint)
        resp = http_client.get(target_url)
        rows = parse_freejobalert_tables(resp.text, target_url, today_utc=today_utc)
    return rows[:limit]


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint mirroring jobful-api JSON endpoints for testing."""
    import httpx

    parser = argparse.ArgumentParser(description="Fetch structured JSON from FreeJobAlert endpoints.")
    parser.add_argument(
        "--endpoint",
        default="latest-notifications",
        help="Endpoint name (latest-notifications, government-jobs, bank-jobs, state:UK, etc.)",
    )
    parser.add_argument("--limit", type=int, default=10, help="Max records to return.")
    parser.add_argument("--api-url", default=os.getenv("JOBFUL_API_URL"), help="Optional hosted jobful-api base URL.")
    args = parser.parse_args(argv)

    class _SimpleClient:
        def __init__(self) -> None:
            self._c = httpx.Client(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "ChayanDeskBot/1.0 (+https://chayandesk.in/about; desk@chayandesk.in)"},
            )

        def get(self, url: str, **_: Any) -> httpx.Response:
            r = self._c.get(url)
            r.raise_for_status()
            return r

        def close(self) -> None:
            self._c.close()

    client = _SimpleClient()
    try:
        rows = fetch_freejobalert_endpoint(args.endpoint, client, limit=args.limit, api_base_url=args.api_url)
        print(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

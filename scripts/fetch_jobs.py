#!/usr/bin/env python3
"""Automated daily recruitment notice collector, FreeJobAlert API feeder,
Admin PDF queue handler, and Gemini structured extractor."""

from __future__ import annotations

import argparse
import email.utils
import io
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator
from pypdf import PdfReader

from freejobalert_api import (
    STATE_ENDPOINTS,
    extract_official_links_from_article,
    fetch_freejobalert_endpoint,
)
from gemini_keys import (
    get_ordered_active_keys,
    mark_key_exhausted,
    mark_key_invalid,
    record_key_model_429,
    record_key_success,
)

_GEMINI_CLIENT_CACHE: dict[str, Any] = {}

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources.json"
SEEN_URLS_PATH = Path(__file__).resolve().parent / "seen_urls.json"
PENDING_PDFS_PATH = Path(__file__).resolve().parent / "pending_pdfs.json"
PENDING_PDFS_MD_PATH = Path(__file__).resolve().parent / "pending_pdfs.md"
STAGING_JOBS_DIR = Path(__file__).resolve().parent / "staging_jobs"
ADMIN_NOTIFICATIONS_PATH = Path(__file__).resolve().parent / "admin_notifications.json"
LAST_RUN_REPORT_PATH = Path(__file__).resolve().parent / "last_run_report.json"
INBOX_PDFS_DIR = REPO_ROOT / "inbox_pdfs"
JOBS_DIR = REPO_ROOT / "src" / "data" / "jobs"
ADMISSIONS_DIR = REPO_ROOT / "src" / "data" / "admissions"

DEFAULT_USER_AGENT = (
    "SarkariIndexBot/1.0 (+https://sarkariindex.pages.dev/about; desk@sarkariindex.pages.dev)"
)
SITE_BASE_URL = os.getenv("PUBLIC_SITE_URL", "https://sarkariindex.pages.dev").rstrip("/")
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
FALLBACK_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
]
MIN_DOMAIN_DELAY_SECONDS = 2.0
MAX_HTML_TEXT_CHARS = 24000
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_TEXT_PAGES = 25
MIN_EXTRACTED_PDF_CHARS = 150
MIN_USEFUL_HTML_CHARS = 300
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fetch_jobs")


# ---------------------------------------------------------------------------
# Pydantic Schemas (Strictly aligned with src/content.config.ts Zod schema)
# ---------------------------------------------------------------------------


class ImportantDateItem(BaseModel):
    """A milestone date extracted from the recruitment notice."""

    label: str = Field(description="Milestone name, e.g. 'Online Application Deadline'")
    date: str = Field(
        description="Exact date formatted strictly as DD Mon YYYY (e.g. '12 Dec 2026'), or concise window text if no single date exists"
    )


class VacancyBreakdownItem(BaseModel):
    """Category or post-wise vacancy distribution entry."""

    category: str = Field(description="Post title or reservation category name")
    vacancies: Optional[int] = Field(
        default=None,
        description="Integer count of vacancies for this row, or null if unspecified",
    )
    eligibility: Optional[str] = Field(
        default=None,
        description="Post-specific qualification/experience requirement for this post/row if stated, else null",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional short clarification (pay level, trade code, quota note)",
    )


class FeeStructureItem(BaseModel):
    """Category-wise fee breakdown item."""

    category: str = Field(description="Applicant category, e.g. General/OBC or SC/ST/PwBD")
    amount: str = Field(description="Exact fee amount or exemption note")


class ImportantLinkItem(BaseModel):
    """Named official action link (Apply, Notification, Result, Admit Card, Answer Key, Custom)."""

    label: str = Field(min_length=2)
    url: HttpUrl
    category: Optional[Literal["result", "admit-card", "answer-key"]] = Field(
        default=None,
        description="Optional homepage tab where this link places the job ('result', 'admit-card', 'answer-key')",
    )
    added_at: Optional[str] = Field(
        default=None,
        description="YYYY-MM-DD date when this link was added/updated",
    )


class GeminiJobExtraction(BaseModel):
    """Structured schema passed to Gemini for zero-hallucination job & admission extraction."""

    is_valid_job_notice: bool = Field(
        description=(
            "True if the document announces an official government/public-sector recruitment "
            "opening OR an official government university/board/entrance admission notice. "
            "False for standalone exam results without recruitment/admission details, tenders, news articles, or unrelated pages."
        )
    )
    post_type: Literal["job", "admission"] = Field(
        default="job",
        description=(
            "Set 'admission' for university, college, polytechnic, ITI, B.Ed, school entrance exams or counselling forms "
            "(e.g. CUET, JEE, NEET, JNVST, JEECUP, ICAR AIEEA, State CET admissions). Set 'job' for all public-sector employment recruitments."
        ),
    )
    course_level: Optional[str] = Field(
        default=None,
        description="If post_type='admission', programme level e.g. 'Undergraduate (B.Tech / B.Sc.)' or 'Diploma / Polytechnic'; else null.",
    )
    exam_date: Optional[str] = Field(
        default=None,
        description="If post_type='admission', entrance exam date formatted as '12 Dec 2026' or window text; else null.",
    )
    title: Optional[str] = Field(
        default=None,
        description="Concise official post or examination title, or null if absent",
    )
    organization: Optional[str] = Field(
        default=None,
        description="Issuing government ministry, commission, board, or PSU name",
    )
    department: Optional[str] = Field(
        default=None,
        description=(
            "Choose ONE concise canonical department bucket such as: "
            "'Banking & Finance', 'State Civil & Police Services', "
            "'State PSU & Infrastructure', 'Science & Defense Research', "
            "'Railways & Transport', 'Healthcare & Medical', or 'Civil Services & Administration'."
        ),
    )
    government_level: Optional[Literal["central", "state"]] = Field(
        default=None,
        description=(
            "Set 'central' for Central Ministries, UPSC, SSC, RRB, Central PSUs (ISRO, EIL, ECIL, Oil India, Munitions India), "
            "Public Sector Banks (SBI, BOB, Canara, EXIM, SIDBI, Indian Bank, Central Bank of India, SEBI, RBI), AIIMS, IIM, IIT, ESIC, Armed Forces Tribunal. "
            "Set 'state' for State PSC/SSC (APPSC, JKSSB, OSSC, RSSB, UKPSC, UKSSSC, KPSC, KEA, etc.), State departments, State medical colleges, municipal/district offices, or State co-op banks."
        ),
    )
    state_name: Optional[str] = Field(
        default=None,
        description="If government_level is 'state', canonical Indian state or UT name (e.g. 'Maharashtra', 'Uttar Pradesh', 'Bihar'); null if 'central'.",
    )
    location: Optional[str] = Field(
        default=None,
        description="Posting location or state/region, e.g. 'All India', 'Uttarakhand', 'Kerala', or 'Mumbai'",
    )
    qualification_level: Optional[str] = Field(
        default=None,
        description=(
            "Choose ONE concise canonical minimum qualification level from: "
            "'10th Pass', '12th Pass', 'Diploma / ITI', 'Graduate', "
            "'Engineering (B.E./B.Tech)', 'Post Graduate', 'Medical / Nursing', or 'Law (LL.B.)'."
        ),
    )
    total_vacancies: Optional[int] = Field(
        default=None,
        description="Exact integer total vacancies if explicitly stated, else null",
    )
    salary_range: Optional[str] = Field(
        default=None,
        description="Exact pay matrix level or salary range if stated, else null",
    )
    application_fee: Optional[str] = Field(
        default=None,
        description="Concise application fee summary if stated, else null",
    )
    age_limit: Optional[str] = Field(
        default=None,
        description="Exact age range or crucial date note if stated, else null",
    )
    notification_code: Optional[str] = Field(
        default=None,
        description="Official advertisement/notification number if present, else null",
    )
    important_dates: list[ImportantDateItem] = Field(
        default_factory=list,
        description="Key chronological milestones explicitly listed in the notice",
    )
    application_deadline: Optional[str] = Field(
        default=None,
        description="Application closing date strictly in YYYY-MM-DD format, or null if not stated",
    )
    posted_date: Optional[str] = Field(
        default=None,
        description="Publication/notification release date strictly in YYYY-MM-DD format, or null",
    )
    summary: Optional[str] = Field(
        default=None,
        description="Original 2-3 sentence factual synthesis (never copy-pasted verbatim paragraphs)",
    )
    eligibility_bullets: list[str] = Field(
        default_factory=list,
        description=(
            "Synthesized eligibility points covering educational qualification, experience, and age rules for EVERY post "
            "mentioned in the notice (prefix multi-post items as 'Post Name: requirement...')"
        ),
    )
    vacancy_breakdown: list[VacancyBreakdownItem] = Field(
        default_factory=list,
        description="Synthesized post-wise or category-wise vacancy rows including per-post eligibility notes where stated",
    )
    fee_structure: Optional[list[FeeStructureItem]] = Field(
        default=None,
        description="Organized category-wise fee table ({category, amount}) whenever fees are explicitly specified, else null",
    )
    how_to_apply_steps: list[str] = Field(
        default_factory=list,
        description="Synthesized step-by-step official application instructions",
    )
    official_apply_url: Optional[str] = Field(
        default=None,
        description="Direct official application or notice URL from the issuing organization",
    )
    official_pdf_url: Optional[str] = Field(
        default=None,
        description="Direct official recruitment notification PDF URL if present, else null",
    )
    important_links: list[ImportantLinkItem] = Field(
        default_factory=list,
        description="Additional verified official links on the page (e.g. Exam Notice, Syllabus PDF, Admit Card, Answer Key, Result), else empty list",
    )


class ValidatedJobRecord(BaseModel):
    """Final strict record matching src/content.config.ts before serialization."""

    slug: str
    post_type: Literal["job", "admission"] = "job"
    course_level: Optional[str] = None
    exam_date: Optional[str] = None
    title: str = Field(min_length=3)
    organization: str = Field(min_length=2)
    department: str = Field(min_length=2)
    government_level: Literal["central", "state"] = "central"
    state_name: Optional[str] = None
    location: str = Field(min_length=2)
    qualification_level: str = Field(min_length=2)
    total_vacancies: Optional[int] = Field(default=None, gt=0)
    salary_range: Optional[str] = Field(default=None, min_length=1)
    salary_min: Optional[int] = Field(default=None, ge=0)
    salary_max: Optional[int] = Field(default=None, ge=0)
    pay_level: Optional[str] = Field(default=None, min_length=1)
    salary_tier: Literal["up-to-25k", "25k-45k", "45k-70k", "70k-1l", "above-1l", "not-specified"] = "not-specified"
    authority_type: Optional[Literal["central", "state", "psu-bank", "defence-police", "teaching"]] = None
    exam_group_id: Optional[str] = None
    application_fee: Optional[str] = Field(default=None, min_length=1)
    age_limit: Optional[str] = Field(default=None, min_length=1)
    important_dates: list[ImportantDateItem] = Field(default_factory=list)
    application_deadline: Optional[str] = None
    posted_date: str
    last_verified_at: str
    summary: str = Field(min_length=20)
    eligibility_bullets: list[str] = Field(default_factory=list)
    vacancy_breakdown: list[VacancyBreakdownItem] = Field(default_factory=list)
    how_to_apply_steps: list[str] = Field(default_factory=list)
    official_apply_url: HttpUrl
    official_pdf_url: Optional[HttpUrl] = None
    important_links: Optional[list[ImportantLinkItem]] = None
    homepage_menus: list[Literal["result", "admit-card", "answer-key"]] = Field(default_factory=list)
    source_domain: str = Field(min_length=3)
    source_url: HttpUrl
    notification_code: Optional[str] = None
    fee_structure: Optional[list[FeeStructureItem]] = None
    archived: bool = False

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"Invalid slug format: {value}")
        return value

    @field_validator("application_deadline")
    @classmethod
    def check_optional_date(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not ISO_DATE_RE.match(value):
            raise ValueError(f"Invalid YYYY-MM-DD deadline: {value}")
        date.fromisoformat(value)
        return value

    @field_validator("posted_date")
    @classmethod
    def check_posted_date(cls, value: str) -> str:
        if not ISO_DATE_RE.match(value):
            raise ValueError(f"Invalid YYYY-MM-DD posted_date: {value}")
        date.fromisoformat(value)
        return value


# ---------------------------------------------------------------------------
# Dataclasses & Run Metrics
# ---------------------------------------------------------------------------


@dataclass
class CandidateItem:
    """Discovered recruitment announcement candidate."""

    url: str
    source_id: str
    source_domain: str
    title_hint: str = ""
    posted_date_hint: Optional[str] = None
    deadline_hint: Optional[str] = None
    default_organization: Optional[str] = None
    default_department: Optional[str] = None
    qualification_hint: Optional[str] = None
    advt_no_hint: Optional[str] = None
    summary_hint: Optional[str] = None
    extra_context: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    discovery_url: Optional[str] = None
    official_apply_url_hint: Optional[str] = None
    official_pdf_url_hint: Optional[str] = None
    allow_pending_queue: bool = False
    state_code: Optional[str] = None


@dataclass
class FetchedContent:
    """Fetched HTML/PDF payload ready for Gemini extraction."""

    final_url: str
    html_text: str = ""
    pdf_url: Optional[str] = None
    pdf_text: str = ""
    raw_pdf_bytes: Optional[bytes] = None
    needs_raw_pdf_upload: bool = False


@dataclass
class RunStats:
    """Execution summary counters."""

    checked: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0
    pending_admin: int = 0
    archived: int = 0


# ---------------------------------------------------------------------------
# Helpers: Normalization, URL validation, Slugification, .env
# ---------------------------------------------------------------------------


def load_dotenv_file(env_path: Path) -> None:
    """Load key=value pairs from .env if not already defined in os.environ."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def slugify(text: str, max_length: int = 72) -> str:
    """Create a deterministic lowercase hyphenated slug matching Astro constraints."""
    cleaned = text.lower().replace("&", " ")
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    if len(cleaned) <= max_length:
        return cleaned or "recruitment-notice"
    truncated = cleaned[:max_length].rsplit("-", 1)[0].strip("-")
    return truncated or cleaned[:max_length].strip("-")


def normalize_url(url: str) -> str:
    """Normalize whitespace and strip fragments from a URL."""
    parsed = urlparse(url.strip())
    return parsed._replace(fragment="").geturl()


def is_valid_http_url(url: Optional[str]) -> bool:
    """Verify URL uses http/https and includes a network host."""
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def extract_domain(url: str) -> str:
    """Extract normalized lowercase domain without leading www."""
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def parse_to_iso_date(raw_value: Optional[str]) -> Optional[str]:
    """Parse ISO, DD Mon YYYY, DD/MM/YYYY, DD-MM-YYYY, or RFC 2822 dates into YYYY-MM-DD."""
    if not raw_value:
        return None
    val = re.sub(r"\s+", " ", raw_value.strip())
    if ISO_DATE_RE.match(val):
        return val
    if len(val) >= 10 and ISO_DATE_RE.match(val[:10]):
        try:
            date.fromisoformat(val[:10])
            return val[:10]
        except ValueError:
            pass
    for fmt in ("%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y", "%b %d, %Y", "%B %d, %Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        dt = email.utils.parsedate_to_datetime(val)
        return dt.date().isoformat()
    except (TypeError, ValueError):
        return None


def format_display_date_str(raw_value: Optional[str]) -> str:
    """Format any parseable date into 'DD Mon YYYY' (e.g. '12 Dec 2026')."""
    if not raw_value or not raw_value.strip():
        return ""
    iso = parse_to_iso_date(raw_value)
    if iso:
        dt = date.fromisoformat(iso)
        return f"{dt.day} {dt.strftime('%b %Y')}"
    return raw_value.strip()


def make_unique_slug(base_slug: str, jobs_dir: Path, reserved_slugs: set[str]) -> str:
    """Ensure generated slug does not collide with existing, staged, or in-flight files."""
    candidate = base_slug
    counter = 2
    while (
        candidate in reserved_slugs
        or (jobs_dir / f"{candidate}.json").exists()
        or (STAGING_JOBS_DIR / f"{candidate}.json").exists()
    ):
        suffix = f"-{counter}"
        candidate = f"{base_slug[: max(10, 72 - len(suffix))].rstrip('-')}{suffix}"
        counter += 1
    reserved_slugs.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Polite HTTP Client with robots.txt & Per-Domain Rate Limiting
# ---------------------------------------------------------------------------


class PoliteHttpClient:
    """HTTP client enforcing robots.txt rules and >=2s delay per domain."""

    def __init__(self, user_agent: str, min_delay: float = MIN_DOMAIN_DELAY_SECONDS) -> None:
        self.user_agent = user_agent
        self.min_delay = max(MIN_DOMAIN_DELAY_SECONDS, min_delay)
        self._domain_last_request: dict[str, float] = {}
        self._robots_cache: dict[str, Optional[RobotFileParser]] = {}
        self._client = httpx.Client(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/json,application/pdf;q=0.9,*/*;q=0.8",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _throttle(self, domain: str) -> None:
        last_ts = self._domain_last_request.get(domain)
        if last_ts is not None:
            elapsed = time.monotonic() - last_ts
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)
        self._domain_last_request[domain] = time.monotonic()

    def can_fetch(self, url: str) -> bool:
        """Fetch and evaluate origin robots.txt once per origin."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            robots_url = f"{origin}/robots.txt"
            parser: Optional[RobotFileParser] = None
            try:
                self._throttle(parsed.netloc.lower())
                resp = self._client.get(robots_url)
                if resp.status_code == 200:
                    rp = RobotFileParser()
                    rp.set_url(robots_url)
                    rp.parse(resp.text.splitlines())
                    parser = rp
                elif resp.status_code in {401, 403}:
                    rp = RobotFileParser()
                    rp.disallow_all = True
                    parser = rp
            except httpx.HTTPError as exc:
                logger.warning("Could not fetch %s (%s); proceeding cautiously.", robots_url, exc)
            self._robots_cache[origin] = parser

        cached = self._robots_cache[origin]
        if cached is None:
            return True
        return cached.can_fetch(self.user_agent, url)

    def get(
        self,
        url: str,
        *,
        extra_headers: Optional[dict[str, str]] = None,
        check_robots: bool = True,
        retries: int = 1,
    ) -> httpx.Response:
        """Perform a polite GET request after robots.txt and rate-limit checks, retrying transient timeouts once."""
        if check_robots and not self.can_fetch(url):
            raise PermissionError(f"Blocked by robots.txt: {url}")
        domain = urlparse(url).netloc.lower()
        attempts = max(1, retries + 1)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            self._throttle(domain)
            try:
                response = self._client.get(url, headers=extra_headers or None)
                response.raise_for_status()
                return response
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    logger.warning("Timeout fetching %s (%s); retrying once cleanly...", url, exc)
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in {502, 503, 504} and attempt < attempts - 1:
                    logger.warning(
                        "Transient HTTP %s for %s; retrying once cleanly...",
                        exc.response.status_code,
                        url,
                    )
                    continue
                raise
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# Source Collectors (html_list, rss_feed, api_json, freejobalert_api)
# ---------------------------------------------------------------------------


def collect_html_list(
    source: dict[str, Any],
    client: PoliteHttpClient,
    limit: int,
    *,
    skip_urls: Optional[set[str]] = None,
) -> list[CandidateItem]:
    """Discover recruitment or admission announcements from a government HTML page."""
    source_url = source["url"]
    resp = client.get(source_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    selectors = source.get("selectors") or {}
    item_selector = selectors.get("item_selector", "a[href]")
    link_selector = selectors.get("link_selector", "a[href]")
    title_selector = selectors.get("title_selector")

    include_patterns = [p.lower() for p in source.get("include_url_patterns", [])]
    exclude_patterns = [p.lower() for p in source.get("exclude_url_patterns", [])]
    include_title_keywords = [kw.lower() for kw in source.get("include_title_keywords", [])]
    exclude_title_keywords = [kw.lower() for kw in source.get("exclude_title_keywords", [])]

    containers = soup.select(item_selector)
    candidates: list[CandidateItem] = []
    seen_local: set[str] = set()

    for node in containers:
        links_in_node = [
            a for a in node.find_all("a", href=True)
            if not str(a["href"]).strip().startswith(("#", "javascript:", "mailto:", "tel:"))
        ]
        if node.name == "a" and node.has_attr("href"):
            link_el = node
        else:
            candidates_in_node = node.select(link_selector) or links_in_node
            link_el = None
            for cand_a in candidates_in_node:
                cand_href = str(cand_a.get("href", "")).strip()
                if not cand_href or cand_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                cand_resolved = normalize_url(urljoin(str(resp.url), cand_href)).lower()
                if exclude_patterns and any(pat in cand_resolved for pat in exclude_patterns):
                    continue
                if include_patterns and not any(pat in cand_resolved for pat in include_patterns):
                    continue
                link_el = cand_a
                break
        if not link_el or not link_el.has_attr("href"):
            continue
        raw_href = str(link_el["href"]).strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        resolved_url = normalize_url(urljoin(str(resp.url), raw_href))
        if not is_valid_http_url(resolved_url) or resolved_url in seen_local:
            continue
        if skip_urls and resolved_url in skip_urls:
            seen_local.add(resolved_url)
            continue

        lowered_url = resolved_url.lower()
        if include_patterns and not any(pat in lowered_url for pat in include_patterns):
            continue
        if exclude_patterns and any(pat in lowered_url for pat in exclude_patterns):
            continue

        if title_selector and node is not link_el:
            title_node = node.select_one(title_selector)
            title_text = title_node.get_text(" ", strip=True) if title_node else ""
        else:
            title_text = link_el.get_text(" ", strip=True)

        if not title_text:
            title_text = node.get_text(" ", strip=True)[:220]
        if len(title_text) < 4:
            continue
        lowered_title = title_text.lower()
        if include_title_keywords and not any(kw in lowered_title for kw in include_title_keywords):
            continue
        if exclude_title_keywords and any(kw in lowered_title for kw in exclude_title_keywords):
            continue

        container_context = ""
        if node is not link_el:
            node_links_text = "\n".join(
                f"- {a.get_text(' ', strip=True)}: {normalize_url(urljoin(str(resp.url), str(a['href'])))}"
                for a in links_in_node[:8]
            )
            container_context = (
                f"Listing Card Text:\n{node.get_text(' ', strip=True)[:1200]}\nListing Links:\n{node_links_text}"
            )

        seen_local.add(resolved_url)
        candidates.append(
            CandidateItem(
                url=resolved_url,
                source_id=source["id"],
                source_domain=source.get("source_domain") or extract_domain(resolved_url),
                title_hint=title_text[:220],
                default_organization=source.get("default_organization"),
                default_department=source.get("default_department"),
                extra_context=container_context[:3000],
            )
        )
        if len(candidates) >= limit:
            break

    return candidates


def collect_admission_hub(
    source: dict[str, Any],
    client: PoliteHttpClient,
    limit: int,
    *,
    portal_filter: Optional[str] = None,
    skip_urls: Optional[set[str]] = None,
    failed_portals_out: Optional[list[dict[str, str]]] = None,
) -> list[CandidateItem]:
    """Collect active admission/entrance notices across all configured Indian conducting organizations."""
    raw_portals = [p for p in source.get("portals", []) if isinstance(p, dict) and p.get("enabled", True)]
    if portal_filter and portal_filter.strip():
        tokens = [t.strip().lower() for t in portal_filter.split(",") if t.strip()]
        filtered: list[dict[str, Any]] = []
        for portal in raw_portals:
            haystack = " ".join([
                str(portal.get("id") or ""),
                str(portal.get("name") or ""),
                str(portal.get("short_name") or ""),
                str(portal.get("default_organization") or ""),
                str(portal.get("source_domain") or ""),
                str(portal.get("category") or ""),
            ]).lower()
            if any(
                tok == str(portal.get("id") or "").lower() or tok in haystack
                for tok in tokens
            ):
                filtered.append(portal)
        raw_portals = filtered
        if not raw_portals:
            raise ValueError(f"No admission organization matched filter '{portal_filter}'.")

    max_items = int(limit or source.get("max_items") or 20)
    per_portal_limit = max(2, (max_items + len(raw_portals) - 1) // max(1, len(raw_portals)))
    if len(raw_portals) == 1:
        per_portal_limit = max_items

    candidates: list[CandidateItem] = []
    seen_urls_in_hub: set[str] = set()
    succeeded_portals = 0
    last_exc: Optional[Exception] = None

    for portal in raw_portals:
        if len(candidates) >= max_items:
            break
        portal_id = str(portal.get("id") or "admission-portal")
        portal_cfg = dict(portal)
        portal_cfg.setdefault("default_department", "Admissions & Entrances")
        try:
            portal_cands = collect_html_list(
                portal_cfg,
                client,
                per_portal_limit,
                skip_urls=skip_urls,
            )
            succeeded_portals += 1
        except Exception as exc:
            last_exc = exc
            err_line = str(exc).splitlines()[0][:200]
            if failed_portals_out is not None:
                failed_portals_out.append(
                    {
                        "endpoint": f"portal:{portal_id}",
                        "code": "",
                        "name": str(portal.get("short_name") or portal.get("default_organization") or portal_id),
                        "error": err_line,
                    }
                )
            logger.warning("Skipping admission organization '%s' after error (%s).", portal_id, exc)
            continue

        for cand in portal_cands:
            if len(candidates) >= max_items:
                break
            if cand.url in seen_urls_in_hub:
                continue
            seen_urls_in_hub.add(cand.url)
            org_name = portal.get("default_organization") or portal.get("name") or portal_id
            adm_ctx = (
                f"Discovery Channel: Unified Admission Hub ({org_name})\n"
                f"Organization Category: {portal.get('category', 'Entrance / University Admission')}\n"
                "Guidance: If this notice invites student applications for academic admission, entrance examination, "
                "or counselling, classify post_type='admission' and extract course_level, exam_date, important_dates, fee_structure, and eligibility_bullets."
            )
            cand.source_id = source["id"]
            cand.default_department = "Admissions & Entrances"
            cand.extra_context = f"{adm_ctx}\n{cand.extra_context}".strip()[:3000]
            candidates.append(cand)

    if succeeded_portals == 0 and last_exc is not None:
        raise last_exc
    return candidates


def _resolve_freejobalert_endpoints(source: dict[str, Any], state_override: Optional[str] = None) -> list[str]:
    """Expand configured FreeJobAlert endpoints or --state overrides (including state:ALL)."""
    seen_urls_ep: set[str] = set()
    all_unique_states: list[str] = []
    for code, (_, url) in STATE_ENDPOINTS.items():
        if url not in seen_urls_ep:
            seen_urls_ep.add(url)
            all_unique_states.append(f"state:{code}")

    if state_override and state_override.strip():
        raw = state_override.strip().upper()
        if raw in {"ALL", "STATE:ALL"}:
            return all_unique_states
        eps: list[str] = []
        for token in raw.split(","):
            code = token.strip().removeprefix("STATE:")
            if code in STATE_ENDPOINTS:
                eps.append(f"state:{code}")
            elif token.strip():
                eps.append(token.strip().lower())
        return eps or all_unique_states

    raw_list = source.get("endpoints")
    if isinstance(raw_list, list) and raw_list:
        expanded: list[str] = []
        for entry in raw_list:
            ep_str = str(entry).strip()
            if ep_str.upper() in {"ALL", "STATE:ALL"}:
                for st in all_unique_states:
                    if st not in expanded:
                        expanded.append(st)
            elif ep_str and ep_str not in expanded:
                expanded.append(ep_str)
        if expanded:
            return expanded

    single = str(source.get("endpoint") or "latest-notifications").strip()
    if single.upper() in {"ALL", "STATE:ALL"}:
        return all_unique_states
    return [single]


def collect_freejobalert_api(
    source: dict[str, Any],
    client: PoliteHttpClient,
    limit: int,
    seen_urls: dict[str, Any],
    pending_items: dict[str, Any],
    today_utc: date,
    existing_apply_urls: set[str],
    existing_advt_codes: set[str],
    limit_override: Optional[int] = None,
    state_override: Optional[str] = None,
    failed_endpoints_out: Optional[list[dict[str, str]]] = None,
) -> list[CandidateItem]:
    """Discover new announcements via FreeJobAlert API (all-India + state-wise),
    then extract outbound official government URLs for primary verification."""
    endpoints = _resolve_freejobalert_endpoints(source, state_override=state_override)
    api_env_var = source.get("api_base_url_env")
    api_base_url = os.getenv(api_env_var, "").strip() if api_env_var else ""
    max_items = int(limit_override) if limit_override is not None else int(source.get("max_items") or limit)

    existing_pending_discovery = {
        str(item.get("discovery_url")) for item in pending_items.values() if isinstance(item, dict)
    }

    multi_ep = len(endpoints) > 1
    per_endpoint_quota = (
        max(1, (max_items + len(endpoints) - 1) // len(endpoints))
        if multi_ep
        else max_items
    )
    fetch_cap_per_ep = max(per_endpoint_quota * 4, 10) if multi_ep else max(max_items * 3, 15)

    candidates: list[CandidateItem] = []
    seen_detail_in_run: set[str] = set()
    succeeded_endpoints = 0
    last_endpoint_exc: Optional[Exception] = None

    for endpoint in endpoints:
        if len(candidates) >= max_items:
            break

        state_code: Optional[str] = (
            endpoint.split(":", 1)[1].upper() if endpoint.upper().startswith("STATE:") else None
        )
        state_name: Optional[str] = (
            STATE_ENDPOINTS[state_code][0] if state_code and state_code in STATE_ENDPOINTS else None
        )

        try:
            rows = fetch_freejobalert_endpoint(
                endpoint,
                client,
                limit=fetch_cap_per_ep,
                api_base_url=api_base_url or None,
                today_utc=today_utc,
            )
            succeeded_endpoints += 1
        except Exception as exc:
            last_endpoint_exc = exc
            err_line = str(exc).splitlines()[0][:200]
            if failed_endpoints_out is not None:
                failed_endpoints_out.append(
                    {
                        "endpoint": endpoint,
                        "code": state_code or "",
                        "name": state_name or endpoint,
                        "error": err_line,
                    }
                )
            logger.warning(
                "Skipping FreeJobAlert endpoint '%s' after error (%s); continuing with remaining endpoints.",
                endpoint,
                exc,
            )
            continue

        added_from_ep = 0
        for row in rows:
            if len(candidates) >= max_items:
                break
            if multi_ep and added_from_ep >= per_endpoint_quota:
                break

            detail_url = normalize_url(row.detail_url)
            if detail_url in seen_detail_in_run or detail_url in seen_urls or detail_url in existing_pending_discovery:
                continue
            seen_detail_in_run.add(detail_url)
            if row.advt_no and row.advt_no.strip().lower() in existing_advt_codes:
                logger.info("Skipping duplicate advertisement '%s' (%s) already in registry.", row.advt_no, row.post_name)
                continue

            try:
                detail_resp = client.get(detail_url)
                links = extract_official_links_from_article(detail_resp.text, str(detail_resp.url))
            except Exception as exc:
                logger.warning("Could not fetch FreeJobAlert article %s: %s", detail_url, exc)
                continue

            target_official_url = links.official_pdf_url or links.official_website_url or links.official_apply_url
            if not target_official_url:
                logger.info("No outbound official URL found on %s; skipping.", detail_url)
                continue

            apply_url_hint = normalize_url(links.official_apply_url or links.official_website_url or target_official_url)
            if apply_url_hint in existing_apply_urls or normalize_url(target_official_url) in existing_apply_urls:
                logger.info("Skipping duplicate official URL '%s' (%s) already in registry.", apply_url_hint, row.post_name)
                continue

            context_lines = [
                f"Discovery Feed: FreeJobAlert ({endpoint})",
            ]
            if state_name:
                context_lines.append(f"State / Region: {state_name}, India")
            context_lines.extend([
                f"Board / Agency: {row.post_board}",
                f"Exam / Post Name: {row.post_name}",
                f"Qualification Listed: {row.qualification}",
                f"Advt No: {row.advt_no or 'N/A'}",
                f"Last Date: {row.last_date or 'N/A'}",
                f"Official Apply URL: {apply_url_hint}",
                f"Official PDF URL: {links.official_pdf_url or 'N/A'}",
            ])
            candidates.append(
                CandidateItem(
                    url=normalize_url(target_official_url),
                    source_id=source["id"],
                    source_domain=extract_domain(apply_url_hint) or extract_domain(target_official_url),
                    title_hint=row.post_name,
                    posted_date_hint=row.post_date,
                    deadline_hint=row.last_date,
                    default_organization=row.post_board,
                    qualification_hint=row.qualification,
                    advt_no_hint=row.advt_no,
                    extra_context="\n".join(context_lines),
                    discovery_url=detail_url,
                    official_apply_url_hint=normalize_url(apply_url_hint),
                    official_pdf_url_hint=normalize_url(links.official_pdf_url) if links.official_pdf_url else None,
                    allow_pending_queue=True,
                    state_code=state_code,
                )
            )
            added_from_ep += 1

    if succeeded_endpoints == 0 and last_endpoint_exc is not None:
        raise last_endpoint_exc

    return candidates


def _strip_xml_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def collect_rss_feed(
    source: dict[str, Any],
    client: PoliteHttpClient,
    limit: int,
) -> list[CandidateItem]:
    """Discover announcements from RSS 2.0 or Atom feeds."""
    resp = client.get(source["url"])
    root = ET.fromstring(resp.text)
    keywords = [kw.lower() for kw in source.get("filter_keywords", [])]

    entries: list[ET.Element] = []
    for elem in root.iter():
        local_name = _strip_xml_ns(elem.tag)
        if local_name in {"item", "entry"}:
            entries.append(elem)

    candidates: list[CandidateItem] = []
    for entry in entries:
        title = ""
        link = ""
        summary = ""
        pub_date = ""
        origin_domain = source.get("source_domain", "")
        org_hint = source.get("default_organization")

        for child in entry:
            name = _strip_xml_ns(child.tag)
            text_val = (child.text or "").strip()
            if name == "title" and not title:
                title = text_val
            elif name == "link":
                href = child.attrib.get("href", "").strip()
                link = href or text_val
            elif name in {"description", "summary", "content"} and not summary:
                summary = BeautifulSoup(text_val, "html.parser").get_text(" ", strip=True)
            elif name in {"pubdate", "published", "updated"} and not pub_date:
                pub_date = text_val
            elif name == "source":
                src_url = child.attrib.get("url", "").strip()
                if is_valid_http_url(src_url):
                    origin_domain = extract_domain(src_url)
                if text_val and not org_hint:
                    org_hint = text_val

        normalized = normalize_url(link)
        if not is_valid_http_url(normalized):
            continue

        haystack = f"{title} {summary}".lower()
        if keywords and not any(kw in haystack for kw in keywords):
            continue

        candidates.append(
            CandidateItem(
                url=normalized,
                source_id=source["id"],
                source_domain=origin_domain or extract_domain(normalized),
                title_hint=title[:220],
                posted_date_hint=parse_to_iso_date(pub_date),
                default_organization=org_hint,
                default_department=source.get("default_department"),
                summary_hint=summary[:1000] if summary else None,
            )
        )
        if len(candidates) >= limit:
            break

    return candidates


def _get_nested_path(payload: Any, dotted_path: str) -> Any:
    """Resolve a dotted dictionary path safely."""
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def collect_api_json(
    source: dict[str, Any],
    client: PoliteHttpClient,
    limit: int,
) -> list[CandidateItem]:
    """Discover recruitment announcements from a structured government JSON API."""
    required_env = source.get("required_env", [])
    missing_env = [var for var in required_env if not os.getenv(var)]
    if missing_env:
        logger.info(
            "Skipping source '%s': missing environment variable(s) %s",
            source["id"],
            ", ".join(missing_env),
        )
        return []

    custom_headers: dict[str, str] = {}
    for header_name, env_or_literal in (source.get("headers_env") or {}).items():
        if env_or_literal in os.environ:
            custom_headers[header_name] = os.environ[env_or_literal]
        elif header_name.lower() == "host":
            custom_headers[header_name] = env_or_literal

    resp = client.get(source["url"], extra_headers=custom_headers, check_robots=False)
    payload = resp.json()

    items_path = source.get("items_path", "")
    raw_items = _get_nested_path(payload, items_path) if items_path else payload
    if not isinstance(raw_items, list):
        logger.warning("Source '%s' returned unexpected JSON structure at '%s'.", source["id"], items_path)
        return []

    field_map = source.get("field_map", {})
    candidates: list[CandidateItem] = []

    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            continue
        url_val = _get_nested_path(item, field_map.get("url", "url"))
        if not isinstance(url_val, str) or not is_valid_http_url(url_val):
            continue

        title_val = _get_nested_path(item, field_map.get("title", "title"))
        org_val = _get_nested_path(item, field_map.get("organization", "organization"))
        dept_val = _get_nested_path(item, field_map.get("department", "department"))
        posted_val = _get_nested_path(item, field_map.get("posted_date", "posted_date"))
        deadline_val = _get_nested_path(item, field_map.get("deadline", "deadline"))
        summary_val = _get_nested_path(item, field_map.get("summary", "summary"))

        candidates.append(
            CandidateItem(
                url=normalize_url(url_val),
                source_id=source["id"],
                source_domain=source.get("source_domain") or extract_domain(url_val),
                title_hint=str(title_val or "").strip()[:220],
                posted_date_hint=parse_to_iso_date(str(posted_val)) if posted_val else None,
                deadline_hint=parse_to_iso_date(str(deadline_val)) if deadline_val else None,
                default_organization=str(org_val).strip() if org_val else source.get("default_organization"),
                default_department=str(dept_val).strip() if dept_val else source.get("default_department"),
                summary_hint=str(summary_val).strip()[:1500] if summary_val else None,
                extra_context=json.dumps(item, ensure_ascii=False)[:8000],
            )
        )

    return candidates


# ---------------------------------------------------------------------------
# Content Fetching & PDF Text / Scanned PDF Handling
# ---------------------------------------------------------------------------


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pypdf; returns empty string for scanned PDFs."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks: list[str] = []
        for page in reader.pages[:MAX_PDF_TEXT_PAGES]:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text.strip())
        return "\n\n".join(chunks).strip()
    except Exception as exc:
        logger.warning("pypdf failed to extract text: %s", exc)
        return ""


def build_fetched_from_pdf_bytes(pdf_bytes: bytes, pdf_url: str, final_url: str) -> FetchedContent:
    """Create FetchedContent from raw PDF bytes (using Gemini inline bytes if scanned)."""
    clipped = pdf_bytes[:MAX_PDF_BYTES]
    pdf_text = extract_pdf_text(clipped)
    needs_upload = len(re.sub(r"\s+", "", pdf_text)) < MIN_EXTRACTED_PDF_CHARS
    return FetchedContent(
        final_url=final_url,
        pdf_url=pdf_url,
        pdf_text=pdf_text[:MAX_HTML_TEXT_CHARS],
        raw_pdf_bytes=clipped if needs_upload else None,
        needs_raw_pdf_upload=needs_upload,
    )


def _pick_official_pdf_link(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Identify the most relevant official recruitment PDF linked on a notice page."""
    priority_terms = ("notification", "advt", "advertisement", "circular", "notice", "recruitment", "cen", "detailed")
    best_url: Optional[str] = None
    best_score = -1

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if ".pdf" not in href.lower():
            continue
        resolved = normalize_url(urljoin(base_url, href))
        if not is_valid_http_url(resolved):
            continue
        label = f"{anchor.get_text(' ', strip=True)} {resolved}".lower()
        score = sum(2 for term in priority_terms if term in label)
        if extract_domain(resolved) == extract_domain(base_url):
            score += 1
        if score > best_score:
            best_score = score
            best_url = resolved

    return best_url


def fetch_candidate_content(
    candidate: CandidateItem,
    client: PoliteHttpClient,
) -> FetchedContent:
    """Fetch notice HTML and linked official PDF bytes/text politely."""
    resp = client.get(candidate.url, extra_headers=candidate.headers or None)
    final_url = normalize_url(str(resp.url))
    content_type = resp.headers.get("content-type", "").lower()

    if final_url.lower().endswith(".pdf") or "application/pdf" in content_type:
        return build_fetched_from_pdf_bytes(resp.content, final_url, final_url)

    soup = BeautifulSoup(resp.text, "html.parser")
    linked_pdf_url = _pick_official_pdf_link(soup, final_url) or candidate.official_pdf_url_hint

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    visible_text = soup.get_text("\n", strip=True)[:MAX_HTML_TEXT_CHARS]

    pdf_text = ""
    raw_pdf_bytes: Optional[bytes] = None
    needs_upload = False

    if linked_pdf_url:
        try:
            pdf_resp = client.get(linked_pdf_url)
            if len(pdf_resp.content) <= MAX_PDF_BYTES:
                raw_bytes = pdf_resp.content
                pdf_text = extract_pdf_text(raw_bytes)
                if len(re.sub(r"\s+", "", pdf_text)) < MIN_EXTRACTED_PDF_CHARS:
                    raw_pdf_bytes = raw_bytes
                    needs_upload = True
        except PermissionError:
            if len(visible_text) < MIN_USEFUL_HTML_CHARS:
                raise
            logger.warning("Linked PDF disallowed by robots.txt (%s); using HTML notice text.", linked_pdf_url)
        except Exception as exc:
            if len(visible_text) < MIN_USEFUL_HTML_CHARS:
                raise RuntimeError(f"Official PDF download failed ({exc}) and HTML page has insufficient content.") from exc
            logger.warning("Could not fetch linked PDF %s: %s", linked_pdf_url, exc)

    if not pdf_text and not needs_upload and len(visible_text) < MIN_USEFUL_HTML_CHARS:
        raise RuntimeError(
            f"Insufficient static content ({len(visible_text)} chars, likely SPA/portal) and no downloadable PDF."
        )

    return FetchedContent(
        final_url=final_url,
        html_text=visible_text,
        pdf_url=linked_pdf_url,
        pdf_text=pdf_text[:MAX_HTML_TEXT_CHARS],
        raw_pdf_bytes=raw_pdf_bytes,
        needs_raw_pdf_upload=needs_upload,
    )


# ---------------------------------------------------------------------------
# Gemini Structured Extraction
# ---------------------------------------------------------------------------


def build_extraction_prompt(candidate: CandidateItem, fetched: FetchedContent) -> str:
    """Build strict zero-hallucination extraction instructions."""
    parts = [
        "You are an automated public-sector recruitment data extractor.",
        "Follow these strict rules without exception:",
        "1. ZERO HALLUCINATION: Never guess, estimate, or extrapolate missing deadlines, "
        "salaries, vacancy counts, age limits, fees, or URLs. Output null if absent.",
        "2. VALUE-ADD SYNTHESIS: Never copy long raw paragraphs verbatim. Synthesize concise, "
        "factual summaries, bullet points, important dates, vacancy rows, and application steps.",
        "3. DATES: Format application_deadline and posted_date strictly as YYYY-MM-DD when explicitly stated; "
        "format every exact date inside important_dates strictly as DD Mon YYYY (example: '12 Dec 2026').",
        "4. PER-POST ELIGIBILITY: Extract eligibility points in eligibility_bullets for EVERY post listed in the notice "
        "(use 'Post Name: <qualification/age requirement>' prefix when multiple posts have distinct rules) and set eligibility on each vacancy_breakdown row when stated.",
        "5. ORGANIZED FEE & EXTRA LINKS: Populate category-wise fee_structure ({category, amount}) whenever fees are detailed, "
        "and include any extra real official links (syllabus PDF, admit card, answer key, result, exam notice) in important_links.",
        "6. CLASSIFICATION: Set post_type='admission' ONLY for entrance exams, university/college/school/polytechnic/ITI admissions "
        "(JEE, NEET, CUET, B.Ed, JNVST, NVS admissions); set post_type='job' for salaried/apprentice public-sector recruitments. "
        "Set is_valid_job_notice=false if purely unrelated news/tender/ad or standalone result notice without recruitment/admission opening.",
        "",
        f"Source ID: {candidate.source_id}",
        f"Notice URL: {fetched.final_url}",
        f"Source Domain: {candidate.source_domain}",
    ]
    if candidate.title_hint:
        parts.append(f"Listing Title Hint: {candidate.title_hint}")
    if candidate.default_organization:
        parts.append(f"Default Organization: {candidate.default_organization}")
    if candidate.default_department:
        parts.append(f"Default Department: {candidate.default_department}")
    if candidate.posted_date_hint:
        parts.append(f"Feed Posted Date Hint: {candidate.posted_date_hint}")
    if candidate.deadline_hint:
        parts.append(f"Feed Deadline Hint: {candidate.deadline_hint}")
    if candidate.official_apply_url_hint:
        parts.append(f"Official Apply URL Hint: {candidate.official_apply_url_hint}")
    if fetched.pdf_url:
        parts.append(f"Linked Official PDF URL: {fetched.pdf_url}")
    if candidate.summary_hint:
        parts.append(f"\n--- FEED SUMMARY ---\n{candidate.summary_hint}")
    if candidate.extra_context:
        parts.append(f"\n--- LISTING CONTEXT ---\n{candidate.extra_context}")
    if fetched.html_text:
        parts.append(f"\n--- PAGE TEXT ---\n{fetched.html_text}")
    if fetched.pdf_text:
        parts.append(f"\n--- EXTRACTED PDF TEXT ---\n{fetched.pdf_text}")
    return "\n".join(parts)


def _get_client_for_secret(secret: str) -> Any:
    """Return a cached google-genai Client for an API key secret."""
    if secret not in _GEMINI_CLIENT_CACHE:
        from google import genai
        from google.genai import types

        _GEMINI_CLIENT_CACHE[secret] = genai.Client(
            api_key=secret,
            http_options=types.HttpOptions(timeout=45000),
        )
    return _GEMINI_CLIENT_CACHE[secret]


def extract_with_gemini(
    candidate: CandidateItem,
    fetched: FetchedContent,
    genai_client: Any = None,
    model_name: str = DEFAULT_GEMINI_MODEL,
) -> Optional[GeminiJobExtraction]:
    """Call Google GenAI SDK with Pydantic structured output, model fallback, and automatic multi-key failover."""
    from google.genai import types

    prompt = build_extraction_prompt(candidate, fetched)
    contents: list[Any] = [prompt]

    if fetched.needs_raw_pdf_upload and fetched.raw_pdf_bytes:
        logger.info("Uploading raw PDF bytes to Gemini for scanned/image PDF: %s", fetched.pdf_url)
        contents.insert(
            0,
            types.Part.from_bytes(
                data=fetched.raw_pdf_bytes,
                mime_type="application/pdf",
            ),
        )

    base_models = [model_name] + [m for m in FALLBACK_GEMINI_MODELS if m != model_name]
    active_keys = get_ordered_active_keys()
    key_slots: list[tuple[Optional[dict[str, Any]], Any]] = []
    for entry in active_keys:
        secret = str(entry.get("api_key") or "").strip()
        if secret:
            key_slots.append((entry, _get_client_for_secret(secret)))

    if not key_slots and genai_client is not None:
        key_slots.append((None, genai_client))

    if not key_slots:
        push_admin_notification(
            "action_required",
            "No Active Gemini API Keys Available",
            "All configured Gemini keys are exhausted, disabled, or invalid. Add or reset keys at /keys.",
            action_tab="runner",
        )
        raise RuntimeError(
            "No active Gemini API keys available in scripts/gemini_keys.json or .env GEMINI_API_KEY."
        )

    response = None
    last_exc: Optional[Exception] = None

    for key_idx, (key_entry, client_obj) in enumerate(key_slots):
        key_id = str(key_entry["id"]) if key_entry else None
        key_label = str(key_entry.get("label") or key_id) if key_entry else "Default Key"
        ex_models = key_entry.get("exhausted_models", {}) if isinstance(key_entry, dict) else {}
        if isinstance(ex_models, dict) and ex_models:
            non_exhausted = [m for m in base_models if m not in ex_models]
            models_to_try = non_exhausted if non_exhausted else list(base_models)
        else:
            models_to_try = list(base_models)

        saw_429_on_key = False
        saw_invalid_on_key = False

        for idx, candidate_model in enumerate(models_to_try):
            try:
                response = client_obj.models.generate_content(
                    model=candidate_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=GeminiJobExtraction,
                        temperature=0.1,
                    ),
                )
                if key_id:
                    tokens = int(getattr(getattr(response, "usage_metadata", None), "total_token_count", 0) or 0)
                    record_key_success(key_id, tokens=tokens)
                break
            except Exception as exc:
                last_exc = exc
                err_msg = str(exc)
                first_line = err_msg.splitlines()[0][:140]
                is_auth_err = any(
                    tok in err_msg for tok in ("API_KEY_INVALID", "API key not valid", "401", "403")
                ) or ("400" in err_msg and "key" in err_msg.lower())
                is_quota_err = "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg
                is_transient = "503" in err_msg or "UNAVAILABLE" in err_msg

                if is_auth_err:
                    saw_invalid_on_key = True
                    if key_id:
                        mark_key_invalid(key_id, first_line)
                    logger.warning("Gemini key '%s' rejected (%s); switching key if available.", key_label, first_line)
                    push_admin_notification(
                        "action_required",
                        f"Gemini Key Invalid ({key_label})",
                        f"Key '{key_label}' returned an auth error ({first_line}). Check keys at /keys.",
                        action_tab="runner",
                    )
                    break

                if is_quota_err:
                    saw_429_on_key = True
                    if key_id:
                        record_key_model_429(key_id, candidate_model, err_msg)
                    if idx < len(models_to_try) - 1:
                        logger.warning(
                            "Key '%s' model '%s' hit 429 quota (%s); trying fallback '%s'.",
                            key_label,
                            candidate_model,
                            first_line,
                            models_to_try[idx + 1],
                        )
                        time.sleep(1.2)
                        continue
                    break

                if is_transient and idx < len(models_to_try) - 1:
                    logger.warning(
                        "Key '%s' model '%s' unavailable (%s); trying fallback '%s'.",
                        key_label,
                        candidate_model,
                        first_line,
                        models_to_try[idx + 1],
                    )
                    time.sleep(1.5)
                    continue
                raise

        if response is not None:
            break

        if not saw_invalid_on_key and saw_429_on_key and key_id:
            err_tail = str(last_exc).splitlines()[0][:160] if last_exc else "429 RESOURCE_EXHAUSTED"
            mark_key_exhausted(key_id, f"Quota exhausted ({err_tail})", cooldown_minutes=60)
            has_next = key_idx < len(key_slots) - 1
            logger.warning(
                "Gemini key '%s' exhausted across fallback models (%s). %s",
                key_label,
                err_tail,
                "Failover switching to next available key..." if has_next else "No backup keys remaining.",
            )
            push_admin_notification(
                "action_required",
                f"Gemini Key Exhausted: {key_label}",
                (
                    f"Key '{key_label}' hit 429 quota limits and switched to backup key."
                    if has_next
                    else f"Key '{key_label}' hit 429 quota limits and no backup keys remain. Add keys at /keys."
                ),
                action_tab="runner",
            )
            if has_next:
                time.sleep(1.0)
                continue

        if key_idx < len(key_slots) - 1 and (saw_invalid_on_key or saw_429_on_key or (last_exc and ("503" in str(last_exc) or "UNAVAILABLE" in str(last_exc)))):
            continue
        break

    if response is None and last_exc is not None:
        raise last_exc

    if getattr(response, "parsed", None) is not None:
        parsed = response.parsed
        if isinstance(parsed, GeminiJobExtraction):
            return parsed
        return GeminiJobExtraction.model_validate(parsed)

    raw_text = getattr(response, "text", None)
    if raw_text:
        return GeminiJobExtraction.model_validate_json(raw_text)
    return None


# ---------------------------------------------------------------------------
# Validation, Archival, Pending PDF Queue & Persistence
# ---------------------------------------------------------------------------


STATE_CLASSIFIER_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("Andhra Pradesh", ("andhra pradesh", "appsc", "amaravati", "visakhapatnam", "vijayawada", "tirupati", "guntur"), (".ap.gov.in", "psc.ap.gov.in")),
    ("Arunachal Pradesh", ("arunachal pradesh", "itanagar"), ("arunachalpradesh.gov.in",)),
    ("Assam", ("assam", "apsc", "dispur", "guwahati"), ("assam.gov.in", "apsc.nic.in")),
    ("Bihar", ("bihar", "bpsc", "bssc", "csbc", "btsc", "brlps", "jeevika", "patna", "rohtas", "gaya", "muzaffarpur"), (".bihar.gov.in", "brlps.in", "rohtas.nic.in")),
    ("Chhattisgarh", ("chhattisgarh", "cgpsc", "cgvyapam", "csptcl", "raipur"), (".cg.gov.in", "cgstate.gov.in", "cspdcl.co.in")),
    ("Delhi", ("delhi", "dsssb", "nct of delhi", "lok nayak hospital", "new delhi"), ("delhi.gov.in",)),
    ("Goa", ("goa", "goapsc", "panaji"), ("goa.gov.in",)),
    ("Gujarat", ("gujarat", "gpsc", "gsssb", "gandhinagar", "ahmedabad", "surat", "vadodara"), (".gujarat.gov.in", "gujarat.gov.in")),
    ("Haryana", ("haryana", "hssc", "hpsc", "sonepat", "sonipat", "gurugram", "faridabad", "hisar", "rohtak", "panchkula", "karnal"), (".haryana.gov.in", "hssc.gov.in", "cracollegesonepat.org")),
    ("Himachal Pradesh", ("himachal pradesh", "hppsc", "hprca", "hpscb", "shimla", "parwanoo", "solan", "baddi", "kangra", "mandi"), (".hp.gov.in", "hpscb.bank.in", "parwanoourban.bank.in")),
    ("Jammu & Kashmir", ("jammu & kashmir", "jammu and kashmir", "jkssb", "jkpsc", "j&k bank", "j k bank", "srinagar", "jammu"), ("jkssb.nic.in", "jkpsc.nic.in", "jkb.bank.in", ".jk.gov.in")),
    ("Jharkhand", ("jharkhand", "jpsc", "jssc", "jrhms", "ranchi", "jamshedpur", "dhanbad", "bokaro"), (".jharkhand.gov.in", "jssc.nic.in")),
    ("Karnataka", ("karnataka", "kpsc", "kea", "karunadu", "mysuru", "hubballi", "mangaluru", "belagavi"), ("karnataka.gov.in",)),
    ("Kerala", ("kerala", "kerala psc", "c-dit", "cmd kerala", "thiruvananthapuram", "kochi", "kozhikode", "thrissur"), ("kerala.gov.in", "cdit.org")),
    ("Madhya Pradesh", ("madhya pradesh", "mppsc", "mpesb", "mp vyapam", "mprdc", "indore", "jabalpur", "gwalior"), (".mp.gov.in", "mponline.gov.in")),
    ("Maharashtra", ("maharashtra", "mpsc", "mahapariksha", "akola", "vasai virar", "vasai-virar", "vvcmc", "nagpur", "pune", "thane", "nashik", "aurangabad"), ("maharashtra.gov.in", "akola.gov.in", "msc.bank.in", "akolajanata.bank.in", "vvcmc.in")),
    ("Manipur", ("manipur", "imphal"), ("manipur.gov.in",)),
    ("Meghalaya", ("meghalaya", "shillong"), ("meghalaya.gov.in",)),
    ("Mizoram", ("mizoram", "aizawl"), ("mizoram.gov.in",)),
    ("Nagaland", ("nagaland", "kohima"), ("nagaland.gov.in",)),
    ("Odisha", ("odisha", "orissa", "opsc", "ossc", "osssc", "bhubaneswar", "cuttack"), ("odisha.gov.in", "ossc.gov.in", "osssc.gov.in")),
    ("Punjab", ("punjab", "psssb", "ppsc", "patiala", "ludhiana", "amritsar", "jalandhar", "mohali"), ("punjab.gov.in", "mbspsu.ac.in")),
    ("Rajasthan", ("rajasthan", "rpsc", "rssb", "rsmssb", "jaipur", "jodhpur", "bikaner", "ajmer", "kota"), ("rajasthan.gov.in",)),
    ("Sikkim", ("sikkim", "gangtok"), ("sikkim.gov.in",)),
    ("Tamil Nadu", ("tamil nadu", "tnpsc", "tnusrb", "mrb tn", "trb tn", "nilgiris", "chennaicorporation", "coimbatore", "madurai", "salem", "tiruchirappalli"), ("tn.gov.in", "chennaicorporation.gov.in", "nilgiris.nic.in")),
    ("Telangana", ("telangana", "tspsc", "tgpsc", "rajanna sircilla", "sircilla", "warangal", "karimnagar", "nizamabad"), ("telangana.gov.in", "gmcrajannasircilla.org")),
    ("Tripura", ("tripura", "agartala"), ("tripura.gov.in",)),
    ("Uttar Pradesh", ("uttar pradesh", "uppsc", "upsssc", "uppbpb", "uppcl", "mirzapur", "lucknow", "varanasi", "prayagraj", "kanpur", "agra", "meerut", "noida", "ghaziabad"), (".up.gov.in", "uppsc.up.nic.in", "upsssc.gov.in", "upanganwadibharti.in")),
    ("Uttarakhand", ("uttarakhand", "ukpsc", "uksssc", "dehradun", "haldwani", "haridwar", "nainital"), (".uk.gov.in", "ukpsc.net.in", "psc.uk.gov.in")),
    ("West Bengal", ("west bengal", "wbpsc", "wbcs", "wbprb", "wbhealth", "kolkata", "howrah", "siliguri", "durgapur"), ("wb.gov.in", "wbhealth.gov.in")),
    ("Chandigarh", ("chandigarh",), ("chandigarh.gov.in",)),
    ("Puducherry", ("puducherry", "pondicherry"), ("py.gov.in",)),
    ("Ladakh", ("ladakh", "leh", "kargil"), ("ladakh.gov.in",)),
]

CENTRAL_INDICATOR_RE = re.compile(
    r"\b("
    r"state bank of india|sbi|bank of baroda|canara bank|central bank of india|"
    r"indian bank|punjab national bank|union bank of india|bank of india|exim bank|"
    r"export-import bank of india|sidbi|sebi|rbi|reserve bank of india|nabard|lic|ibps|"
    r"aiims|all india institute of medical sciences|iit|iim|nit|iiser|upsc|"
    r"staff selection commission|railway|rrb|isro|department of space|drdo|barc|ecil|electronics corporation of india|"
    r"engineers india limited|eil|oil india|ongc|ntpc|bhel|gail|sail|iocl|bpcl|hpcl|"
    r"mazagon dock|munitions india|ordnance factory|esic|epfo|armed forces tribunal|"
    r"kendriya vidyalaya|navodaya|ministry of|government of india|all india|anywhere in india"
    r")\b",
    re.IGNORECASE,
)

STATE_INDICATOR_RE = re.compile(
    r"\b("
    r"appsc|apsc|bpsc|bssc|cgpsc|dsssb|gpsc|gsssb|hpsc|hssc|hppsc|hpscb|jkpsc|jkssb|"
    r"jpsc|jssc|kpsc|kea|mppsc|mpsc|opsc|ossc|osssc|ppsc|psssb|rpsc|rssb|rsmssb|"
    r"tnpsc|tspsc|tgpsc|uppsc|upsssc|ukpsc|uksssc|wbpsc|"
    r"nagar parishad|municipal corporation|municipality|collectorate|gram panchayat|"
    r"zilla parishad|district child protection|state co-?operative|urban co-?operative|"
    r"co-?operative bank|urban health mission|state psu|state civil|government of nct|"
    r"j&k bank|brlps|c-?dit|cmd kerala|state sports university"
    r")\b",
    re.IGNORECASE,
)


def normalize_state_name(raw: Optional[str]) -> Optional[str]:
    """Normalize a state/UT name to canonical Indian state title, or None."""
    if not raw:
        return None
    cleaned = raw.strip().lower()
    if not cleaned or cleaned in {"all india", "see official notification", "anywhere in india", "india"}:
        return None
    for canonical, aliases, _ in STATE_CLASSIFIER_RULES:
        if canonical.lower() == cleaned or any(alias in cleaned for alias in aliases):
            return canonical
    return None


def infer_government_classification(
    *,
    title: str,
    organization: str,
    department: str,
    location: str,
    source_domain: str,
    extra_context: str = "",
    explicit_level: Optional[str] = None,
    explicit_state: Optional[str] = None,
) -> tuple[Literal["central", "state"], Optional[str]]:
    """Deterministically classify a notice as Central vs State Govt (+ canonical state_name)."""
    org_title_domain = f"{organization} {title} {source_domain}"
    combined_all = f"{org_title_domain} {department} {location}"

    matched_state: Optional[str] = normalize_state_name(explicit_state) or normalize_state_name(location)
    if not matched_state and extra_context:
        st_match = re.search(r"State / Region:\s*([^,\n]+)", extra_context, re.IGNORECASE)
        if st_match:
            matched_state = normalize_state_name(st_match.group(1))
    if not matched_state:
        domain_lower = source_domain.lower()
        for canonical, _, domains in STATE_CLASSIFIER_RULES:
            if any(d in domain_lower for d in domains):
                matched_state = canonical
                break
    if not matched_state:
        for canonical, aliases, _ in STATE_CLASSIFIER_RULES:
            if any(re.search(rf"\b{re.escape(alias)}\b", combined_all, re.IGNORECASE) for alias in aliases):
                matched_state = canonical
                break

    if explicit_level in {"central", "state"}:
        level: Literal["central", "state"] = explicit_level  # type: ignore[assignment]
        return (level, matched_state if level == "state" else None)

    if STATE_INDICATOR_RE.search(org_title_domain):
        return "state", matched_state

    if CENTRAL_INDICATOR_RE.search(f"{org_title_domain} {location}"):
        return "central", None

    if department.lower().startswith("state "):
        return "state", matched_state

    loc_lower = location.lower()
    if "all india" in loc_lower or "anywhere in india" in loc_lower:
        return "central", None

    if matched_state:
        return "state", matched_state
    return "central", None


_CPC_LEVEL_TABLE: dict[int, tuple[int, int, str]] = {
    1: (18000, 56900, "Level 1 (7th CPC)"),
    2: (19900, 63200, "Level 2 (7th CPC)"),
    3: (21700, 69100, "Level 3 (7th CPC)"),
    4: (25500, 81100, "Level 4 (7th CPC)"),
    5: (29200, 92300, "Level 5 (7th CPC)"),
    6: (35400, 112400, "Level 6 (7th CPC)"),
    7: (44900, 142400, "Level 7 (7th CPC)"),
    8: (47600, 151100, "Level 8 (7th CPC)"),
    9: (53100, 167800, "Level 9 (7th CPC)"),
    10: (56100, 177500, "Level 10 (7th CPC)"),
    11: (67700, 208700, "Level 11 (7th CPC)"),
    12: (78800, 209200, "Level 12 (7th CPC)"),
    13: (123100, 215900, "Level 13 (7th CPC)"),
    14: (144200, 218200, "Level 14 (7th CPC)"),
}


def infer_salary_fields(
    salary_range: Optional[str],
) -> tuple[
    Optional[int],
    Optional[int],
    Optional[str],
    Literal["up-to-25k", "25k-45k", "45k-70k", "70k-1l", "above-1l", "not-specified"],
]:
    """Derive structured monthly pay min/max, 7th CPC level label, and bracket without hallucination."""
    if not salary_range:
        return None, None, None, "not-specified"
    cleaned = re.sub(r"1,000,00\b", "100000", salary_range.strip())
    lower = cleaned.lower()

    level_nums = [int(m) for m in re.findall(r"(?:level|l)[-\s]*0?(\d{1,2})\b", cleaned, re.IGNORECASE) if 1 <= int(m) <= 14]
    pay_level: Optional[str] = None
    lvl_match = re.search(
        r"((?:academic\s+)?(?:pay\s*(?:matrix\s*)?)?level[-\s]*\d{1,2}[a-z]?(?:\s*(?:to|-|–)\s*(?:level[-\s]*)?\d{1,2}[a-z]?)?)",
        cleaned,
        re.IGNORECASE,
    )
    if lvl_match:
        pay_level = re.sub(r"\s+", " ", lvl_match.group(1)).strip().title().replace("Cpc", "CPC")
        if "cpc" not in pay_level.lower() and "level" in pay_level.lower() and "s-" not in lower:
            pay_level = f"{pay_level} (7th CPC)"
    elif re.search(r"Levels?[-\s]*S-\d+", cleaned, re.IGNORECASE):
        sm = re.search(r"Pay\s*Matrix\s*Levels?\s*S-\d+(?:\s*(?:to|-)\s*S-\d+)?", cleaned, re.IGNORECASE)
        if sm:
            pay_level = sm.group(0).strip()

    money_vals: list[int] = []
    for token in re.findall(r"(?:₹|Rs\.?\s*)?\b(\d{1,2}(?:,\d{2})*(?:,\d{3})|\d{4,7})\b", cleaned):
        num = int(token.replace(",", ""))
        if 4000 <= num <= 400000 and num not in {2025, 2026, 2027}:
            money_vals.append(num)

    s_min: Optional[int] = None
    s_max: Optional[int] = None
    cpc_ref_min: Optional[int] = None
    cpc_ref_max: Optional[int] = None
    if money_vals and "per case" not in lower and "last pay drawn" not in lower:
        s_min = min(money_vals)
        s_max = max(money_vals) if len(money_vals) > 1 and max(money_vals) > s_min else None
    elif level_nums:
        lo, hi = min(level_nums), max(level_nums)
        cpc_ref_min = _CPC_LEVEL_TABLE[lo][0]
        cpc_ref_max = _CPC_LEVEL_TABLE[hi][1]
        if not pay_level:
            pay_level = _CPC_LEVEL_TABLE[lo][2] if lo == hi else f"Level {lo}–{hi} (7th CPC)"

    ref = s_min if (s_min and s_min > 0) else (s_max if (s_max and s_max > 0) else cpc_ref_min)
    if ref is None:
        tier: Literal["up-to-25k", "25k-45k", "45k-70k", "70k-1l", "above-1l", "not-specified"] = "not-specified"
    elif ref <= 25000:
        tier = "up-to-25k"
    elif ref <= 45000:
        tier = "25k-45k"
    elif ref <= 70000:
        tier = "45k-70k"
    elif ref <= 100000:
        tier = "70k-1l"
    else:
        tier = "above-1l"
    return s_min, s_max, pay_level, tier


def infer_authority_type(
    *,
    title: str,
    organization: str,
    department: str,
    source_domain: str,
    gov_level: Literal["central", "state"],
) -> Literal["central", "state", "psu-bank", "defence-police", "teaching"]:
    """Classify issuing authority into SarkariIndex recruitment vertical."""
    org_title_domain = f"{organization} {title} {source_domain}".lower()
    if re.search(r"\b(bank|sbi|bob|canara|exim|sidbi|sebi|rbi|nabard|ibps|ecil|eil|oil india|ongc|ntpc|bhel|gail|sail|dfccil|edcil|c-?dit|cmd)\b", org_title_domain):
        return "psu-bank"
    if re.search(r"\b(police|defence|defense|army|navy|air force|armed forces|ordnance|munitions|advanced weapons|constable|sub-?inspector|cisf|bsf|crpf)\b", org_title_domain):
        return "defence-police"
    if re.search(r"\b(teacher|professor|lecturer|librarian|university|college|kgbv|basic education|pgt|tgt|prt|iim|iit|nit)\b", org_title_domain):
        return "teaching"
    return "state" if gov_level == "state" else "central"


def validate_extracted_job(
    extracted: GeminiJobExtraction,
    candidate: CandidateItem,
    fetched: FetchedContent,
    reserved_slugs: set[str],
    today_utc: date,
) -> tuple[Optional[ValidatedJobRecord], str]:
    """Validate extracted data and reject incomplete or already-expired items."""
    if not extracted.is_valid_job_notice:
        return None, "not a recruitment announcement"

    title = (extracted.title or "").strip()
    organization = (extracted.organization or candidate.default_organization or "").strip()
    if len(title) < 3 or len(organization) < 2:
        return None, "missing required title or organization"

    apply_url = (extracted.official_apply_url or candidate.official_apply_url_hint or "").strip()
    if not is_valid_http_url(apply_url):
        if is_valid_http_url(fetched.final_url) and extract_domain(fetched.final_url) not in {"news.google.com", "freejobalert.com"}:
            apply_url = fetched.final_url
        else:
            return None, "missing valid official_apply_url"

    source_url = fetched.final_url if is_valid_http_url(fetched.final_url) else apply_url

    pdf_url: Optional[str] = None
    if extracted.official_pdf_url and is_valid_http_url(extracted.official_pdf_url):
        pdf_url = normalize_url(extracted.official_pdf_url)
    elif fetched.pdf_url and is_valid_http_url(fetched.pdf_url):
        pdf_url = normalize_url(fetched.pdf_url)
    elif candidate.official_pdf_url_hint and is_valid_http_url(candidate.official_pdf_url_hint):
        pdf_url = normalize_url(candidate.official_pdf_url_hint)

    deadline = parse_to_iso_date(extracted.application_deadline) or candidate.deadline_hint
    if deadline:
        try:
            if date.fromisoformat(deadline) < today_utc:
                return None, f"application_deadline ({deadline}) already passed"
        except ValueError:
            deadline = None

    posted_date = (
        parse_to_iso_date(extracted.posted_date)
        or candidate.posted_date_hint
        or today_utc.isoformat()
    )

    department = (extracted.department or candidate.default_department or "Public Sector Recruitment").strip()
    location = (extracted.location or "See Official Notification").strip()
    qualification = (extracted.qualification_level or "See Official Notification").strip()

    summary = (extracted.summary or candidate.summary_hint or "").strip()
    if len(summary) < 20:
        summary = f"{organization} has issued a recruitment notice for {title}. Refer to the official circular for complete details."

    total_vacancies = extracted.total_vacancies if (extracted.total_vacancies and extracted.total_vacancies > 0) else None
    source_domain = extract_domain(apply_url) or candidate.source_domain

    year_hint = posted_date[:4]
    base_slug = slugify(f"{organization} {title} {year_hint}")
    unique_slug = make_unique_slug(base_slug, JOBS_DIR, reserved_slugs)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gov_level, state_name = infer_government_classification(
        title=title,
        organization=organization,
        department=department,
        location=location,
        source_domain=source_domain,
        extra_context=candidate.extra_context,
        explicit_level=extracted.government_level,
        explicit_state=extracted.state_name,
    )

    salary_clean = extracted.salary_range.strip() if extracted.salary_range else None
    s_min, s_max, pay_level, salary_tier = infer_salary_fields(salary_clean)
    auth_type = infer_authority_type(
        title=title,
        organization=organization,
        department=department,
        source_domain=source_domain,
        gov_level=gov_level,
    )

    normalized_dates = [
        ImportantDateItem(
            label=item.label.strip(),
            date=format_display_date_str(item.date) or item.date.strip(),
        )
        for item in extracted.important_dates
        if item.label.strip() and item.date.strip()
    ]
    cleaned_extra_links = [
        item
        for item in extracted.important_links
        if item.label.strip() and is_valid_http_url(str(item.url))
    ]

    inferred_post_type: Literal["job", "admission"] = extracted.post_type
    if inferred_post_type == "job" and re.search(
        r"\b(cuet|jee\s*main|jee\s*advanced|neet\s*ug|neet\s*pg|jeecup|jnvst|b\.ed\s*admission|entrance\s+examination.*admission|common\s+entrance\s+test)\b",
        f"{title} {summary}",
        re.IGNORECASE,
    ):
        inferred_post_type = "admission"

    try:
        record = ValidatedJobRecord(
            slug=unique_slug,
            post_type=inferred_post_type,
            course_level=extracted.course_level.strip() if extracted.course_level else None,
            exam_date=format_display_date_str(extracted.exam_date) if extracted.exam_date else None,
            title=title,
            organization=organization,
            department=department,
            government_level=gov_level,
            state_name=state_name,
            location=location,
            qualification_level=qualification,
            total_vacancies=total_vacancies,
            salary_range=salary_clean,
            salary_min=s_min,
            salary_max=s_max,
            pay_level=pay_level,
            salary_tier=salary_tier,
            authority_type=auth_type,
            application_fee=extracted.application_fee.strip() if extracted.application_fee else None,
            age_limit=extracted.age_limit.strip() if extracted.age_limit else None,
            important_dates=normalized_dates,
            application_deadline=deadline,
            posted_date=posted_date,
            last_verified_at=now_iso,
            summary=summary,
            eligibility_bullets=[b.strip() for b in extracted.eligibility_bullets if len(b.strip()) >= 3],
            vacancy_breakdown=extracted.vacancy_breakdown,
            how_to_apply_steps=[s.strip() for s in extracted.how_to_apply_steps if len(s.strip()) >= 3],
            official_apply_url=apply_url,  # type: ignore[arg-type]
            official_pdf_url=pdf_url,  # type: ignore[arg-type]
            important_links=cleaned_extra_links or None,
            source_domain=source_domain,
            source_url=source_url,  # type: ignore[arg-type]
            notification_code=(extracted.notification_code or candidate.advt_no_hint or "").strip() or None,
            fee_structure=extracted.fee_structure,
            archived=False,
        )
    except ValidationError as exc:
        return None, f"schema validation failed: {exc.errors()[0].get('msg', exc)}"

    return record, "ok"


class ValidatedAdmissionRecord(BaseModel):
    """Strict admission record matching src/content.config.ts admissions schema."""

    slug: str
    title: str = Field(min_length=3)
    organization: str = Field(min_length=2)
    course_level: str = Field(min_length=2)
    location: str = Field(min_length=2)
    application_deadline: Optional[str] = None
    posted_date: Optional[str] = None
    last_verified_at: Optional[str] = None
    exam_date: str = Field(min_length=3)
    application_fee: Optional[str] = Field(default=None, min_length=1)
    age_limit: Optional[str] = Field(default=None, min_length=1)
    notification_code: Optional[str] = None
    fee_structure: Optional[list[FeeStructureItem]] = None
    summary: Optional[str] = None
    eligibility_bullets: list[str] = Field(default_factory=list)
    important_dates: list[ImportantDateItem] = Field(default_factory=list)
    how_to_apply_steps: Optional[list[str]] = None
    important_links: Optional[list[ImportantLinkItem]] = None
    homepage_menus: list[Literal["result", "admit-card", "answer-key"]] = Field(default_factory=list)
    official_apply_url: HttpUrl
    official_brochure_pdf: Optional[HttpUrl] = None
    source_domain: str = Field(min_length=3)
    exam_group_id: Optional[str] = None


def serialize_job_payload(record: ValidatedJobRecord, *, for_staging: bool = False) -> dict[str, Any]:
    """Serialize validated job record cleanly for Astro JSON ingestion."""
    payload = record.model_dump(mode="json", exclude_none=False)
    if not for_staging:
        payload.pop("post_type", None)
        payload.pop("course_level", None)
        payload.pop("exam_date", None)
    for opt_key in ("notification_code", "fee_structure", "important_links"):
        if payload.get(opt_key) is None:
            payload.pop(opt_key, None)
    if not payload.get("homepage_menus"):
        payload.pop("homepage_menus", None)
    for lnk in payload.get("important_links") or []:
        if isinstance(lnk, dict):
            if lnk.get("category") is None:
                lnk.pop("category", None)
            if lnk.get("added_at") is None:
                lnk.pop("added_at", None)
    for vb_item in payload.get("vacancy_breakdown") or []:
        if isinstance(vb_item, dict):
            if vb_item.get("notes") is None:
                vb_item.pop("notes", None)
            if vb_item.get("eligibility") is None:
                vb_item.pop("eligibility", None)
    route_prefix = "admissions" if (for_staging and record.post_type == "admission") else "jobs"
    ordered: dict[str, Any] = {
        "slug": payload["slug"],
        "sarkariindex_url": f"{SITE_BASE_URL}/{route_prefix}/{record.slug}",
    }
    for k, v in payload.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def serialize_admission_payload(record: ValidatedJobRecord) -> dict[str, Any]:
    """Convert a ValidatedJobRecord marked as admission into strict admissions schema JSON."""
    exam_dt = record.exam_date
    if not exam_dt:
        for row in record.important_dates:
            if any(tok in row.label.lower() for tok in ("exam", "test", "cbt", "entrance")):
                exam_dt = format_display_date_str(row.date) or row.date
                break
    exam_dt = (exam_dt or "Visit Official Site").strip()
    if len(exam_dt) < 3:
        exam_dt = "Visit Official Site"

    fee_str = record.application_fee
    if not fee_str and record.fee_structure:
        fee_str = " | ".join(f"{f.category}: {f.amount}" for f in record.fee_structure)

    adm = ValidatedAdmissionRecord(
        slug=record.slug,
        title=record.title,
        organization=record.organization,
        course_level=(record.course_level or record.qualification_level or "University / Entrance Admission").strip(),
        location=record.location,
        application_deadline=record.application_deadline,
        posted_date=record.posted_date,
        last_verified_at=record.last_verified_at,
        exam_date=exam_dt,
        application_fee=fee_str,
        age_limit=record.age_limit,
        notification_code=record.notification_code,
        fee_structure=record.fee_structure,
        summary=record.summary if len(record.summary) >= 20 else None,
        eligibility_bullets=record.eligibility_bullets or [f"Refer to official portal {record.source_domain} for eligibility criteria."],
        important_dates=record.important_dates,
        how_to_apply_steps=record.how_to_apply_steps or None,
        important_links=record.important_links,
        homepage_menus=record.homepage_menus,
        official_apply_url=record.official_apply_url,
        official_brochure_pdf=record.official_pdf_url,
        source_domain=record.source_domain,
        exam_group_id=record.exam_group_id,
    )
    payload = adm.model_dump(mode="json", exclude_none=False)
    for opt_k in ("summary", "exam_group_id", "age_limit", "notification_code", "fee_structure", "how_to_apply_steps", "important_links"):
        if payload.get(opt_k) is None:
            payload.pop(opt_k, None)
    if not payload.get("homepage_menus"):
        payload.pop("homepage_menus", None)
    ordered: dict[str, Any] = {
        "slug": payload["slug"],
        "sarkariindex_url": f"{SITE_BASE_URL}/admissions/{record.slug}",
    }
    for k, v in payload.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def archive_expired_jobs(jobs_dir: Path, today_utc: date, dry_run: bool) -> int:
    """Flag expired job JSON files so active feeds stay uncluttered."""
    if not jobs_dir.exists():
        return 0

    archived_count = 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for json_file in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning("Could not parse %s during archival check: %s", json_file.name, exc)
            continue

        deadline_str = data.get("application_deadline")
        if not deadline_str or not ISO_DATE_RE.match(str(deadline_str)):
            continue
        if data.get("archived") is True:
            continue

        if date.fromisoformat(str(deadline_str)) < today_utc:
            archived_count += 1
            if dry_run:
                logger.info("[DRY-RUN] Would archive expired notice: %s (deadline %s)", json_file.name, deadline_str)
            else:
                data["archived"] = True
                data["archived_at"] = now_iso
                json_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                logger.info("Archived expired notice: %s (deadline %s)", json_file.name, deadline_str)

    return archived_count


def load_json_registry(path: Path, root_key: str) -> dict[str, Any]:
    """Load a JSON registry file safely."""
    if not path.exists():
        return {root_key: {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and isinstance(data.get(root_key), dict):
            return data
    except Exception as exc:
        logger.warning("Could not read %s (%s); starting fresh.", path, exc)
    return {root_key: {}}


def load_existing_job_index(*extra_dirs: Path) -> tuple[set[str], set[str]]:
    """Index existing, admission, and staged official URLs and notification codes to skip duplicates."""
    existing_urls: set[str] = set()
    existing_codes: set[str] = set()
    scan_dirs = list(extra_dirs) if extra_dirs else [JOBS_DIR]
    if ADMISSIONS_DIR not in scan_dirs:
        scan_dirs.append(ADMISSIONS_DIR)
    if STAGING_JOBS_DIR not in scan_dirs:
        scan_dirs.append(STAGING_JOBS_DIR)
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for json_file in scan_dir.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for url_field in ("official_apply_url", "official_pdf_url", "official_brochure_pdf", "source_url"):
                val = data.get(url_field)
                if isinstance(val, str) and val.strip():
                    existing_urls.add(normalize_url(val))
            code = data.get("notification_code")
            if isinstance(code, str) and code.strip():
                existing_codes.add(code.strip().lower())
    return existing_urls, existing_codes


def push_admin_notification(
    kind: str,
    title: str,
    message: str,
    *,
    action_tab: Optional[str] = None,
) -> None:
    """Append an admin notification to scripts/admin_notifications.json."""
    items: list[dict[str, Any]] = []
    if ADMIN_NOTIFICATIONS_PATH.exists():
        try:
            loaded = json.loads(ADMIN_NOTIFICATIONS_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
                items = loaded["items"]
        except Exception:
            items = []

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items.insert(
        0,
        {
            "id": f"notif-{int(time.time() * 1000)}",
            "kind": kind,
            "title": title,
            "message": message,
            "action_tab": action_tab,
            "read": False,
            "created_at": now_iso,
        },
    )
    items = items[:50]
    ADMIN_NOTIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_NOTIFICATIONS_PATH.write_text(
        json.dumps({"items": items}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def save_json_registry(path: Path, data: dict[str, Any]) -> None:
    """Persist a JSON registry file cleanly without BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def queue_pending_pdf(
    pending_items: dict[str, Any],
    candidate: CandidateItem,
    reason: str,
) -> str:
    """Record an official notice needing manual Admin PDF upload."""
    org_part = candidate.default_organization or candidate.source_domain
    pending_id = slugify(f"{org_part} {candidate.title_hint}", max_length=60)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pending_items[pending_id] = {
        "id": pending_id,
        "status": "pending",
        "title": candidate.title_hint or "Official Recruitment Notice",
        "organization": candidate.default_organization or candidate.source_domain,
        "department": candidate.default_department,
        "qualification": candidate.qualification_hint,
        "advt_no": candidate.advt_no_hint,
        "posted_date": candidate.posted_date_hint,
        "application_deadline": candidate.deadline_hint,
        "discovery_url": candidate.discovery_url,
        "official_apply_url": candidate.official_apply_url_hint or candidate.url,
        "official_pdf_url": candidate.official_pdf_url_hint or (candidate.url if ".pdf" in candidate.url.lower() else None),
        "source_domain": candidate.source_domain,
        "suggested_pdf_path": f"inbox_pdfs/{pending_id}.pdf",
        "reason": reason,
        "created_at": now_iso,
    }
    return pending_id


def write_pending_markdown_report(pending_items: dict[str, Any], out_path: Path) -> int:
    """Generate a clean Markdown checklist for Admin notification / GitHub Issue."""
    active_pending = [
        item for item in pending_items.values()
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    if not active_pending:
        if out_path.exists():
            out_path.unlink()
        return 0

    lines = [
        "# Admin Action Required: Official PDFs Needed for Verification",
        "",
        "The automated pipeline discovered these active official notices whose PDFs/portals "
        "blocked automated download or timed out. To ingest and publish them:",
        "",
        "1. Download the official PDF in your browser using the link below.",
        "2. Save it to `inbox_pdfs/<id>.pdf` (matching the exact filename listed) and commit/push, OR run:",
        "   `python scripts/fetch_jobs.py --pdf path/to/file.pdf --pending-id <id>`",
        "",
        "| Pending ID | Organization & Post | Deadline | Official PDF / Portal | Target Upload Path | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in active_pending:
        pid = item.get("id", "")
        org = item.get("organization", "")
        title = item.get("title", "")
        deadline = item.get("application_deadline") or "Check Notice"
        pdf_link = item.get("official_pdf_url") or item.get("official_apply_url") or ""
        apply_link = item.get("official_apply_url") or ""
        target = item.get("suggested_pdf_path") or f"inbox_pdfs/{pid}.pdf"
        reason = str(item.get("reason", "")).replace("|", "/")[:90]
        links_md = f"[Official PDF]({pdf_link})" if item.get("official_pdf_url") else f"[Official Portal]({apply_link})"
        lines.append(f"| `{pid}` | **{org}** — {title} | {deadline} | {links_md} | `{target}` | {reason} |")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(active_pending)


def process_local_pdf_item(
    pdf_path: Path,
    *,
    pending_items: dict[str, Any],
    seen_urls: dict[str, Any],
    reserved_slugs: set[str],
    genai_client: Any,
    model_name: str,
    today_utc: date,
    dry_run: bool,
    pending_id: Optional[str] = None,
    apply_url_override: Optional[str] = None,
    pdf_url_override: Optional[str] = None,
    title_override: Optional[str] = None,
    org_override: Optional[str] = None,
    dept_override: Optional[str] = None,
    delete_after_ingest: bool = False,
    stage: bool = False,
) -> tuple[bool, str]:
    """Extract and ingest a manually uploaded official PDF (from --pdf or inbox_pdfs/)."""
    if not pdf_path.exists():
        return False, f"PDF not found: {pdf_path}"

    meta: dict[str, Any] = {}
    meta_sidecar = pdf_path.with_suffix(".meta.json")
    if meta_sidecar.exists():
        try:
            meta = json.loads(meta_sidecar.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning("Could not parse sidecar %s: %s", meta_sidecar.name, exc)

    matched_id = pending_id or pdf_path.stem
    pending_entry = pending_items.get(matched_id) if isinstance(pending_items.get(matched_id), dict) else None

    apply_url = (
        apply_url_override
        or meta.get("apply_url")
        or (pending_entry or {}).get("official_apply_url")
        or ""
    ).strip()
    official_pdf_url = (
        pdf_url_override
        or meta.get("pdf_url")
        or (pending_entry or {}).get("official_pdf_url")
        or apply_url
    ).strip()

    if not is_valid_http_url(apply_url):
        return False, (
            f"PDF '{pdf_path.name}' requires an official apply/portal URL "
            "(pass --apply-url, match a pending_id in scripts/pending_pdfs.json, or add <name>.meta.json)."
        )

    pdf_bytes = pdf_path.read_bytes()
    fetched = build_fetched_from_pdf_bytes(
        pdf_bytes,
        pdf_url=official_pdf_url if is_valid_http_url(official_pdf_url) else apply_url,
        final_url=apply_url,
    )

    candidate = CandidateItem(
        url=apply_url,
        source_id="admin-pdf-upload",
        source_domain=extract_domain(apply_url),
        title_hint=title_override or meta.get("title") or (pending_entry or {}).get("title") or pdf_path.stem,
        posted_date_hint=(pending_entry or {}).get("posted_date") or meta.get("posted_date"),
        deadline_hint=(pending_entry or {}).get("application_deadline") or meta.get("application_deadline"),
        default_organization=org_override or meta.get("organization") or (pending_entry or {}).get("organization"),
        default_department=dept_override or meta.get("department") or (pending_entry or {}).get("department"),
        advt_no_hint=(pending_entry or {}).get("advt_no") or meta.get("advt_no"),
        discovery_url=(pending_entry or {}).get("discovery_url"),
        official_apply_url_hint=apply_url,
        official_pdf_url_hint=official_pdf_url if is_valid_http_url(official_pdf_url) else None,
    )

    extracted = extract_with_gemini(candidate, fetched, genai_client, model_name)
    if extracted is None:
        return False, "empty Gemini response"

    record, reason = validate_extracted_job(extracted, candidate, fetched, reserved_slugs, today_utc)
    if record is None:
        return False, reason

    if dry_run:
        logger.info("[DRY-RUN] Validated admin PDF '%s' (%s) -> slug '%s'", pdf_path.name, record.post_type, record.slug)
        return True, record.slug

    if stage:
        out_dir = STAGING_JOBS_DIR
        payload = serialize_job_payload(record, for_staging=True)
    elif record.post_type == "admission":
        out_dir = ADMISSIONS_DIR
        payload = serialize_admission_payload(record)
    else:
        out_dir = JOBS_DIR
        payload = serialize_job_payload(record, for_staging=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / f"{record.slug}.json"
    target_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info(
        "%s admin-uploaded PDF '%s' -> %s",
        "Staged" if stage else "Ingested",
        pdf_path.name,
        target_path.relative_to(REPO_ROOT),
    )

    for seen_key in filter(None, {candidate.discovery_url, apply_url, official_pdf_url}):
        seen_urls[seen_key] = {
            "seen_at": record.last_verified_at,
            "source_id": "admin-pdf-upload",
            "status": "staged" if stage else "added",
            "slug": record.slug,
        }

    if pending_entry is not None:
        pending_entry["status"] = "resolved"
        pending_entry["resolved_at"] = record.last_verified_at
        pending_entry["resolved_slug"] = record.slug

    if delete_after_ingest:
        pdf_path.unlink(missing_ok=True)
        if meta_sidecar.exists():
            meta_sidecar.unlink(missing_ok=True)

    return True, record.slug


# ---------------------------------------------------------------------------
# Main Pipeline Orchestrator
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect, extract, validate, and archive official government job notices."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run discovery and extraction checks without writing job files or updating registries.",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Save newly extracted jobs into scripts/staging_jobs/ for admin review instead of publishing directly.",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Run only the single source matching this id in sources.json.",
    )
    parser.add_argument(
        "--portal",
        type=str,
        default=None,
        help="Filter admission portals inside 'Admission' target source by portal id(s) or organization search keyword.",
    )
    parser.add_argument(
        "--state",
        type=str,
        default=None,
        help="Target FreeJobAlert state code(s) e.g. UP, MH, UP,BR, or ALL (automatically selects freejobalert-statewise if --source is omitted).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum unseen items to process per source.",
    )
    parser.add_argument(
        "--pdf",
        type=str,
        default=None,
        help="Ingest a manually downloaded official PDF file directly.",
    )
    parser.add_argument("--pending-id", type=str, default=None, help="Pending ID in scripts/pending_pdfs.json.")
    parser.add_argument("--apply-url", type=str, default=None, help="Official application URL for --pdf.")
    parser.add_argument("--pdf-url", type=str, default=None, help="Official PDF URL for --pdf.")
    parser.add_argument("--title", type=str, default=None, help="Optional title hint for --pdf.")
    parser.add_argument("--org", type=str, default=None, help="Optional organization hint for --pdf.")
    parser.add_argument("--dept", type=str, default=None, help="Optional department hint for --pdf.")
    return parser.parse_args(argv)


def write_last_run_report(
    args: argparse.Namespace,
    *,
    exit_code: int,
    stats: Optional[RunStats] = None,
    failed_states: Optional[dict[str, dict[str, str]]] = None,
    failed_sources: Optional[list[dict[str, str]]] = None,
    failed_candidates: Optional[list[dict[str, str]]] = None,
    fatal_error: Optional[str] = None,
) -> None:
    """Persist structured summary and failed states/sources for Admin UI recovery controls."""
    report = {
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exit_code": exit_code,
        "source": args.source or "",
        "state": args.state or "",
        "limit": args.limit,
        "stage": bool(args.stage),
        "dry_run": bool(args.dry_run),
        "stats": {
            "checked": stats.checked if stats else 0,
            "added": stats.added if stats else 0,
            "skipped": stats.skipped if stats else 0,
            "failed": stats.failed if stats else (1 if exit_code != 0 else 0),
            "pending_admin": stats.pending_admin if stats else 0,
            "archived": stats.archived if stats else 0,
        },
        "failed_states": sorted(
            (failed_states or {}).values(),
            key=lambda x: str(x.get("code") or x.get("endpoint") or ""),
        ),
        "failed_sources": list(failed_sources or []),
        "failed_candidates": list(failed_candidates or [])[:30],
        "fatal_error": fatal_error,
    }
    try:
        LAST_RUN_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUN_REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not write last run report: %s", exc)


def run_pipeline(args: argparse.Namespace) -> int:
    """Execute admin PDF ingestion, daily collection, pending queue tracking, and archival."""
    load_dotenv_file(REPO_ROOT / ".env")
    if not SOURCES_PATH.exists():
        logger.error("Missing sources configuration: %s", SOURCES_PATH)
        write_last_run_report(args, exit_code=1, fatal_error="Missing sources.json configuration.")
        return 1

    config = json.loads(SOURCES_PATH.read_text(encoding="utf-8-sig"))
    bot_user_agent = config.get("bot_user_agent", DEFAULT_USER_AGENT)
    delay_seconds = float(config.get("request_delay_seconds", MIN_DOMAIN_DELAY_SECONDS))
    default_limit = int(args.limit or config.get("max_items_per_source", 10))
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]

    if args.source:
        sources = [s for s in sources if s.get("id") == args.source]
        if not sources:
            logger.error("No enabled source found with id '%s'.", args.source)
            write_last_run_report(args, exit_code=1, fatal_error=f"No enabled source found with id '{args.source}'.")
            return 1
    elif args.portal:
        adm_sources = [s for s in sources if s.get("type") == "admission_hub" or str(s.get("id") or "").lower() == "admission"]
        if not adm_sources:
            logger.error("No enabled Admission hub source found in sources.json for --portal filter '%s'.", args.portal)
            write_last_run_report(args, exit_code=1, fatal_error="No enabled Admission source found.")
            return 1
        sources = adm_sources
    elif args.state:
        state_sources = [s for s in sources if s.get("type") == "freejobalert_api" and "state" in str(s.get("id") or "")]
        if state_sources:
            sources = state_sources
        else:
            sources = [
                {
                    "id": "freejobalert-statewise",
                    "name": "FreeJobAlert State-Wise Government Jobs",
                    "type": "freejobalert_api",
                    "enabled": True,
                    "endpoint": f"state:{args.state}",
                    "api_base_url_env": "JOBFUL_API_URL",
                    "source_domain": "freejobalert.com",
                    "max_items": default_limit,
                }
            ]

    seen_registry = load_json_registry(SEEN_URLS_PATH, "urls")
    seen_urls: dict[str, Any] = seen_registry["urls"]
    pending_registry = load_json_registry(PENDING_PDFS_PATH, "items")
    pending_items: dict[str, Any] = pending_registry["items"]

    today_utc = datetime.now(timezone.utc).date()
    gemini_model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    active_gemini_keys = get_ordered_active_keys()
    genai_client = None

    if active_gemini_keys:
        logger.info(
            "Loaded %d active Gemini API key(s); primary is '%s'.",
            len(active_gemini_keys),
            active_gemini_keys[0].get("label") or active_gemini_keys[0].get("id"),
        )
    elif not args.dry_run:
        logger.error("No active Gemini API keys available in scripts/gemini_keys.json or .env GEMINI_API_KEY.")
        push_admin_notification(
            "action_required",
            "No Active Gemini API Keys",
            "Add or reset Gemini API keys in the Admin Keys page (/keys) to run extraction.",
            action_tab="runner",
        )
        write_last_run_report(
            args,
            exit_code=1,
            fatal_error="No active Gemini API keys available (all exhausted, disabled, or missing).",
        )
        return 1
    else:
        logger.warning("No active Gemini API keys available; running --dry-run in discovery/scraping verification mode.")

    stats = RunStats()
    failed_state_records: dict[str, dict[str, str]] = {}
    failed_sources: list[dict[str, str]] = []
    failed_candidates: list[dict[str, str]] = []
    reserved_slugs: set[str] = set()
    existing_apply_urls, existing_advt_codes = load_existing_job_index(JOBS_DIR)

    # 1) Direct --pdf ingestion mode
    if args.pdf:
        if not active_gemini_keys:
            logger.error("An active Gemini API key is required to extract --pdf files.")
            write_last_run_report(args, exit_code=1, fatal_error="Active Gemini API key required for --pdf.")
            return 1
        ok, outcome = process_local_pdf_item(
            Path(args.pdf),
            pending_items=pending_items,
            seen_urls=seen_urls,
            reserved_slugs=reserved_slugs,
            genai_client=genai_client,
            model_name=gemini_model,
            today_utc=today_utc,
            dry_run=args.dry_run,
            pending_id=args.pending_id,
            apply_url_override=args.apply_url,
            pdf_url_override=args.pdf_url,
            title_override=args.title,
            org_override=args.org,
            dept_override=args.dept,
            delete_after_ingest=False,
            stage=args.stage,
        )
        stats.checked += 1
        if ok:
            stats.added += 1
            if not args.dry_run and args.stage:
                push_admin_notification(
                    "review_required",
                    "Uploaded PDF Extracted for Review",
                    f"Extracted '{outcome}' into scripts/staging_jobs/ for admin approval.",
                    action_tab="review",
                )
        else:
            stats.failed += 1
            logger.error("Failed --pdf ingestion: %s", outcome)
        if not args.dry_run:
            save_json_registry(SEEN_URLS_PATH, seen_registry)
            save_json_registry(PENDING_PDFS_PATH, pending_registry)
            write_pending_markdown_report(pending_items, PENDING_PDFS_MD_PATH)
        write_last_run_report(
            args,
            exit_code=0 if ok else 1,
            stats=stats,
            fatal_error=None if ok else str(outcome),
        )
        print(
            f"Run Summary: checked={stats.checked}, added={stats.added}, "
            f"skipped={stats.skipped}, failed={stats.failed}, "
            f"pending_admin={stats.pending_admin}, archived={stats.archived}"
        )
        return 0 if ok else 1

    # 2) Auto-ingest any admin-uploaded PDFs placed in inbox_pdfs/
    if INBOX_PDFS_DIR.exists() and active_gemini_keys:
        for inbox_pdf in sorted(INBOX_PDFS_DIR.glob("*.pdf")):
            stats.checked += 1
            ok, outcome = process_local_pdf_item(
                inbox_pdf,
                pending_items=pending_items,
                seen_urls=seen_urls,
                reserved_slugs=reserved_slugs,
                genai_client=genai_client,
                model_name=gemini_model,
                today_utc=today_utc,
                dry_run=args.dry_run,
                delete_after_ingest=True,
                stage=args.stage,
            )
            if ok:
                stats.added += 1
            else:
                stats.failed += 1
                logger.warning("Could not ingest inbox PDF '%s': %s", inbox_pdf.name, outcome)

    # 3) Crawl configured sources
    http_client = PoliteHttpClient(user_agent=bot_user_agent, min_delay=delay_seconds)
    try:
        for source in sources:
            src_id = source.get("id", "unknown")
            src_type = source.get("type")
            logger.info("Processing source '%s' (%s)...", src_id, src_type)

            try:
                if src_type == "html_list":
                    candidates = collect_html_list(source, http_client, default_limit, skip_urls=set(seen_urls.keys()))
                elif src_type == "admission_hub":
                    portal_failures: list[dict[str, str]] = []
                    candidates = collect_admission_hub(
                        source,
                        http_client,
                        int(args.limit) if args.limit else int(source.get("max_items") or default_limit),
                        portal_filter=args.portal,
                        skip_urls=set(seen_urls.keys()) | existing_apply_urls,
                        failed_portals_out=portal_failures,
                    )
                    for pf in portal_failures:
                        stats.failed += 1
                        key_pf = str(pf.get("endpoint") or pf.get("name") or "admission-portal")
                        failed_state_records[key_pf] = pf
                elif src_type == "freejobalert_api":
                    ep_failures: list[dict[str, str]] = []
                    candidates = collect_freejobalert_api(
                        source,
                        http_client,
                        default_limit,
                        seen_urls,
                        pending_items,
                        today_utc,
                        existing_apply_urls,
                        existing_advt_codes,
                        limit_override=args.limit,
                        state_override=args.state,
                        failed_endpoints_out=ep_failures,
                    )
                    for ef in ep_failures:
                        stats.failed += 1
                        code = ef.get("code") or ef.get("endpoint") or "unknown"
                        failed_state_records[code] = ef
                elif src_type == "rss_feed":
                    candidates = collect_rss_feed(source, http_client, default_limit)
                elif src_type == "api_json":
                    candidates = collect_api_json(source, http_client, default_limit)
                else:
                    logger.warning("Unknown source type '%s' for source '%s'; skipping.", src_type, src_id)
                    continue
            except PermissionError as exc:
                logger.warning("Skipping source '%s' (%s)", src_id, exc)
                stats.skipped += 1
                continue
            except Exception as exc:
                logger.error("Source '%s' failed during discovery: %s", src_id, exc)
                stats.failed += 1
                failed_sources.append({"source_id": src_id, "error": str(exc).splitlines()[0][:200]})
                if src_type == "freejobalert_api":
                    for ep in _resolve_freejobalert_endpoints(source, state_override=args.state):
                        if ep.upper().startswith("STATE:"):
                            st_code = ep.split(":", 1)[1].upper()
                            st_name = STATE_ENDPOINTS[st_code][0] if st_code in STATE_ENDPOINTS else st_code
                            failed_state_records[st_code] = {
                                "endpoint": ep,
                                "code": st_code,
                                "name": st_name,
                                "error": str(exc).splitlines()[0][:200],
                            }
                continue

            logger.info("Discovered %d candidate URL(s) from '%s'.", len(candidates), src_id)

            for candidate in candidates:
                stats.checked += 1
                if candidate.url in seen_urls or (candidate.discovery_url and candidate.discovery_url in seen_urls):
                    stats.skipped += 1
                    continue

                if args.dry_run and not active_gemini_keys:
                    logger.info(
                        "[DRY-RUN] Unseen candidate: title=%r official_url=%s domain=%s",
                        candidate.title_hint or "(no title)",
                        candidate.url,
                        candidate.source_domain,
                    )
                    try:
                        fetched = fetch_candidate_content(candidate, http_client)
                        logger.info(
                            "[DRY-RUN] Official content ok: html_chars=%d pdf_url=%s pdf_chars=%d",
                            len(fetched.html_text),
                            fetched.pdf_url,
                            len(fetched.pdf_text),
                        )
                    except Exception as exc:
                        if candidate.allow_pending_queue:
                            stats.pending_admin += 1
                            logger.info(
                                "[DRY-RUN] Would queue for Admin PDF upload (%s): %s -> %s",
                                exc,
                                candidate.title_hint,
                                candidate.official_pdf_url_hint or candidate.url,
                            )
                        else:
                            stats.failed += 1
                            logger.warning("[DRY-RUN] Candidate fetch failed (%s): %s", candidate.url, exc)
                    continue

                try:
                    fetched = fetch_candidate_content(candidate, http_client)
                except Exception as exc:
                    if candidate.allow_pending_queue:
                        pending_id = queue_pending_pdf(pending_items, candidate, str(exc))
                        stats.pending_admin += 1
                        logger.warning(
                            "Official source unreachable/blocked (%s). Queued for Admin PDF upload as '%s' -> inbox_pdfs/%s.pdf",
                            exc,
                            candidate.title_hint,
                            pending_id,
                        )
                        if not args.dry_run and candidate.discovery_url:
                            seen_urls[candidate.discovery_url] = {
                                "seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "source_id": src_id,
                                "status": "pending_admin",
                                "pending_id": pending_id,
                            }
                    else:
                        stats.failed += 1
                        logger.error("Failed fetching candidate %s: %s", candidate.url, exc)
                        failed_candidates.append(
                            {
                                "title": candidate.title_hint or candidate.url,
                                "url": candidate.url,
                                "source_id": src_id,
                                "state_code": candidate.state_code or "",
                                "error": str(exc).splitlines()[0][:180],
                            }
                        )
                        if candidate.state_code and candidate.state_code not in failed_state_records:
                            st_name = STATE_ENDPOINTS[candidate.state_code][0] if candidate.state_code in STATE_ENDPOINTS else candidate.state_code
                            failed_state_records[candidate.state_code] = {
                                "endpoint": f"state:{candidate.state_code}",
                                "code": candidate.state_code,
                                "name": st_name,
                                "error": str(exc).splitlines()[0][:180],
                            }
                    continue

                try:
                    extracted = extract_with_gemini(candidate, fetched, genai_client, gemini_model)
                    if extracted is None:
                        stats.failed += 1
                        logger.warning("Empty Gemini response for %s", candidate.url)
                        failed_candidates.append(
                            {
                                "title": candidate.title_hint or candidate.url,
                                "url": candidate.url,
                                "source_id": src_id,
                                "state_code": candidate.state_code or "",
                                "error": "Empty Gemini response",
                            }
                        )
                        if candidate.state_code and candidate.state_code not in failed_state_records:
                            st_name = STATE_ENDPOINTS[candidate.state_code][0] if candidate.state_code in STATE_ENDPOINTS else candidate.state_code
                            failed_state_records[candidate.state_code] = {
                                "endpoint": f"state:{candidate.state_code}",
                                "code": candidate.state_code,
                                "name": st_name,
                                "error": "Empty Gemini response",
                            }
                        continue

                    record, reason = validate_extracted_job(
                        extracted, candidate, fetched, reserved_slugs, today_utc
                    )
                    if record is None:
                        stats.skipped += 1
                        logger.info("Rejected candidate (%s): %s", reason, candidate.url)
                        if not args.dry_run:
                            seen_key = candidate.discovery_url or candidate.url
                            seen_urls[seen_key] = {
                                "seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "source_id": src_id,
                                "status": "skipped",
                                "reason": reason,
                            }
                        continue

                    if args.dry_run:
                        payload = (
                            serialize_admission_payload(record)
                            if record.post_type == "admission" and not args.stage
                            else serialize_job_payload(record, for_staging=args.stage)
                        )
                        stats.added += 1
                        logger.info(
                            "[DRY-RUN] Validated %s JSON for slug '%s': %s",
                            record.post_type,
                            record.slug,
                            json.dumps(payload, indent=2, ensure_ascii=False)[:500],
                        )
                    else:
                        if args.stage:
                            out_dir = STAGING_JOBS_DIR
                            payload = serialize_job_payload(record, for_staging=True)
                        elif record.post_type == "admission":
                            out_dir = ADMISSIONS_DIR
                            payload = serialize_admission_payload(record)
                        else:
                            out_dir = JOBS_DIR
                            payload = serialize_job_payload(record, for_staging=False)
                        out_dir.mkdir(parents=True, exist_ok=True)
                        target_path = out_dir / f"{record.slug}.json"
                        target_path.write_text(
                            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        for key in filter(None, {candidate.url, candidate.discovery_url}):
                            seen_urls[key] = {
                                "seen_at": record.last_verified_at,
                                "source_id": src_id,
                                "status": "staged" if args.stage else "added",
                                "slug": record.slug,
                            }
                        existing_apply_urls.add(normalize_url(str(record.official_apply_url)))
                        if record.notification_code:
                            existing_advt_codes.add(record.notification_code.lower())
                        stats.added += 1
                        logger.info(
                            "%s %s notice: %s",
                            "Staged" if args.stage else "Saved new",
                            record.post_type,
                            target_path.relative_to(REPO_ROOT),
                        )
                except Exception as exc:
                    stats.failed += 1
                    logger.error("Failed processing candidate %s: %s", candidate.url, exc)
                    failed_candidates.append(
                        {
                            "title": candidate.title_hint or candidate.url,
                            "url": candidate.url,
                            "source_id": src_id,
                            "state_code": candidate.state_code or "",
                            "error": str(exc).splitlines()[0][:180],
                        }
                    )
                    if candidate.state_code and candidate.state_code not in failed_state_records:
                        st_name = STATE_ENDPOINTS[candidate.state_code][0] if candidate.state_code in STATE_ENDPOINTS else candidate.state_code
                        failed_state_records[candidate.state_code] = {
                            "endpoint": f"state:{candidate.state_code}",
                            "code": candidate.state_code,
                            "name": st_name,
                            "error": str(exc).splitlines()[0][:180],
                        }

        stats.archived = archive_expired_jobs(JOBS_DIR, today_utc, dry_run=args.dry_run)
        if not args.dry_run:
            save_json_registry(SEEN_URLS_PATH, seen_registry)
            save_json_registry(PENDING_PDFS_PATH, pending_registry)
            pending_total = write_pending_markdown_report(pending_items, PENDING_PDFS_MD_PATH)
            if pending_total > 0:
                logger.warning(
                    "ADMIN ACTION REQUIRED: %d notice(s) waiting in scripts/pending_pdfs.json (see scripts/pending_pdfs.md).",
                    pending_total,
                )
            if stats.pending_admin > 0:
                push_admin_notification(
                    "action_required",
                    "Official PDFs Required",
                    f"{stats.pending_admin} new notice(s) could not be fetched automatically and need admin PDF upload.",
                    action_tab="pending",
                )
            if stats.added > 0:
                if args.stage:
                    push_admin_notification(
                        "review_required",
                        "Extracted Jobs Ready for Review",
                        f"{stats.added} newly extracted job(s) staged in scripts/staging_jobs/ for approval.",
                        action_tab="review",
                    )
                else:
                    push_admin_notification(
                        "run_completed",
                        "New Jobs Published",
                        f"{stats.added} verified job(s) published to src/data/jobs/.",
                        action_tab="published",
                    )
            elif stats.failed > 0:
                push_admin_notification(
                    "run_error",
                    "Pipeline Completed With Errors",
                    f"Run finished with {stats.failed} error(s). Check run logs for details.",
                    action_tab="runner",
                )
    finally:
        http_client.close()

    if failed_state_records:
        logger.warning(
            "Failed States (can be re-run cleanly): %s",
            ", ".join(sorted(failed_state_records.keys())),
        )

    write_last_run_report(
        args,
        exit_code=0,
        stats=stats,
        failed_states=failed_state_records,
        failed_sources=failed_sources,
        failed_candidates=failed_candidates,
    )

    summary = (
        f"Run Summary: checked={stats.checked}, added={stats.added}, "
        f"skipped={stats.skipped}, failed={stats.failed}, "
        f"pending_admin={stats.pending_admin}, archived={stats.archived}"
    )
    print(summary)
    logger.info(summary)
    return 0


if __name__ == "__main__":
    sys.exit(run_pipeline(parse_args()))

#!/usr/bin/env python3
"""Local Admin Control Server for ChayanDesk.

Runs on 127.0.0.1:8787 (localhost-only) to provide manual/automatic pipeline
execution, staging review, manual job creation, pending PDF upload + Gemini scan,
notifications, and Astro site build verification.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import ValidationError

from fetch_jobs import (
    ADMIN_NOTIFICATIONS_PATH,
    ADMISSIONS_DIR,
    INBOX_PDFS_DIR,
    JOBS_DIR,
    LAST_RUN_REPORT_PATH,
    PENDING_PDFS_MD_PATH,
    PENDING_PDFS_PATH,
    REPO_ROOT,
    SEEN_URLS_PATH,
    SOURCES_PATH,
    STAGING_JOBS_DIR,
    ValidatedJobRecord,
    extract_domain,
    format_display_date_str,
    infer_authority_type,
    infer_government_classification,
    infer_salary_fields,
    load_dotenv_file,
    load_json_registry,
    make_unique_slug,
    parse_to_iso_date,
    push_admin_notification,
    save_json_registry,
    serialize_admission_payload,
    serialize_job_payload,
    slugify,
    write_pending_markdown_report,
)

from freejobalert_api import STATE_ENDPOINTS
from gemini_keys import (
    add_gemini_keys,
    delete_key_entry,
    load_keys_store,
    probe_key_live,
    sanitize_key_for_ui,
    update_key_entry,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
ADMIN_UI_PATH = SCRIPTS_DIR / "admin_ui.html"
ADMIN_KEYS_UI_PATH = SCRIPTS_DIR / "admin_keys.html"
ADMIN_STATE_PATH = SCRIPTS_DIR / "admin_state.json"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

STATE_LOCK = threading.Lock()


class RunnerState:
    """Tracks live and persisted background execution of fetch_jobs.py or astro build."""

    def __init__(self) -> None:
        self.running: bool = False
        self.task_name: str = ""
        self.pid: Optional[int] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.logs: list[str] = []
        self.last_summary: str = ""
        self.current_params: dict[str, Any] = {}
        self.last_params: dict[str, Any] = {}
        self.last_failed_params: dict[str, Any] = {}
        self.failed_states: list[dict[str, Any]] = []
        self.failed_sources: list[dict[str, Any]] = []
        self.failed_candidates: list[dict[str, Any]] = []
        self.has_failure: bool = False

    def snapshot(self) -> dict[str, Any]:
        with STATE_LOCK:
            return {
                "running": self.running,
                "task_name": self.task_name,
                "pid": self.pid,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "exit_code": self.exit_code,
                "log_tail": "\n".join(self.logs[-160:]),
                "last_summary": self.last_summary,
                "last_params": self.last_params,
                "last_failed_params": self.last_failed_params,
                "failed_states": self.failed_states,
                "failed_sources": self.failed_sources,
                "failed_candidates": self.failed_candidates,
                "has_failure": self.has_failure,
                "can_rerun_last": (not self.running) and bool(self.last_params),
                "can_rerun_failed_states": (not self.running) and any(
                    bool(s.get("code")) for s in self.failed_states
                ),
            }


RUNNER = RunnerState()


def load_admin_state() -> dict[str, Any]:
    """Load persisted scheduler and last-runner recovery configuration."""
    default_state: dict[str, Any] = {
        "scheduler": {
            "enabled": False,
            "interval_hours": 12,
            "stage_for_review": True,
            "source": "",
            "limit": 15,
            "last_run_at": None,
            "next_run_at": None,
        },
        "last_runner": {},
    }
    if not ADMIN_STATE_PATH.exists():
        return default_state
    try:
        data = json.loads(ADMIN_STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            if isinstance(data.get("scheduler"), dict):
                default_state["scheduler"].update(data["scheduler"])
            if isinstance(data.get("last_runner"), dict):
                default_state["last_runner"] = data["last_runner"]
    except Exception:
        pass
    return default_state


def _init_runner_from_disk() -> None:
    """Restore failure metadata from admin_state.json and last_run_report.json on server start."""
    state = load_admin_state()
    lr = state.get("last_runner") or {}
    with STATE_LOCK:
        RUNNER.task_name = str(lr.get("task_name") or "")
        RUNNER.finished_at = lr.get("finished_at")
        RUNNER.exit_code = lr.get("exit_code")
        RUNNER.last_summary = str(lr.get("last_summary") or "")
        RUNNER.last_params = dict(lr.get("last_params") or {})
        RUNNER.last_failed_params = dict(lr.get("last_failed_params") or {})
        RUNNER.failed_states = list(lr.get("failed_states") or [])
        RUNNER.failed_sources = list(lr.get("failed_sources") or [])
        RUNNER.failed_candidates = list(lr.get("failed_candidates") or [])
        RUNNER.has_failure = bool(lr.get("has_failure", False))

    if LAST_RUN_REPORT_PATH.exists() and not RUNNER.failed_states and not RUNNER.last_summary:
        try:
            rep = json.loads(LAST_RUN_REPORT_PATH.read_text(encoding="utf-8-sig"))
            with STATE_LOCK:
                RUNNER.failed_states = list(rep.get("failed_states") or [])
                RUNNER.failed_sources = list(rep.get("failed_sources") or [])
                RUNNER.failed_candidates = list(rep.get("failed_candidates") or [])
                st = rep.get("stats") or {}
                RUNNER.has_failure = (
                    rep.get("exit_code", 0) != 0
                    or int(st.get("failed") or 0) > 0
                    or bool(RUNNER.failed_states)
                    or bool(RUNNER.failed_sources)
                )
        except Exception:
            pass


def save_admin_state(state: dict[str, Any]) -> None:
    ADMIN_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _compute_next_run(hours: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=max(1.0, hours))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jobs_in_dir(target_dir: Path) -> list[dict[str, Any]]:
    if not target_dir.exists():
        return []
    today_utc = datetime.now(timezone.utc).date()
    items: list[dict[str, Any]] = []
    for path in sorted(target_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        deadline_str = data.get("application_deadline")
        is_archived = bool(data.get("archived", False))
        days_left: Optional[int] = None
        if isinstance(deadline_str, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", deadline_str):
            days_left = (date.fromisoformat(deadline_str) - today_utc).days
        is_active = (not is_archived) and (days_left is None or days_left >= 0)
        is_closing_soon = is_active and (days_left is not None and 0 <= days_left <= 7)
        items.append(
            {
                "filename": path.name,
                "is_active": is_active,
                "is_closing_soon": is_closing_soon,
                "days_left": days_left,
                "data": data,
            }
        )
    items.sort(key=lambda x: str(x["data"].get("posted_date") or ""), reverse=True)
    return items


def _launch_subprocess_task(
    task_name: str,
    cmd: list[str],
    extra_env: Optional[dict[str, str]] = None,
    run_params: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    params_copy = dict(run_params or {"kind": task_name, "cmd": list(cmd)})
    with STATE_LOCK:
        if RUNNER.running:
            return False, f"Another task ({RUNNER.task_name}) is already running."
        prev_failed_states = list(RUNNER.failed_states)
        RUNNER.running = True
        RUNNER.task_name = task_name
        RUNNER.pid = None
        RUNNER.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        RUNNER.finished_at = None
        RUNNER.exit_code = None
        RUNNER.logs = [f"[{RUNNER.started_at}] Starting {task_name}: {' '.join(cmd)}"]
        RUNNER.last_summary = ""
        RUNNER.current_params = params_copy
        RUNNER.last_params = params_copy

    def _worker() -> None:
        env = os.environ.copy()
        load_dotenv_file(REPO_ROOT / ".env")
        for k, v in os.environ.items():
            env.setdefault(k, v)
        if extra_env:
            env.update(extra_env)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            with STATE_LOCK:
                RUNNER.pid = proc.pid
            assert proc.stdout is not None
            for line in proc.stdout:
                clean = line.rstrip("\r\n")
                with STATE_LOCK:
                    RUNNER.logs.append(clean)
                    if len(RUNNER.logs) > 600:
                        RUNNER.logs = RUNNER.logs[-500:]
                    if clean.startswith("Run Summary:"):
                        RUNNER.last_summary = clean
            code = proc.wait()
        except Exception as exc:
            code = 1
            with STATE_LOCK:
                RUNNER.logs.append(f"[ERROR] Failed executing subprocess: {exc}")
        finally:
            finished_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            new_failed_states: list[dict[str, Any]] = []
            new_failed_sources: list[dict[str, Any]] = []
            new_failed_candidates: list[dict[str, Any]] = []
            failed_counter = 0

            if task_name == "fetch_jobs" and LAST_RUN_REPORT_PATH.exists():
                try:
                    rep = json.loads(LAST_RUN_REPORT_PATH.read_text(encoding="utf-8-sig"))
                    new_failed_states = list(rep.get("failed_states") or [])
                    new_failed_sources = list(rep.get("failed_sources") or [])
                    new_failed_candidates = list(rep.get("failed_candidates") or [])
                    failed_counter = int((rep.get("stats") or {}).get("failed") or 0)
                except Exception:
                    pass

            retried_codes = [
                str(c).strip().upper()
                for c in (params_copy.get("rerun_states") or [])
                if str(c).strip()
            ]
            if task_name == "fetch_jobs":
                if retried_codes:
                    # Keep previously failed states that were not part of this retry, plus any newly failed ones
                    by_code: dict[str, dict[str, Any]] = {
                        str(s.get("code") or s.get("endpoint") or ""): s
                        for s in prev_failed_states
                        if str(s.get("code") or "").upper() not in retried_codes
                    }
                    if code != 0 and not new_failed_states:
                        for rc in retried_codes:
                            by_code[rc] = {
                                "endpoint": f"state:{rc}",
                                "code": rc,
                                "name": STATE_ENDPOINTS[rc][0] if rc in STATE_ENDPOINTS else rc,
                                "error": f"Re-run exited with code {code}",
                            }
                    for ns in new_failed_states:
                        key = str(ns.get("code") or ns.get("endpoint") or "")
                        if key:
                            by_code[key] = ns
                    final_failed_states = sorted(by_code.values(), key=lambda x: str(x.get("code") or ""))
                else:
                    final_failed_states = new_failed_states
            else:
                final_failed_states = prev_failed_states if code == 0 else []

            has_failure = (
                (code != 0)
                or (failed_counter > 0)
                or bool(final_failed_states)
                or bool(new_failed_sources)
            )

            with STATE_LOCK:
                RUNNER.running = False
                RUNNER.exit_code = code
                RUNNER.finished_at = finished_iso
                RUNNER.failed_states = final_failed_states
                RUNNER.failed_sources = new_failed_sources
                RUNNER.failed_candidates = new_failed_candidates
                RUNNER.has_failure = has_failure
                if has_failure:
                    RUNNER.last_failed_params = params_copy

                state_file = load_admin_state()
                state_file["last_runner"] = {
                    "task_name": task_name,
                    "finished_at": finished_iso,
                    "exit_code": code,
                    "last_summary": RUNNER.last_summary,
                    "last_params": RUNNER.last_params,
                    "last_failed_params": RUNNER.last_failed_params,
                    "failed_states": final_failed_states,
                    "failed_sources": new_failed_sources,
                    "failed_candidates": new_failed_candidates,
                    "has_failure": has_failure,
                }
                try:
                    save_admin_state(state_file)
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()
    return True, "Started"


def start_fetch_job_run(
    *,
    source: str = "",
    state: str = "",
    portal: str = "",
    limit: Optional[int] = None,
    dry_run: bool = False,
    stage: bool = True,
    rerun_states: Optional[list[str]] = None,
) -> tuple[bool, str]:
    cmd = [sys.executable, str(SCRIPTS_DIR / "fetch_jobs.py")]
    if source:
        cmd.extend(["--source", source])
    if state:
        cmd.extend(["--state", state])
    if portal:
        cmd.extend(["--portal", portal])
    if limit and limit > 0:
        cmd.extend(["--limit", str(limit)])
    if dry_run:
        cmd.append("--dry-run")
    if stage:
        cmd.append("--stage")
    params = {
        "kind": "fetch_jobs",
        "source": source,
        "state": state,
        "portal": portal,
        "limit": limit,
        "dry_run": dry_run,
        "stage": stage,
        "rerun_states": rerun_states or [],
        "cmd": list(cmd),
    }
    return _launch_subprocess_task("fetch_jobs", cmd, run_params=params)


def _scheduler_loop() -> None:
    """Background thread running automatic scheduled pipeline syncs when enabled."""
    while True:
        time.sleep(15)
        try:
            state = load_admin_state()
            sched = state.get("scheduler", {})
            if not sched.get("enabled"):
                continue
            next_run_str = sched.get("next_run_at")
            now_utc = datetime.now(timezone.utc)
            if not next_run_str:
                sched["next_run_at"] = _compute_next_run(float(sched.get("interval_hours", 12)))
                save_admin_state(state)
                continue
            next_dt = datetime.strptime(next_run_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if now_utc >= next_dt and not RUNNER.running:
                ok, _ = start_fetch_job_run(
                    source=str(sched.get("source") or ""),
                    limit=int(sched.get("limit") or 15),
                    dry_run=False,
                    stage=bool(sched.get("stage_for_review", True)),
                )
                if ok:
                    sched["last_run_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    sched["next_run_at"] = _compute_next_run(float(sched.get("interval_hours", 12)))
                    save_admin_state(state)
        except Exception:
            pass


def _get_state_options() -> list[dict[str, str]]:
    """Build deduplicated list of FreeJobAlert states for UI dropdowns."""
    seen_urls: set[str] = set()
    opts: list[dict[str, str]] = []
    for code, (name, url) in STATE_ENDPOINTS.items():
        if url in seen_urls:
            continue
        seen_urls.add(url)
        opts.append({"code": code, "name": name, "url": url})
    opts.sort(key=lambda x: x["name"])
    return opts


def _get_admission_portal_options(sources_list: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build searchable list of official Admission conducting organizations from Admission source(s)."""
    portals: list[dict[str, str]] = []
    for src in sources_list:
        if src.get("type") != "admission_hub" and str(src.get("id") or "").lower() != "admission":
            continue
        for p in src.get("portals") or []:
            if not isinstance(p, dict):
                continue
            portals.append({
                "id": str(p.get("id") or ""),
                "name": str(p.get("name") or ""),
                "short_name": str(p.get("short_name") or p.get("default_organization") or p.get("id") or ""),
                "category": str(p.get("category") or "Admission Portal"),
                "source_domain": str(p.get("source_domain") or ""),
                "url": str(p.get("url") or ""),
                "enabled": p.get("enabled", True),
            })
    return portals


def _get_sanitized_keys_summary() -> dict[str, Any]:
    """Return masked Gemini key pool entries and aggregate daily metrics."""
    store = load_keys_store(seed_from_env=True)
    raw_keys = store.get("keys", [])
    safe_keys = [sanitize_key_for_ui(k) for k in raw_keys if isinstance(k, dict)]
    active_count = sum(
        1 for k in safe_keys
        if k.get("enabled", True) and k.get("status") not in {"exhausted", "invalid"}
    )
    exhausted_count = sum(1 for k in safe_keys if k.get("status") == "exhausted")
    invalid_count = sum(1 for k in safe_keys if k.get("status") == "invalid")
    return {
        "keys": safe_keys,
        "total_count": len(safe_keys),
        "active_count": active_count,
        "exhausted_count": exhausted_count,
        "invalid_count": invalid_count,
        "total_requests_today": sum(int(k.get("requests_today") or 0) for k in safe_keys),
        "total_tokens_today": sum(int(k.get("tokens_today") or 0) for k in safe_keys),
        "total_estimated_remaining": sum(
            int(k.get("estimated_remaining") or 0)
            for k in safe_keys
            if k.get("enabled", True) and k.get("status") not in {"exhausted", "invalid"}
        ),
    }


def _validate_and_normalize_job_payload(raw: dict[str, Any], *, target_dir: Path, keep_existing_slug: Optional[str] = None) -> ValidatedJobRecord:
    """Validate manual or edited job JSON strictly against ValidatedJobRecord."""
    data = dict(raw)
    title = str(data.get("title") or "").strip()
    org = str(data.get("organization") or "").strip()
    apply_url = str(data.get("official_apply_url") or "").strip()
    raw_posted = str(data.get("posted_date") or "").strip()
    posted_date = parse_to_iso_date(raw_posted) or datetime.now(timezone.utc).date().isoformat()
    raw_deadline = str(data.get("application_deadline") or "").strip()
    if raw_deadline:
        parsed_dl = parse_to_iso_date(raw_deadline)
        if not parsed_dl:
            raise ValueError(f"Unrecognized Application Deadline '{raw_deadline}'. Use format like '12 Dec 2026' or '2026-12-12'.")
        data["application_deadline"] = parsed_dl
    else:
        data["application_deadline"] = None

    if keep_existing_slug and re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", keep_existing_slug):
        slug = keep_existing_slug
    elif data.get("slug") and re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", str(data["slug"]).strip()):
        slug = str(data["slug"]).strip()
    else:
        base_slug = slugify(f"{org} {title} {posted_date[:4]}")
        slug = make_unique_slug(base_slug, target_dir, set())

    data["slug"] = slug
    data["posted_date"] = posted_date
    data["last_verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not data.get("source_domain") and apply_url:
        data["source_domain"] = extract_domain(apply_url)
    if not data.get("source_url") and apply_url:
        data["source_url"] = apply_url

    for nullable_str in ("salary_range", "application_fee", "age_limit", "official_pdf_url", "notification_code", "state_name"):
        if isinstance(data.get(nullable_str), str) and not data[nullable_str].strip():
            data[nullable_str] = None

    raw_gov_level = str(data.get("government_level") or "").strip().lower() or None
    gov_level, norm_state = infer_government_classification(
        title=title,
        organization=org,
        department=str(data.get("department") or ""),
        location=str(data.get("location") or ""),
        source_domain=str(data.get("source_domain") or ""),
        explicit_level=raw_gov_level if raw_gov_level in {"central", "state"} else None,
        explicit_state=data.get("state_name"),
    )
    data["government_level"] = gov_level
    data["state_name"] = norm_state
    s_min, s_max, pay_lvl, s_tier = infer_salary_fields(data.get("salary_range"))
    data["salary_min"] = s_min
    data["salary_max"] = s_max
    data["pay_level"] = pay_lvl
    data["salary_tier"] = s_tier
    if not data.get("authority_type"):
        data["authority_type"] = infer_authority_type(
            title=title,
            organization=org,
            department=str(data.get("department") or ""),
            source_domain=str(data.get("source_domain") or ""),
            gov_level=gov_level,
        )

    if isinstance(data.get("important_dates"), list):
        data["important_dates"] = [
            {
                "label": str(row.get("label") or "").strip(),
                "date": format_display_date_str(str(row.get("date") or "")) or str(row.get("date") or "").strip(),
            }
            for row in data["important_dates"]
            if isinstance(row, dict) and str(row.get("label") or "").strip() and str(row.get("date") or "").strip()
        ]

    if isinstance(data.get("fee_structure"), list):
        cleaned_fees = [
            {
                "category": str(row.get("category") or "").strip(),
                "amount": str(row.get("amount") or "").strip(),
            }
            for row in data["fee_structure"]
            if isinstance(row, dict) and str(row.get("category") or "").strip() and str(row.get("amount") or "").strip()
        ]
        data["fee_structure"] = cleaned_fees or None

    if isinstance(data.get("vacancy_breakdown"), list):
        cleaned_vacs = []
        for row in data["vacancy_breakdown"]:
            if not isinstance(row, dict) or not str(row.get("category") or "").strip():
                continue
            vac_raw = row.get("vacancies")
            vac_int = int(vac_raw) if str(vac_raw).strip().isdigit() else (vac_raw if isinstance(vac_raw, int) else None)
            elig_txt = str(row.get("eligibility") or "").strip() or None
            notes_txt = str(row.get("notes") or "").strip() or None
            cleaned_vacs.append({
                "category": str(row.get("category") or "").strip(),
                "vacancies": vac_int,
                "eligibility": elig_txt,
                "notes": notes_txt,
            })
        data["vacancy_breakdown"] = cleaned_vacs

    if isinstance(data.get("eligibility_bullets"), list):
        data["eligibility_bullets"] = [str(b).strip() for b in data["eligibility_bullets"] if len(str(b).strip()) >= 3]

    if isinstance(data.get("how_to_apply_steps"), list):
        data["how_to_apply_steps"] = [str(s).strip() for s in data["how_to_apply_steps"] if len(str(s).strip()) >= 3]

    if isinstance(data.get("important_links"), list):
        today_iso = datetime.now(timezone.utc).date().isoformat()
        cleaned_links = []
        for item in data["important_links"]:
            if not isinstance(item, dict):
                continue
            lbl = str(item.get("label") or "").strip()
            url = str(item.get("url") or "").strip()
            if not lbl or not url:
                continue
            raw_cat = str(item.get("category") or "").strip().lower()
            cat = raw_cat if raw_cat in {"result", "admit-card", "answer-key"} else None
            added_at = parse_to_iso_date(str(item.get("added_at") or "")) or (today_iso if cat else None)
            cleaned_links.append({
                "label": lbl,
                "url": url,
                "category": cat,
                "added_at": added_at,
            })
        data["important_links"] = cleaned_links or None

    raw_pt = str(data.get("post_type") or "job").strip().lower()
    data["post_type"] = "admission" if raw_pt == "admission" else "job"
    if isinstance(data.get("course_level"), str):
        data["course_level"] = data["course_level"].strip() or None
    if isinstance(data.get("exam_date"), str):
        ed_raw = data["exam_date"].strip()
        data["exam_date"] = (format_display_date_str(ed_raw) or ed_raw) if ed_raw else None

    raw_menus = data.get("homepage_menus")
    clean_menus: list[str] = []
    if isinstance(raw_menus, list):
        for m in raw_menus:
            mv = str(m or "").strip().lower()
            if mv in {"result", "admit-card", "answer-key"} and mv not in clean_menus:
                clean_menus.append(mv)
    data["homepage_menus"] = clean_menus

    if data.get("total_vacancies") in ("", None):
        data["total_vacancies"] = None
    elif isinstance(data.get("total_vacancies"), str):
        data["total_vacancies"] = int(data["total_vacancies"])

    return ValidatedJobRecord.model_validate(data)


def _read_admissions_in_dir(directory: Path) -> list[dict[str, Any]]:
    """Read admission JSON files cleanly sorted by application deadline."""
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for p in sorted(directory.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        items.append({"filename": p.name, "data": d})
    items.sort(key=lambda x: str(x["data"].get("application_deadline") or "9999-12-31"))
    return items


class AdminHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the local Admin Panel UI and JSON API."""

    server_version = "ChayanDeskAdmin/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > MAX_UPLOAD_BYTES + 2_000_000:
            raise ValueError("Invalid or oversized request body.")
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object.")
        return parsed

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in {"/", "/index.html"}:
            if not ADMIN_UI_PATH.exists():
                self._send_html(HTTPStatus.NOT_FOUND, "<h1>Missing scripts/admin_ui.html</h1>")
                return
            self._send_html(HTTPStatus.OK, ADMIN_UI_PATH.read_text(encoding="utf-8"))
            return

        if path in {"/keys", "/keys.html"}:
            if not ADMIN_KEYS_UI_PATH.exists():
                self._send_html(HTTPStatus.NOT_FOUND, "<h1>Missing scripts/admin_keys.html</h1>")
                return
            self._send_html(HTTPStatus.OK, ADMIN_KEYS_UI_PATH.read_text(encoding="utf-8"))
            return

        if path == "/api/keys":
            self._send_json(HTTPStatus.OK, _get_sanitized_keys_summary())
            return

        if path == "/api/overview":
            published = _read_jobs_in_dir(JOBS_DIR)
            admissions = _read_admissions_in_dir(ADMISSIONS_DIR)
            staged = _read_jobs_in_dir(STAGING_JOBS_DIR)
            pending_reg = load_json_registry(PENDING_PDFS_PATH, "items")
            pending_active = [
                v for v in pending_reg["items"].values()
                if isinstance(v, dict) and v.get("status") == "pending"
            ]
            notif_reg = load_json_registry(ADMIN_NOTIFICATIONS_PATH, "items")
            notifs = notif_reg.get("items", []) if isinstance(notif_reg.get("items"), list) else []
            unread = sum(1 for n in notifs if isinstance(n, dict) and not n.get("read"))
            sources_cfg = json.loads(SOURCES_PATH.read_text(encoding="utf-8-sig")) if SOURCES_PATH.exists() else {"sources": []}
            sources_list = sources_cfg.get("sources", [])
            state = load_admin_state()
            keys_summary = _get_sanitized_keys_summary()
            self._send_json(
                HTTPStatus.OK,
                {
                    "published_count": len(published),
                    "admissions_count": len(admissions),
                    "active_count": sum(1 for j in published if j["is_active"]),
                    "closing_soon_count": sum(1 for j in published if j["is_closing_soon"]),
                    "staged_count": len(staged),
                    "pending_pdf_count": len(pending_active),
                    "unread_notifications": unread,
                    "active_gemini_keys": keys_summary["active_count"],
                    "exhausted_gemini_keys": keys_summary["exhausted_count"],
                    "total_gemini_keys": keys_summary["total_count"],
                    "runner": RUNNER.snapshot(),
                    "scheduler": state["scheduler"],
                    "sources": sources_list,
                    "state_options": _get_state_options(),
                    "admission_portals": _get_admission_portal_options(sources_list),
                },
            )
            return

        if path == "/api/jobs":
            self._send_json(HTTPStatus.OK, {"jobs": _read_jobs_in_dir(JOBS_DIR)})
            return

        if path == "/api/admissions":
            self._send_json(HTTPStatus.OK, {"admissions": _read_admissions_in_dir(ADMISSIONS_DIR)})
            return

        if path == "/api/staging":
            self._send_json(HTTPStatus.OK, {"jobs": _read_jobs_in_dir(STAGING_JOBS_DIR)})
            return

        if path == "/api/pending-pdfs":
            pending_reg = load_json_registry(PENDING_PDFS_PATH, "items")
            items = list(pending_reg["items"].values())
            items.sort(key=lambda x: (0 if x.get("status") == "pending" else 1, str(x.get("created_at") or "")), reverse=False)
            inbox_files = [p.name for p in sorted(INBOX_PDFS_DIR.glob("*.pdf"))] if INBOX_PDFS_DIR.exists() else []
            self._send_json(HTTPStatus.OK, {"items": items, "inbox_files": inbox_files})
            return

        if path == "/api/sources":
            cfg = json.loads(SOURCES_PATH.read_text(encoding="utf-8-sig")) if SOURCES_PATH.exists() else {"sources": []}
            self._send_json(HTTPStatus.OK, cfg)
            return

        if path == "/api/run/status":
            self._send_json(HTTPStatus.OK, RUNNER.snapshot())
            return

        if path == "/api/scheduler":
            self._send_json(HTTPStatus.OK, load_admin_state()["scheduler"])
            return

        if path == "/api/notifications":
            if ADMIN_NOTIFICATIONS_PATH.exists():
                data = json.loads(ADMIN_NOTIFICATIONS_PATH.read_text(encoding="utf-8-sig"))
            else:
                data = {"items": []}
            self._send_json(HTTPStatus.OK, data)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json_body() if int(self.headers.get("Content-Length") or "0") > 0 else {}
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/run":
            ok, msg = start_fetch_job_run(
                source=str(body.get("source") or ""),
                state=str(body.get("state") or "").strip(),
                portal=str(body.get("portal") or "").strip(),
                limit=int(body["limit"]) if body.get("limit") else None,
                dry_run=bool(body.get("dry_run", False)),
                stage=bool(body.get("stage", True)),
            )
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
            return

        if path == "/api/run/rerun-failed":
            mode = str(body.get("mode") or "auto").strip().lower()
            requested_states = [
                str(s).strip().upper()
                for s in (body.get("states") or [])
                if str(s).strip()
            ]
            snap = RUNNER.snapshot()
            failed_codes = [
                str(s.get("code") or "").strip().upper()
                for s in (snap.get("failed_states") or [])
                if str(s.get("code") or "").strip()
            ]
            failed_sources = list(snap.get("failed_sources") or [])
            base_params = snap.get("last_failed_params") or snap.get("last_params") or {}

            if requested_states or (mode in {"auto", "states"} and failed_codes):
                target_codes = requested_states or failed_codes
                ok, msg = start_fetch_job_run(
                    source=str(base_params.get("source") or "freejobalert-statewise") or "freejobalert-statewise",
                    state=",".join(target_codes),
                    limit=max(len(target_codes) * 2, int(base_params.get("limit") or 10)),
                    dry_run=bool(base_params.get("dry_run", False)),
                    stage=bool(base_params.get("stage", True)),
                    rerun_states=target_codes,
                )
                self._send_json(
                    HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                    {"ok": ok, "message": msg, "mode": "states", "states": target_codes},
                )
                return

            if mode in {"auto", "sources"} and failed_sources and not base_params.get("source"):
                first_src = str(failed_sources[0].get("source_id") or "")
                if first_src:
                    ok, msg = start_fetch_job_run(
                        source=first_src,
                        limit=int(base_params.get("limit") or 10),
                        dry_run=bool(base_params.get("dry_run", False)),
                        stage=bool(base_params.get("stage", True)),
                    )
                    self._send_json(
                        HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                        {"ok": ok, "message": msg, "mode": "sources", "source": first_src},
                    )
                    return

            replay = snap.get("last_failed_params") if mode != "last" else snap.get("last_params")
            if not replay:
                replay = snap.get("last_params") or {}
            if not replay:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "No previous run or failed state recorded yet."})
                return

            kind = str(replay.get("kind") or "fetch_jobs")
            if kind == "astro_check_build":
                if os.name == "nt":
                    cmd = ["powershell", "-NoProfile", "-Command", "$env:ASTRO_TELEMETRY_DISABLED='1'; npx astro check; if ($LASTEXITCODE -eq 0) { npm run build }"]
                else:
                    cmd = ["sh", "-c", "ASTRO_TELEMETRY_DISABLED=1 npx astro check && npm run build"]
                ok, msg = _launch_subprocess_task("astro_check_build", cmd, {"ASTRO_TELEMETRY_DISABLED": "1"}, run_params={"kind": "astro_check_build", "cmd": cmd})
            elif kind == "admin_pdf_scan" and isinstance(replay.get("cmd"), list):
                ok, msg = _launch_subprocess_task("admin_pdf_scan", list(replay["cmd"]), run_params=replay)
            else:
                ok, msg = start_fetch_job_run(
                    source=str(replay.get("source") or ""),
                    state=str(replay.get("state") or ""),
                    portal=str(replay.get("portal") or ""),
                    limit=int(replay["limit"]) if replay.get("limit") else None,
                    dry_run=bool(replay.get("dry_run", False)),
                    stage=bool(replay.get("stage", True)),
                )
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg, "mode": kind})
            return

        if path == "/api/keys":
            raw_items: list[dict[str, Any]] = []
            default_cap = int(body.get("daily_rpd_limit") or 250)
            base_priority = int(body.get("priority") or 1)
            label_input = str(body.get("label") or "").strip()

            single_key = str(body.get("api_key") or "").strip()
            bulk_text = str(body.get("api_keys_text") or "").strip()

            parsed_secrets: list[str] = []
            if single_key:
                parsed_secrets.append(single_key)
            if bulk_text:
                for line in re.split(r"[\r\n,;]+", bulk_text):
                    tok = line.strip()
                    if tok and tok not in parsed_secrets:
                        parsed_secrets.append(tok)

            for idx, secret in enumerate(parsed_secrets):
                if len(parsed_secrets) == 1:
                    lbl = label_input or f"Gemini Key ({secret[:6]}...{secret[-4:]})"
                else:
                    lbl = f"{label_input} #{idx + 1}" if label_input else f"Gemini Key #{idx + 1} ({secret[-4:]})"
                raw_items.append(
                    {
                        "api_key": secret,
                        "label": lbl,
                        "priority": base_priority + idx,
                        "daily_rpd_limit": default_cap,
                    }
                )

            added = add_gemini_keys(raw_items)
            if not added:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "No valid Gemini API key provided (keys must be at least 15 characters)."})
                return

            if body.get("test_now", True) and len(added) <= 5:
                probed: list[dict[str, Any]] = []
                for entry in added:
                    res = probe_key_live(str(entry["id"]))
                    probed.append(res or entry)
                added = probed

            self._send_json(HTTPStatus.OK, {"ok": True, "added": added, "summary": _get_sanitized_keys_summary()})
            return

        if path == "/api/keys/test-all":
            store = load_keys_store(seed_from_env=True)
            test_model = str(body.get("model") or "gemini-3.5-flash-lite")
            for k in store.get("keys", []):
                if isinstance(k, dict) and k.get("enabled", True) and k.get("id"):
                    probe_key_live(str(k["id"]), test_model=test_model)
            self._send_json(HTTPStatus.OK, {"ok": True, "summary": _get_sanitized_keys_summary()})
            return

        if path.startswith("/api/keys/") and path.endswith("/test"):
            parts = path.split("/")
            key_id = parts[3] if len(parts) > 3 else ""
            test_model = str(body.get("model") or "gemini-3.5-flash-lite")
            res = probe_key_live(key_id, test_model=test_model)
            if res is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "API key not found"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "key": res, "summary": _get_sanitized_keys_summary()})
            return

        if path.startswith("/api/keys/") and path.endswith("/update"):
            parts = path.split("/")
            key_id = parts[3] if len(parts) > 3 else ""
            updated = update_key_entry(key_id, body)
            if updated is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "API key not found"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "key": updated, "summary": _get_sanitized_keys_summary()})
            return

        if path == "/api/build":
            if os.name == "nt":
                cmd = ["powershell", "-NoProfile", "-Command", "$env:ASTRO_TELEMETRY_DISABLED='1'; npx astro check; if ($LASTEXITCODE -eq 0) { npm run build }"]
            else:
                cmd = ["sh", "-c", "ASTRO_TELEMETRY_DISABLED=1 npx astro check && npm run build"]
            ok, msg = _launch_subprocess_task("astro_check_build", cmd, {"ASTRO_TELEMETRY_DISABLED": "1"}, run_params={"kind": "astro_check_build", "cmd": cmd})
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
            return

        if path == "/api/scheduler":
            state = load_admin_state()
            sched = state["scheduler"]
            sched["enabled"] = bool(body.get("enabled", sched.get("enabled", False)))
            sched["interval_hours"] = max(1, int(body.get("interval_hours", sched.get("interval_hours", 12))))
            sched["stage_for_review"] = bool(body.get("stage_for_review", sched.get("stage_for_review", True)))
            sched["source"] = str(body.get("source", sched.get("source", "")))
            sched["limit"] = max(1, int(body.get("limit", sched.get("limit", 15))))
            sched["next_run_at"] = _compute_next_run(float(sched["interval_hours"])) if sched["enabled"] else None
            save_admin_state(state)
            self._send_json(HTTPStatus.OK, {"ok": True, "scheduler": sched})
            return

        if path == "/api/sources/toggle":
            src_id = str(body.get("id") or "")
            cfg = json.loads(SOURCES_PATH.read_text(encoding="utf-8-sig"))
            updated = False
            for src in cfg.get("sources", []):
                if src.get("id") == src_id:
                    if "enabled" in body:
                        src["enabled"] = bool(body["enabled"])
                    if "max_items" in body and body["max_items"]:
                        src["max_items"] = int(body["max_items"])
                    updated = True
            if not updated:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Source '{src_id}' not found"})
                return
            SOURCES_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._send_json(HTTPStatus.OK, {"ok": True, "sources": cfg["sources"]})
            return

        if path == "/api/jobs":
            target_mode = str(body.get("target") or "publish")
            job_payload = dict(body.get("job") if isinstance(body.get("job"), dict) else body)
            post_type = str(job_payload.get("post_type") or body.get("post_type") or "job").strip().lower()
            is_admission = post_type == "admission"
            if is_admission and not str(job_payload.get("department") or "").strip():
                job_payload["department"] = "Admissions & Entrances"
            dest_dir = STAGING_JOBS_DIR if target_mode == "stage" else (ADMISSIONS_DIR if is_admission else JOBS_DIR)
            try:
                record = _validate_and_normalize_job_payload(job_payload, target_dir=dest_dir)
            except (ValidationError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_file = dest_dir / f"{record.slug}.json"
            if target_mode == "stage":
                serialized = serialize_job_payload(record, for_staging=True)
            elif record.post_type == "admission":
                serialized = serialize_admission_payload(record)
            else:
                serialized = serialize_job_payload(record, for_staging=False)
            out_file.write_text(json.dumps(serialized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            kind_lbl = "Admission" if record.post_type == "admission" else "Job"
            dest_lbl = "review queue" if target_mode == "stage" else ("src/data/admissions/" if record.post_type == "admission" else "src/data/jobs/")
            push_admin_notification(
                "review_required" if target_mode == "stage" else "run_completed",
                f"Manual {kind_lbl} Saved" if target_mode == "stage" else f"Manual {kind_lbl} Published",
                f"'{record.title}' ({record.organization}) saved to {dest_lbl}.",
                action_tab="review" if target_mode == "stage" else "published",
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "slug": record.slug, "target": target_mode, "post_type": record.post_type})
            return

        if (path.startswith("/api/jobs/") or path.startswith("/api/admissions/")) and path.endswith("/menus"):
            parts = path.strip("/").split("/")
            kind = parts[1]
            slug = parts[2]
            target_file = (ADMISSIONS_DIR if kind == "admissions" else JOBS_DIR) / f"{slug}.json"
            if not target_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"{kind[:-1].title()} not found"})
                return
            data = json.loads(target_file.read_text(encoding="utf-8-sig"))
            valid_menus = {"result", "admit-card", "answer-key"}

            existing_menus: list[str] = []
            for m in data.get("homepage_menus") or []:
                mv = str(m).strip().lower()
                if mv in valid_menus and mv not in existing_menus:
                    existing_menus.append(mv)
            for lnk in data.get("important_links") or []:
                if isinstance(lnk, dict):
                    lc = str(lnk.get("category") or "").strip().lower()
                    if lc in valid_menus and lc not in existing_menus:
                        existing_menus.append(lc)

            if isinstance(body.get("homepage_menus"), list):
                new_menus: list[str] = []
                for m in body["homepage_menus"]:
                    mv = str(m or "").strip().lower()
                    if mv in valid_menus and mv not in new_menus:
                        new_menus.append(mv)
            else:
                menu_name = str(body.get("menu") or "").strip().lower()
                if menu_name not in valid_menus:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid homepage menu category."})
                    return
                enabled = bool(body.get("enabled", menu_name not in existing_menus))
                new_menus = list(existing_menus)
                if enabled and menu_name not in new_menus:
                    new_menus.append(menu_name)
                elif not enabled:
                    new_menus = [m for m in new_menus if m != menu_name]

            if new_menus:
                data["homepage_menus"] = new_menus
            else:
                data.pop("homepage_menus", None)

            if isinstance(data.get("important_links"), list):
                for lnk in data["important_links"]:
                    if isinstance(lnk, dict):
                        lc = str(lnk.get("category") or "").strip().lower()
                        if lc and lc not in new_menus:
                            lnk.pop("category", None)
                            lnk.pop("added_at", None)

            target_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._send_json(HTTPStatus.OK, {"ok": True, "slug": slug, "homepage_menus": new_menus})
            return

        if path.startswith("/api/jobs/") and not path.endswith("/archive"):
            slug = path.split("/")[3]
            job_file = JOBS_DIR / f"{slug}.json"
            if not job_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                return
            existing = json.loads(job_file.read_text(encoding="utf-8-sig"))
            raw_job = dict(body.get("job") if isinstance(body.get("job"), dict) else body)
            for keep_key in ("exam_group_id", "fee_structure"):
                if keep_key not in raw_job and existing.get(keep_key) is not None:
                    raw_job[keep_key] = existing[keep_key]
            try:
                record = _validate_and_normalize_job_payload(raw_job, target_dir=JOBS_DIR, keep_existing_slug=slug)
            except (ValidationError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if record.post_type == "admission":
                ADMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
                adm_file = ADMISSIONS_DIR / f"{record.slug}.json"
                adm_file.write_text(json.dumps(serialize_admission_payload(record), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                job_file.unlink(missing_ok=True)
                msg = f"Moved '{record.title}' from Jobs to src/data/admissions/{record.slug}.json."
            else:
                payload = serialize_job_payload(record, for_staging=False)
                if "archived" not in raw_job and existing.get("archived"):
                    payload["archived"] = True
                    if existing.get("archived_at"):
                        payload["archived_at"] = existing["archived_at"]
                job_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                msg = f"Updated '{record.title}' in src/data/jobs/{record.slug}.json."
            push_admin_notification("run_completed", "Published Post Updated", msg, action_tab="published")
            self._send_json(HTTPStatus.OK, {"ok": True, "slug": record.slug, "post_type": record.post_type})
            return

        if path.startswith("/api/admissions/"):
            slug = path.split("/")[3]
            adm_file = ADMISSIONS_DIR / f"{slug}.json"
            if not adm_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Admission not found"})
                return
            existing = json.loads(adm_file.read_text(encoding="utf-8-sig"))
            raw_job = dict(body.get("job") if isinstance(body.get("job"), dict) else body)
            if not str(raw_job.get("department") or "").strip():
                raw_job["department"] = "Admissions & Entrances"
            if not str(raw_job.get("qualification_level") or "").strip():
                raw_job["qualification_level"] = str(raw_job.get("course_level") or existing.get("course_level") or "Graduate")
            if "exam_group_id" not in raw_job and existing.get("exam_group_id") is not None:
                raw_job["exam_group_id"] = existing["exam_group_id"]
            try:
                record = _validate_and_normalize_job_payload(raw_job, target_dir=ADMISSIONS_DIR, keep_existing_slug=slug)
            except (ValidationError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if record.post_type == "job":
                JOBS_DIR.mkdir(parents=True, exist_ok=True)
                job_file = JOBS_DIR / f"{record.slug}.json"
                job_file.write_text(json.dumps(serialize_job_payload(record, for_staging=False), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                adm_file.unlink(missing_ok=True)
                msg = f"Moved '{record.title}' from Admissions to src/data/jobs/{record.slug}.json."
            else:
                adm_file.write_text(json.dumps(serialize_admission_payload(record), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                msg = f"Updated '{record.title}' in src/data/admissions/{record.slug}.json."
            push_admin_notification("run_completed", "Published Admission Updated", msg, action_tab="published")
            self._send_json(HTTPStatus.OK, {"ok": True, "slug": record.slug, "post_type": record.post_type})
            return

        if path.startswith("/api/jobs/") and path.endswith("/archive"):
            slug = path.split("/")[3]
            job_file = JOBS_DIR / f"{slug}.json"
            if not job_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                return
            data = json.loads(job_file.read_text(encoding="utf-8-sig"))
            new_archived = bool(body.get("archived", not bool(data.get("archived", False))))
            data["archived"] = new_archived
            if new_archived:
                data["archived_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                data.pop("archived_at", None)
            job_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._send_json(HTTPStatus.OK, {"ok": True, "archived": new_archived})
            return

        if path.startswith("/api/staging/") and path.endswith("/approve"):
            slug = path.split("/")[3]
            staged_file = STAGING_JOBS_DIR / f"{slug}.json"
            if not staged_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Staged job not found"})
                return
            raw_job = dict(body.get("job") if isinstance(body.get("job"), dict) else json.loads(staged_file.read_text(encoding="utf-8-sig")))
            explicit_pt = str(body.get("post_type") or "").strip().lower()
            raw_pt = explicit_pt if explicit_pt in {"job", "admission"} else str(raw_job.get("post_type") or "job").strip().lower()
            post_type = "admission" if raw_pt == "admission" else "job"
            raw_job["post_type"] = post_type
            is_admission = post_type == "admission"
            if is_admission and not str(raw_job.get("department") or "").strip():
                raw_job["department"] = "Admissions & Entrances"
            elif not is_admission and not str(raw_job.get("department") or "").strip():
                raw_job["department"] = "Civil Services & Administration"
            if not str(raw_job.get("qualification_level") or "").strip():
                raw_job["qualification_level"] = str(raw_job.get("course_level") or "See Official Notification")
            dest_dir = ADMISSIONS_DIR if is_admission else JOBS_DIR
            try:
                record = _validate_and_normalize_job_payload(raw_job, target_dir=dest_dir, keep_existing_slug=slug)
            except (ValidationError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            dest_dir.mkdir(parents=True, exist_ok=True)
            pub_file = dest_dir / f"{record.slug}.json"
            pub_payload = serialize_admission_payload(record) if record.post_type == "admission" else serialize_job_payload(record, for_staging=False)
            pub_file.write_text(json.dumps(pub_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            staged_file.unlink(missing_ok=True)
            dest_rel = "src/data/admissions" if record.post_type == "admission" else "src/data/jobs"
            push_admin_notification(
                "run_completed",
                f"Staged {'Admission' if record.post_type == 'admission' else 'Job'} Approved",
                f"Published '{record.title}' to {dest_rel}/{record.slug}.json.",
                action_tab="published",
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "slug": record.slug, "post_type": record.post_type})
            return

        if path.startswith("/api/staging/") and path.endswith("/reject"):
            slug = path.split("/")[3]
            staged_file = STAGING_JOBS_DIR / f"{slug}.json"
            if not staged_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Staged job not found"})
                return
            data = json.loads(staged_file.read_text(encoding="utf-8-sig"))
            staged_file.unlink(missing_ok=True)
            seen_reg = load_json_registry(SEEN_URLS_PATH, "urls")
            for k in filter(None, {data.get("source_url"), data.get("official_apply_url")}):
                seen_reg["urls"][k] = {
                    "seen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source_id": "admin-rejected",
                    "status": "rejected",
                }
            save_json_registry(SEEN_URLS_PATH, seen_reg)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if path.startswith("/api/pending-pdfs/") and path.endswith("/dismiss"):
            pid = path.split("/")[3]
            pending_reg = load_json_registry(PENDING_PDFS_PATH, "items")
            if pid not in pending_reg["items"]:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Pending item not found"})
                return
            pending_reg["items"][pid]["status"] = "dismissed"
            save_json_registry(PENDING_PDFS_PATH, pending_reg)
            write_pending_markdown_report(pending_reg["items"], PENDING_PDFS_MD_PATH)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/upload-pdf":
            b64_data = str(body.get("pdf_base64") or "")
            if not b64_data:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing pdf_base64 payload"})
                return
            if "," in b64_data and b64_data.startswith("data:"):
                b64_data = b64_data.split(",", 1)[1]
            pdf_bytes = base64.b64decode(b64_data)
            if len(pdf_bytes) > MAX_UPLOAD_BYTES or not pdf_bytes.startswith(b"%PDF"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "File must be a valid PDF under 20 MB."})
                return

            pending_id = str(body.get("pending_id") or "").strip()
            raw_name = Path(str(body.get("filename") or "uploaded.pdf")).stem
            file_stem = pending_id or slugify(raw_name, max_length=60)
            INBOX_PDFS_DIR.mkdir(parents=True, exist_ok=True)
            pdf_dest = INBOX_PDFS_DIR / f"{file_stem}.pdf"
            pdf_dest.write_bytes(pdf_bytes)

            meta: dict[str, Any] = {}
            for key in ("apply_url", "pdf_url", "title", "organization", "department"):
                val = str(body.get(key) or "").strip()
                if val:
                    meta[key] = val
            if meta:
                pdf_dest.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

            stage = bool(body.get("stage", True))
            cmd = [sys.executable, str(SCRIPTS_DIR / "fetch_jobs.py"), "--pdf", str(pdf_dest)]
            if pending_id:
                cmd.extend(["--pending-id", pending_id])
            if meta.get("apply_url"):
                cmd.extend(["--apply-url", meta["apply_url"]])
            if meta.get("pdf_url"):
                cmd.extend(["--pdf-url", meta["pdf_url"]])
            if meta.get("title"):
                cmd.extend(["--title", meta["title"]])
            if meta.get("organization"):
                cmd.extend(["--org", meta["organization"]])
            if meta.get("department"):
                cmd.extend(["--dept", meta["department"]])
            if stage:
                cmd.append("--stage")

            ok, msg = _launch_subprocess_task("admin_pdf_scan", cmd)
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg, "saved_path": str(pdf_dest.relative_to(REPO_ROOT))})
            return

        if path == "/api/notifications/read":
            if ADMIN_NOTIFICATIONS_PATH.exists():
                data = json.loads(ADMIN_NOTIFICATIONS_PATH.read_text(encoding="utf-8-sig"))
                for item in data.get("items", []):
                    if isinstance(item, dict):
                        item["read"] = True
                ADMIN_NOTIFICATIONS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/keys/"):
            key_id = path.split("/")[3]
            if not delete_key_entry(key_id):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "API key not found"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "summary": _get_sanitized_keys_summary()})
            return
        if path.startswith("/api/jobs/"):
            slug = path.split("/")[3]
            job_file = JOBS_DIR / f"{slug}.json"
            if not job_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                return
            job_file.unlink()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path.startswith("/api/admissions/"):
            slug = path.split("/")[3]
            adm_file = ADMISSIONS_DIR / f"{slug}.json"
            if not adm_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Admission not found"})
                return
            adm_file.unlink()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the ChayanDesk Local Admin Panel.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    args = parser.parse_args()

    load_dotenv_file(REPO_ROOT / ".env")
    _init_runner_from_disk()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), AdminHandler)
    print(f"ChayanDesk Admin Panel running at http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down admin server.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

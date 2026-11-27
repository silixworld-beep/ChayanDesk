#!/usr/bin/env python3
"""Multi-key Gemini API vault, live quota/health probing, usage tracking, and failover."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

KEYS_PATH = Path(__file__).resolve().parent / "gemini_keys.json"
DEFAULT_DAILY_RPD_CAP = 250


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_api_key(key: str) -> str:
    """Mask API key safely for UI display."""
    k = key.strip()
    if len(k) <= 10:
        return "***"
    return f"{k[:6]}...{k[-4:]}"


def _default_key_entry(
    key_id: str,
    label: str,
    api_key: str,
    priority: int = 1,
    daily_rpd_limit: int = DEFAULT_DAILY_RPD_CAP,
) -> dict[str, Any]:
    return {
        "id": key_id,
        "label": label,
        "api_key": api_key.strip(),
        "masked_key": mask_api_key(api_key),
        "enabled": True,
        "priority": priority,
        "daily_rpd_limit": daily_rpd_limit,
        "status": "active",
        "requests_today": 0,
        "success_today": 0,
        "rate_limit_hits_today": 0,
        "tokens_today": 0,
        "usage_date": _today_utc_str(),
        "cooldown_until": None,
        "exhausted_models": {},
        "last_used_at": None,
        "last_checked_at": None,
        "last_http_status": None,
        "last_quota_metric": None,
        "last_quota_value": None,
        "last_error": None,
    }


def _refresh_daily_resets(keys: list[dict[str, Any]]) -> bool:
    """Reset counters on UTC day rollover and restore expired cooldowns."""
    today = _today_utc_str()
    now_utc = datetime.now(timezone.utc)
    changed = False

    for entry in keys:
        if entry.get("usage_date") != today:
            entry["usage_date"] = today
            entry["requests_today"] = 0
            entry["success_today"] = 0
            entry["rate_limit_hits_today"] = 0
            entry["tokens_today"] = 0
            entry["exhausted_models"] = {}
            if entry.get("status") == "exhausted":
                entry["status"] = "active"
                entry["cooldown_until"] = None
                entry["last_error"] = None
            changed = True

        cd = entry.get("cooldown_until")
        if cd and entry.get("status") == "exhausted":
            try:
                cd_dt = datetime.strptime(cd, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if now_utc >= cd_dt:
                    entry["status"] = "active"
                    entry["cooldown_until"] = None
                    entry["exhausted_models"] = {}
                    changed = True
            except ValueError:
                pass

    return changed


def load_keys_store(*, seed_from_env: bool = True) -> dict[str, Any]:
    """Load scripts/gemini_keys.json, seeding from GEMINI_API_KEY env var if empty."""
    data: dict[str, Any] = {"keys": []}
    if KEYS_PATH.exists():
        try:
            loaded = json.loads(KEYS_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict) and isinstance(loaded.get("keys"), list):
                data = loaded
        except Exception:
            data = {"keys": []}

    keys: list[dict[str, Any]] = data["keys"]
    changed = _refresh_daily_resets(keys)

    if seed_from_env and not keys:
        env_key = os.getenv("GEMINI_API_KEY", "").strip()
        if env_key:
            keys.append(_default_key_entry("key-1", "Primary Key (.env)", env_key, priority=1))
            changed = True

    if changed and KEYS_PATH.parent.exists():
        save_keys_store(data)
    return data


def save_keys_store(data: dict[str, Any]) -> None:
    """Persist scripts/gemini_keys.json safely."""
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sanitize_key_for_ui(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of key metadata with raw api_key redacted."""
    copy = {k: v for k, v in entry.items() if k != "api_key"}
    copy["masked_key"] = entry.get("masked_key") or mask_api_key(str(entry.get("api_key") or ""))
    cap = int(entry.get("daily_rpd_limit") or DEFAULT_DAILY_RPD_CAP)
    used = int(entry.get("requests_today") or 0)
    copy["estimated_remaining"] = max(0, cap - used)
    return copy


def add_gemini_keys(
    raw_keys: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add one or more new Gemini API keys without duplicating existing keys."""
    store = load_keys_store(seed_from_env=False)
    keys: list[dict[str, Any]] = store["keys"]
    existing_values = {str(k.get("api_key") or "").strip(): k for k in keys}
    added: list[dict[str, Any]] = []

    for idx, item in enumerate(raw_keys):
        secret = str(item.get("api_key") or "").strip()
        if len(secret) < 15:
            continue
        label = str(item.get("label") or f"Gemini Key {len(keys) + 1}").strip()
        cap = max(1, int(item.get("daily_rpd_limit") or DEFAULT_DAILY_RPD_CAP))
        priority = int(item.get("priority") or (len(keys) + 1))

        if secret in existing_values:
            existing_values[secret]["label"] = label
            existing_values[secret]["enabled"] = True
            existing_values[secret]["daily_rpd_limit"] = cap
            existing_values[secret]["status"] = "active"
            existing_values[secret]["cooldown_until"] = None
            added.append(existing_values[secret])
            continue

        key_id = f"key-{int(time.time() * 1000)}-{idx + 1}"
        entry = _default_key_entry(key_id, label, secret, priority=priority, daily_rpd_limit=cap)
        keys.append(entry)
        existing_values[secret] = entry
        added.append(entry)

    keys.sort(key=lambda x: (int(x.get("priority") or 99), str(x.get("id") or "")))
    save_keys_store(store)
    return [sanitize_key_for_ui(e) for e in added]


def update_key_entry(key_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Update enabled, label, priority, daily_rpd_limit, or reset status."""
    store = load_keys_store(seed_from_env=False)
    for entry in store["keys"]:
        if entry.get("id") == key_id:
            if "enabled" in updates:
                entry["enabled"] = bool(updates["enabled"])
            if "label" in updates and str(updates["label"]).strip():
                entry["label"] = str(updates["label"]).strip()
            if "priority" in updates:
                entry["priority"] = int(updates["priority"])
            if "daily_rpd_limit" in updates:
                entry["daily_rpd_limit"] = max(1, int(updates["daily_rpd_limit"]))
            if updates.get("reset"):
                entry["status"] = "active"
                entry["cooldown_until"] = None
                entry["exhausted_models"] = {}
                entry["last_error"] = None
            store["keys"].sort(key=lambda x: (int(x.get("priority") or 99), str(x.get("id") or "")))
            save_keys_store(store)
            return sanitize_key_for_ui(entry)
    return None


def delete_key_entry(key_id: str) -> bool:
    store = load_keys_store(seed_from_env=False)
    before = len(store["keys"])
    store["keys"] = [k for k in store["keys"] if k.get("id") != key_id]
    if len(store["keys"]) < before:
        save_keys_store(store)
        return True
    return False


def get_ordered_active_keys() -> list[dict[str, Any]]:
    """Return enabled keys not currently exhausted or invalid, sorted by priority."""
    store = load_keys_store(seed_from_env=True)
    active = [
        k for k in store["keys"]
        if k.get("enabled", True) and k.get("status") not in {"exhausted", "invalid"}
    ]
    active.sort(key=lambda x: (int(x.get("priority") or 99), int(x.get("requests_today") or 0)))
    return active


def parse_quota_error_details(err_msg: str) -> dict[str, Optional[str]]:
    """Extract quotaMetric, quotaValue, and retryDelay from Gemini 429 error text."""
    metric_m = re.search(r"quotaMetric['\"]?\s*:\s*['\"]([^'\"]+)['\"]", err_msg)
    val_m = re.search(r"quotaValue['\"]?\s*:\s*['\"]?([0-9]+)['\"]?", err_msg)
    delay_m = re.search(r"retryDelay['\"]?\s*:\s*['\"]([0-9.]+s)['\"]", err_msg)
    return {
        "quota_metric": metric_m.group(1) if metric_m else None,
        "quota_value": val_m.group(1) if val_m else None,
        "retry_delay": delay_m.group(1) if delay_m else None,
    }


def record_key_success(key_id: str, tokens: int = 0) -> None:
    """Increment live usage counters after a successful Gemini API call."""
    store = load_keys_store(seed_from_env=False)
    for entry in store["keys"]:
        if entry.get("id") == key_id:
            entry["requests_today"] = int(entry.get("requests_today") or 0) + 1
            entry["success_today"] = int(entry.get("success_today") or 0) + 1
            entry["tokens_today"] = int(entry.get("tokens_today") or 0) + max(0, int(tokens))
            entry["status"] = "active"
            entry["last_used_at"] = _now_utc_iso()
            entry["last_http_status"] = 200
            entry["last_error"] = None
            save_keys_store(store)
            return


def record_key_model_429(key_id: str, model_name: str, err_msg: str) -> dict[str, Optional[str]]:
    """Record a 429 rate-limit/quota hit on a specific model for this key."""
    parsed = parse_quota_error_details(err_msg)
    store = load_keys_store(seed_from_env=False)
    for entry in store["keys"]:
        if entry.get("id") == key_id:
            entry["requests_today"] = int(entry.get("requests_today") or 0) + 1
            entry["rate_limit_hits_today"] = int(entry.get("rate_limit_hits_today") or 0) + 1
            entry["last_used_at"] = _now_utc_iso()
            entry["last_http_status"] = 429
            if parsed["quota_metric"]:
                entry["last_quota_metric"] = parsed["quota_metric"]
            if parsed["quota_value"]:
                entry["last_quota_value"] = parsed["quota_value"]
                try:
                    if "per_day" in (parsed["quota_metric"] or "").lower() or "rpd" in (parsed["quota_metric"] or "").lower():
                        entry["daily_rpd_limit"] = int(parsed["quota_value"])
                except ValueError:
                    pass
            entry["last_error"] = err_msg.splitlines()[0][:240]
            ex_models = entry.get("exhausted_models")
            if not isinstance(ex_models, dict):
                ex_models = {}
            ex_models[model_name] = _now_utc_iso()
            entry["exhausted_models"] = ex_models
            save_keys_store(store)
            break
    return parsed


def mark_key_exhausted(key_id: str, reason: str, cooldown_minutes: int = 60) -> None:
    """Mark a key exhausted with automatic cooldown until next check/midnight."""
    store = load_keys_store(seed_from_env=False)
    cooldown_dt = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
    for entry in store["keys"]:
        if entry.get("id") == key_id:
            entry["status"] = "exhausted"
            entry["cooldown_until"] = cooldown_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            entry["last_error"] = reason[:240]
            save_keys_store(store)
            return


def mark_key_invalid(key_id: str, reason: str) -> None:
    """Mark an API key invalid after 400/401/403 authentication errors."""
    store = load_keys_store(seed_from_env=False)
    for entry in store["keys"]:
        if entry.get("id") == key_id:
            entry["status"] = "invalid"
            entry["last_http_status"] = 403
            entry["last_error"] = reason[:240]
            save_keys_store(store)
            return


def probe_key_live(key_id: str, test_model: str = "gemini-3.5-flash-lite") -> Optional[dict[str, Any]]:
    """Probe a stored Gemini API key live (models.list + tiny 1-token ping) to inspect status & 429 quota metrics."""
    from google import genai
    from google.genai import types

    store = load_keys_store(seed_from_env=False)
    target = next((k for k in store["keys"] if k.get("id") == key_id), None)
    if target is None:
        return None

    secret = str(target.get("api_key") or "").strip()
    client = genai.Client(api_key=secret, http_options=types.HttpOptions(timeout=15000))
    target["last_checked_at"] = _now_utc_iso()

    try:
        # Step 1: validate key via models.list()
        models = [m.name for m in client.models.list() if "flash" in (m.name or "").lower()]
        target["available_flash_models"] = len(models)

        # Step 2: minimal generateContent probe to check live 200 vs 429 QuotaFailure
        resp = client.models.generate_content(
            model=test_model,
            contents="Reply with OK",
            config=types.GenerateContentConfig(max_output_tokens=4, temperature=0.0),
        )
        tokens = int(getattr(getattr(resp, "usage_metadata", None), "total_token_count", 0) or 0)
        target["status"] = "active"
        target["cooldown_until"] = None
        target["last_http_status"] = 200
        target["last_error"] = None
        target["requests_today"] = int(target.get("requests_today") or 0) + 1
        target["success_today"] = int(target.get("success_today") or 0) + 1
        target["tokens_today"] = int(target.get("tokens_today") or 0) + tokens
    except Exception as exc:
        err_msg = str(exc)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            parsed = parse_quota_error_details(err_msg)
            target["status"] = "exhausted"
            target["last_http_status"] = 429
            target["rate_limit_hits_today"] = int(target.get("rate_limit_hits_today") or 0) + 1
            target["last_quota_metric"] = parsed["quota_metric"]
            target["last_quota_value"] = parsed["quota_value"]
            target["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            target["last_error"] = err_msg.splitlines()[0][:240]
        elif "503" in err_msg or "UNAVAILABLE" in err_msg:
            target["status"] = "active"
            target["last_http_status"] = 503
            target["last_error"] = f"Model {test_model} temporarily busy (503), key is valid."
        elif "API_KEY_INVALID" in err_msg or "400" in err_msg or "401" in err_msg or "403" in err_msg:
            target["status"] = "invalid"
            target["last_http_status"] = 403
            target["last_error"] = err_msg.splitlines()[0][:240]
        else:
            target["last_error"] = err_msg.splitlines()[0][:240]

    save_keys_store(store)
    return sanitize_key_for_ui(target)

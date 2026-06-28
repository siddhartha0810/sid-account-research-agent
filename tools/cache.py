import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# Local JSON cache — fast pre-flight check before any API call.
# Stores one entry per researched company keyed by normalized name.
# Complements the HubSpot notes check: cache is instant (no network),
# HubSpot is authoritative (reflects manual team edits too).
#
# Cache file: config.CACHE_FILE_PATH (default: cache.json)
# Shadow mode: check_cache reads normally, mark_researched prints instead of writing.
# ---------------------------------------------------------------------------

CACHE_PATH   = Path(config.CACHE_FILE_PATH)
STALE_DAYS   = 30   # entries older than this are treated as stale
_lock        = threading.Lock()   # safe for single-process concurrent tool calls


# ---------------------------------------------------------------------------
# Tool 1 — check_cache
# ---------------------------------------------------------------------------

_CHECK_SCHEMA = {
    "name": "check_cache",
    "description": (
        "Check the local cache to see if this company has been researched before. "
        "Call this before any HubSpot or Origami calls — it costs nothing. "
        "Returns cached=true with metadata if found and not stale (within 30 days). "
        "If cached=true and is_stale=false, skip all enrichment steps and go straight "
        "to building the battle card using the cached data. "
        "If cached=true and is_stale=true, re-run enrichment to refresh."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company name as submitted by the AE.",
            }
        },
        "required": ["company_name"],
    },
}


def run_check_cache(company_name: str) -> dict:
    company_name = company_name.strip()
    if not company_name:
        return _error("company_name is required.", tool="check_cache")

    cache = _load()
    key   = _normalize(company_name)
    entry = cache.get(key)

    if not entry:
        return {
            "cached":    False,
            "is_stale":  False,
            "days_since": None,
            "last_run":   None,
        }

    researched_at = entry.get("researched_at", "")
    days_since    = _days_since(researched_at)
    is_stale      = days_since is None or days_since > STALE_DAYS

    return {
        "cached":     True,
        "is_stale":   is_stale,
        "days_since": days_since,
        "last_run":   entry,
    }


# ---------------------------------------------------------------------------
# Tool 2 — mark_researched
# ---------------------------------------------------------------------------

_MARK_SCHEMA = {
    "name": "mark_researched",
    "description": (
        "Write a completed research run to the local cache. "
        "Call this after the battle card is posted (or after any terminal outcome: "
        "low_confidence, enrichment_failed, skipped). "
        "Always call this as the second-to-last step, just before log_to_linear."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company name exactly as submitted.",
            },
            "hs_object_id": {
                "type": "string",
                "description": "HubSpot company object ID.",
            },
            "run_status": {
                "type": "string",
                "enum": ["battle_card_posted", "skipped_cached", "low_confidence_warning", "enrichment_failed", "error"],
                "description": "Outcome of this run.",
            },
            "confidence_score": {
                "type": "number",
                "description": "Origami confidence score. Omit if enrichment was skipped.",
            },
            "slack_permalink": {
                "type": "string",
                "description": "Slack message permalink if battle card was posted.",
            },
            "linear_issue_url": {
                "type": "string",
                "description": "Linear issue URL from log_to_linear.",
            },
            "ae_name": {
                "type": "string",
                "description": "AE who submitted this company.",
            },
        },
        "required": ["company_name", "hs_object_id", "run_status", "ae_name"],
    },
}


def run_mark_researched(
    company_name: str,
    hs_object_id: str,
    run_status: str,
    ae_name: str,
    confidence_score: float | None = None,
    slack_permalink: str | None = None,
    linear_issue_url: str | None = None,
) -> dict:
    company_name = company_name.strip()
    if not company_name:
        return _error("company_name is required.", tool="mark_researched")

    entry = {
        "original_name":   company_name,
        "hs_object_id":    hs_object_id,
        "researched_at":   datetime.now(timezone.utc).isoformat(),
        "run_status":      run_status,
        "ae_name":         ae_name,
        "confidence_score": confidence_score,
        "slack_permalink":  slack_permalink,
        "linear_issue_url": linear_issue_url,
    }
    # Strip None values to keep the cache file clean
    entry = {k: v for k, v in entry.items() if v is not None}

    if config.SHADOW_MODE:
        return _shadow_mark(company_name, entry)

    if not config.is_write_allowed("mark_researched"):
        return _error("mark_researched is not on the write allowlist.", tool="mark_researched")

    return _write_entry(company_name, entry)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _load() -> dict:
    with _lock:
        if not CACHE_PATH.exists():
            return {}
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # Guardrail: corrupt or unreadable cache → treat as empty, don't crash
            return {}


def _write_entry(company_name: str, entry: dict) -> dict:
    key = _normalize(company_name)
    with _lock:
        cache = _load()

        # Preserve run_count across runs
        prior = cache.get(key, {})
        entry["run_count"] = prior.get("run_count", 0) + 1

        cache[key] = entry
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump(cache, f, indent=2)
        except OSError as e:
            return _error(f"Failed to write cache file: {e}", tool="mark_researched")

    return {"marked": True, "key": key, "run_count": entry["run_count"], "source": "cache"}


def _shadow_mark(company_name: str, entry: dict) -> dict:
    key = _normalize(company_name)
    print("\n" + "=" * 60)
    print(f"[SHADOW] Cache write — would store entry for '{company_name}':")
    for k, v in entry.items():
        print(f"  {k}: {v}")
    print("=" * 60 + "\n")
    return {"marked": True, "key": key, "run_count": 1, "source": "shadow"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    return name.strip().lower()


def _days_since(iso_timestamp: str) -> int | None:
    if not iso_timestamp:
        return None
    try:
        past = datetime.fromisoformat(iso_timestamp)
        if past.tzinfo is None:
            past = past.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - past
        return delta.days
    except ValueError:
        return None


def _error(message: str, tool: str = "cache") -> dict:
    return {"cached": False, "marked": False, "error": message, "source": tool}


# ---------------------------------------------------------------------------
# Tool registry — imported by agent.py
# ---------------------------------------------------------------------------

TOOLS = [
    {"schema": _CHECK_SCHEMA, "fn": run_check_cache},
    {"schema": _MARK_SCHEMA,  "fn": run_mark_researched},
]

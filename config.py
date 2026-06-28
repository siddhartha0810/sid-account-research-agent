import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        print(f"[config] ERROR: required env var '{key}' is missing. Check your .env file.")
        sys.exit(1)
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Shadow mode — all writes are blocked when True.
# Slack and Linear print to stdout instead of calling live APIs.
# HubSpot and Origami load from mocks/ instead of making HTTP calls.
# ---------------------------------------------------------------------------
SHADOW_MODE: bool = _optional("SHADOW_MODE", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Guardrail: hard cap on Claude tool-loop iterations.
# If Claude hasn't reached end_turn by this count, the agent aborts and logs.
# Prevents runaway loops from burning API credits or hanging indefinitely.
# ---------------------------------------------------------------------------
MAX_LOOP_ITERATIONS: int = int(_optional("MAX_LOOP_ITERATIONS", "10"))

# ---------------------------------------------------------------------------
# Guardrail: explicit allowlist of tools permitted to perform writes.
# Even if a tool attempts a write, it is blocked unless its name appears here
# AND SHADOW_MODE is False. In shadow mode this list is ignored entirely.
# ---------------------------------------------------------------------------
WRITE_PERMITTED_TOOLS: set[str] = {
    "post_to_slack",
    "log_to_linear",
    "mark_researched",
    "write_enrichment_to_hubspot",
}

# ---------------------------------------------------------------------------
# Guardrail: company name validation limits.
# Rejects submissions that are clearly garbage before any API call is made.
# ---------------------------------------------------------------------------
COMPANY_NAME_MIN_LENGTH: int = 2
COMPANY_NAME_MAX_LENGTH: int = 120

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
CACHE_FILE_PATH: str = _optional("CACHE_FILE_PATH", "cache.json")

# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
CLAUDE_MODEL: str = _optional("CLAUDE_MODEL", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------
HUBSPOT_API_KEY: str = "" if SHADOW_MODE else _require("HUBSPOT_API_KEY")
HUBSPOT_BASE_URL: str = _optional("HUBSPOT_BASE_URL", "https://api.hubapi.com")

# ---------------------------------------------------------------------------
# Origami (origami.chat)
# Base URL : https://origami.chat/api/v1
# Auth     : Authorization: Bearer og_live_<key>  (created in Settings → API Keys)
# Rate limits: 100 req/min general, 10 req/min for row inserts
# Docs     : https://docs.origami.chat
# ---------------------------------------------------------------------------
ORIGAMI_API_KEY: str = "" if SHADOW_MODE else _require("ORIGAMI_API_KEY")
ORIGAMI_TABLE_ID: str = _optional("ORIGAMI_TABLE_ID", "")  # tableId for the company enrichment table

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN: str = "" if SHADOW_MODE else _require("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID: str = _optional("SLACK_CHANNEL_ID", "")

# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------
LINEAR_API_KEY: str = "" if SHADOW_MODE else _require("LINEAR_API_KEY")
LINEAR_TEAM_ID: str = _optional("LINEAR_TEAM_ID", "")


def is_write_allowed(tool_name: str) -> bool:
    """
    Central guardrail check called by every tool before performing a write.
    Returns False in shadow mode or if the tool is not on the allowlist.
    """
    if SHADOW_MODE:
        return False
    return tool_name in WRITE_PERMITTED_TOOLS


def validate_company_name(name: str) -> tuple[bool, str]:
    """
    Returns (is_valid, reason). Called by runner before the agent starts.
    Guardrail: stops garbage input from reaching any downstream API.
    """
    name = name.strip()
    if len(name) < COMPANY_NAME_MIN_LENGTH:
        return False, f"Company name too short (min {COMPANY_NAME_MIN_LENGTH} chars)."
    if len(name) > COMPANY_NAME_MAX_LENGTH:
        return False, f"Company name too long (max {COMPANY_NAME_MAX_LENGTH} chars)."
    if not any(c.isalpha() for c in name):
        return False, "Company name must contain at least one letter."
    return True, ""


def summarize() -> dict:
    """Returns a loggable snapshot of active config (no secrets)."""
    return {
        "shadow_mode": SHADOW_MODE,
        "claude_model": CLAUDE_MODEL,
        "max_loop_iterations": MAX_LOOP_ITERATIONS,
        "cache_file": CACHE_FILE_PATH,
        "slack_channel": SLACK_CHANNEL_ID,
        "linear_team": LINEAR_TEAM_ID,
        "write_permitted_tools": sorted(WRITE_PERMITTED_TOOLS),
    }

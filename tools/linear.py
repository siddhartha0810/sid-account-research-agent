import json
from datetime import datetime, timezone

import httpx

import config

# ---------------------------------------------------------------------------
# Linear GraphQL API — Issue logging
#
# Endpoint  : POST https://api.linear.app/graphql
# Auth      : Authorization: <API_KEY>  (no Bearer prefix for personal API keys)
#             Authorization: Bearer <token>  (for OAuth tokens)
# Docs      : https://linear.app/developers/graphql
#
# This tool logs every agent run as a Linear issue so the team has a full
# audit trail of every account researched, skipped, or flagged.
# Shadow mode: prints the would-be issue to stdout — no write to Linear.
# ---------------------------------------------------------------------------

ENDPOINT = "https://api.linear.app/graphql"

# GraphQL mutation — variables injected separately to avoid string interpolation.
# Requests the issue url + identifier back so we can surface a clickable link.
ISSUE_CREATE_MUTATION = """
mutation IssueCreate($title: String!, $description: String!, $teamId: String!) {
  issueCreate(
    input: {
      title: $title
      description: $description
      teamId: $teamId
    }
  ) {
    success
    issue {
      id
      identifier
      title
      url
      createdAt
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Tool schema — what Claude sees
# ---------------------------------------------------------------------------

SCHEMA = {
    "name": "log_to_linear",
    "description": (
        "Log the completed agent run as a Linear issue for audit and tracking. "
        "Call this as the final step of every run regardless of outcome — "
        "successful battle cards, skipped (already researched), low confidence warnings, "
        "and errors all get logged. Never skip this step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company name that was researched.",
            },
            "run_status": {
                "type": "string",
                "enum": ["battle_card_posted", "skipped_cached", "low_confidence_warning", "enrichment_failed", "error"],
                "description": "Outcome of this agent run.",
            },
            "ae_name": {
                "type": "string",
                "description": "Name of the AE who submitted the company in HubSpot.",
            },
            "summary": {
                "type": "string",
                "description": "1–3 sentence summary of what happened — what was found, what was posted or skipped, and why.",
            },
            "confidence_score": {
                "type": "number",
                "description": "Origami enrichment confidence score (0.0–1.0). Omit if enrichment was skipped.",
            },
            "battle_card_url": {
                "type": "string",
                "description": "Slack message URL or permalink if a battle card was posted. Omit otherwise.",
            },
        },
        "required": ["company_name", "run_status", "ae_name", "summary"],
    },
}


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def run(
    company_name: str,
    run_status: str,
    ae_name: str,
    summary: str,
    confidence_score: float | None = None,
    battle_card_url: str | None = None,
) -> dict:
    """
    Guardrails applied:
    - Shadow mode  → prints issue to stdout, no write to Linear.
    - Write check  → blocked unless log_to_linear is on the allowlist.
    - Empty inputs → rejected before any network call.
    """
    company_name = company_name.strip()
    ae_name = ae_name.strip()
    summary = summary.strip()

    if not company_name or not ae_name or not summary:
        return _error("company_name, ae_name, and summary are all required.")

    title = _build_title(company_name, run_status)
    description = _build_description(
        company_name=company_name,
        run_status=run_status,
        ae_name=ae_name,
        summary=summary,
        confidence_score=confidence_score,
        battle_card_url=battle_card_url,
    )

    if config.SHADOW_MODE:
        return _shadow_log(title, description)

    # Guardrail: confirm write is permitted even outside shadow mode
    if not config.is_write_allowed("log_to_linear"):
        return _error("Write to Linear is not permitted — log_to_linear is not on the allowlist.")

    return _create_issue(title, description)


def _create_issue(title: str, description: str) -> dict:
    team_id = config.LINEAR_TEAM_ID
    if not team_id:
        return _error("LINEAR_TEAM_ID is not set. Add the team ID from your Linear workspace settings.")

    # Personal API keys: no Bearer prefix. OAuth tokens: Bearer prefix.
    # LINEAR_API_KEY stores whichever — we detect by checking for "lin_api_" prefix.
    api_key = config.LINEAR_API_KEY
    auth_header = api_key if api_key.startswith("lin_api_") else f"Bearer {api_key}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": auth_header,
    }

    payload = {
        "query": ISSUE_CREATE_MUTATION,
        "variables": {
            "title": title,
            "description": description,
            "teamId": team_id,
        },
    }

    try:
        response = httpx.post(ENDPOINT, json=payload, headers=headers, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _error(f"Linear API error {e.response.status_code}: {e.response.text}")
    except httpx.RequestError as e:
        return _error(f"Linear network error: {str(e)}")

    body = response.json()

    # GraphQL returns 200 even on errors — check the errors array
    if body.get("errors"):
        messages = [e.get("message", "unknown") for e in body["errors"]]
        return _error(f"Linear GraphQL error: {'; '.join(messages)}")

    result = body.get("data", {}).get("issueCreate", {})
    if not result.get("success"):
        return _error("Linear issueCreate returned success=false with no error details.")

    issue = result.get("issue", {})
    return {
        "logged": True,
        "issue_id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "created_at": issue.get("createdAt"),
        "source": "linear_live",
    }


def _shadow_log(title: str, description: str) -> dict:
    print("\n" + "=" * 60)
    print("[SHADOW] Linear issue — would create:")
    print(f"  Title      : {title}")
    print(f"  Team ID    : {config.LINEAR_TEAM_ID or '(not set)'}")
    print(f"  Description:\n")
    for line in description.splitlines():
        print(f"    {line}")
    print("=" * 60 + "\n")

    return {
        "logged": True,
        "issue_id": "shadow_issue_001",
        "identifier": "SHADOW-1",
        "title": title,
        "url": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "shadow",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "battle_card_posted":      "Battle Card Posted",
    "skipped_cached":          "Skipped — Already Researched",
    "low_confidence_warning":  "Low Confidence Warning",
    "enrichment_failed":       "Enrichment Failed",
    "error":                   "Error",
}

def _build_title(company_name: str, run_status: str) -> str:
    label = _STATUS_LABELS.get(run_status, run_status)
    return f"[Account Research] {company_name} — {label}"


def _build_description(
    company_name: str,
    run_status: str,
    ae_name: str,
    summary: str,
    confidence_score: float | None,
    battle_card_url: str | None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    label = _STATUS_LABELS.get(run_status, run_status)

    lines = [
        f"## Account Research Run — {company_name}",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| Company | {company_name} |",
        f"| AE | {ae_name} |",
        f"| Status | {label} |",
        f"| Run at | {now} |",
    ]

    if confidence_score is not None:
        flag = "  ⚠️ below threshold (0.6)" if confidence_score < 0.6 else ""
        lines.append(f"| Enrichment confidence | {confidence_score:.2f}{flag} |")

    if battle_card_url:
        lines.append(f"| Battle card | {battle_card_url} |")

    lines += [
        "",
        "## Summary",
        "",
        summary,
        "",
        "---",
        "_Logged automatically by the TrueRestore account research agent._",
    ]

    return "\n".join(lines)


def _error(message: str) -> dict:
    return {"logged": False, "error": message, "source": "linear"}

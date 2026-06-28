import asyncio
import json
import httpx
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# Origami (origami.chat) — Enrichment API — Async
#
# Base URL  : https://origami.chat/api/v1
# Auth      : Authorization: Bearer og_live_<key>  (Settings → API Keys)
# Rate limit: 100 req/min general · 10 req/min row inserts
# Docs      : https://docs.origami.chat
#
# Async pattern:
#   1. POST /tables/{tableId}/rows  → submit rows, receive { batchId }
#   2. GET  /batches/{batchId}      → poll until status == "complete" | "failed"
#   3. Return result payload from the final batch response.
# ---------------------------------------------------------------------------

BASE_URL = "https://origami.chat/api/v1"
SUBMIT_ENDPOINT = "/tables/{table_id}/rows"
BATCH_ENDPOINT = "/batches/{batch_id}"

MOCK_PATH = Path(__file__).parent.parent / "mocks" / "origami_enrichment.json"

# Async guardrail constants
MAX_POLL_ATTEMPTS = 12          # hard cap — abort after this many polls
POLL_INTERVAL_SECONDS = 5       # wait between polls
REQUEST_TIMEOUT_SECONDS = 15.0  # per-request timeout

# Confidence guardrail — Origami returns a match confidence score (0.0–1.0).
# Below this threshold the agent warns the AE instead of posting the battle card.
CONFIDENCE_THRESHOLD = 0.6

# ---------------------------------------------------------------------------
# Tool schema — what Claude sees
# ---------------------------------------------------------------------------

SCHEMA = {
    "name": "enrich_company",
    "description": (
        "Asynchronously enrich a company via Origami Risk. "
        "Returns firmographic data, tech stack, key contacts, buying signals, "
        "competitive landscape, and ICP fit score. "
        "Only call this if the cache indicates the company has NOT been previously researched. "
        "IMPORTANT: check the 'low_confidence' flag in the response before proceeding. "
        "If low_confidence is true, do NOT build or post a battle card — instead warn the AE "
        "that enrichment data quality is too low (confidence_score will tell you how low). "
        "Only proceed to build_battle_card if low_confidence is false."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Full company name as submitted by the AE.",
            },
            "domain": {
                "type": "string",
                "description": "Company website domain (e.g. summitrestoresdfw.com). Improves match accuracy.",
            },
        },
        "required": ["company_name"],
    },
}


# ---------------------------------------------------------------------------
# Public entry point — called by the agent tool dispatcher
# ---------------------------------------------------------------------------

def run(company_name: str, domain: str = "") -> dict:
    """
    Synchronous wrapper so the agent's tool dispatcher doesn't need to be async-aware.
    Spins up an event loop for the async enrichment flow.
    """
    company_name = company_name.strip()
    domain = domain.strip()

    # Guardrail: reject blank input before any I/O
    if not company_name:
        return _error("company_name is required and cannot be blank.")

    if config.SHADOW_MODE:
        return asyncio.run(_load_mock())

    return asyncio.run(_enrich(company_name, domain))


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------

async def _enrich(company_name: str, domain: str) -> dict:
    headers = {
        "Authorization": f"Bearer {config.ORIGAMI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:

        # Step 1 — POST to /tables, get batchId back
        submission = await _submit_job(client, company_name, domain)
        if "error" in submission:
            return submission

        batch_id = submission["batch_id"]

        # Step 2 — poll /batches/{batchId} until complete or failed
        result = await _poll_batch(client, batch_id)
        return result


async def _submit_job(client: httpx.AsyncClient, company_name: str, domain: str) -> dict:
    table_id = config.ORIGAMI_TABLE_ID
    if not table_id:
        return _error("ORIGAMI_TABLE_ID is not set. Add the company enrichment table ID from origami.chat.")

    # POST raw row data — Origami enriches it and returns enriched columns in the batch result.
    payload = {
        "rows": [
            {
                "company_name": company_name,
                **({"domain": domain} if domain else {}),
            }
        ]
    }

    endpoint = SUBMIT_ENDPOINT.format(table_id=table_id)

    try:
        response = await client.post(endpoint, json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _error(f"Origami submit error {e.response.status_code}: {e.response.text}")
    except httpx.RequestError as e:
        return _error(f"Origami network error on submit: {str(e)}")

    data = response.json()
    batch_id = data.get("batchId")

    if not batch_id:
        return _error("Origami did not return a batchId on submit.")

    return {"batch_id": batch_id, "status": data.get("status", "queued")}


async def _poll_batch(client: httpx.AsyncClient, batch_id: str) -> dict:
    """
    Guardrail: MAX_POLL_ATTEMPTS caps the polling loop.
    If Origami hasn't completed the batch by then, we abort rather than hang.
    """
    endpoint = BATCH_ENDPOINT.format(batch_id=batch_id)

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

        try:
            response = await client.get(endpoint)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return _error(f"Origami poll error {e.response.status_code} on attempt {attempt}: {e.response.text}")
        except httpx.RequestError as e:
            return _error(f"Origami network error on poll attempt {attempt}: {str(e)}")

        data = response.json()
        status = data.get("status", "unknown")

        if status == "complete":
            result = data.get("result")
            if not result:
                return _error("Origami returned status=complete but result payload is empty.")
            return _build_response(batch_id=batch_id, source="origami_live", result=result)

        if status == "failed":
            reason = data.get("error", "no reason provided")
            return _error(f"Origami batch failed: {reason}")

        # status is "queued" or "processing" — keep polling

    # Guardrail: exceeded max poll attempts
    return _error(
        f"Origami enrichment timed out after {MAX_POLL_ATTEMPTS} poll attempts "
        f"({MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS}s). batchId={batch_id}"
    )


# ---------------------------------------------------------------------------
# Confidence guardrail
# ---------------------------------------------------------------------------

def _evaluate_confidence(result: dict) -> tuple[float, bool]:
    """
    Reads the top-level 'confidence' field Origami includes in the result payload.
    Returns (score, is_low_confidence).
    If the field is absent, defaults to 1.0 — assume confident rather than block on missing data.
    """
    score = float(result.get("confidence", 1.0))
    return score, score < CONFIDENCE_THRESHOLD


def _build_response(batch_id: str, source: str, result: dict) -> dict:
    confidence_score, low_confidence = _evaluate_confidence(result)

    response = {
        "enriched": True,
        "batch_id": batch_id,
        "source": source,
        "confidence_score": confidence_score,
        "low_confidence": low_confidence,
        "result": result,
    }

    if low_confidence:
        response["confidence_warning"] = (
            f"Enrichment confidence {confidence_score:.2f} is below threshold {CONFIDENCE_THRESHOLD}. "
            "Do not build or post a battle card — warn the AE that data quality is insufficient "
            "and suggest re-submitting with the company domain for a better match."
        )

    return response


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------

async def _load_mock() -> dict:
    if not MOCK_PATH.exists():
        return _error(f"Mock file not found at {MOCK_PATH}. Cannot run in shadow mode.")

    with open(MOCK_PATH) as f:
        mock = json.load(f)

    return _build_response(batch_id="mock_batch_001", source="mock", result=mock)


def _error(message: str) -> dict:
    return {"enriched": False, "error": message, "source": "origami"}

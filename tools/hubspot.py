import json
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# HubSpot CRM API
# Base URL    : https://api.hubapi.com
# Auth        : Authorization: Bearer <HUBSPOT_API_KEY>
# Docs        : https://developers.hubspot.com/docs/api/crm/search
#
# Endpoints used:
#   POST /crm/objects/2026-03/companies/search    — search company by name
#   GET  /crm/v4/objects/companies/{id}/associations/notes     — get associated notes
#   GET  /crm/v4/objects/companies/{id}/associations/contacts  — get associated contacts
#   POST /crm/v3/objects/notes/batch/read         — batch-read note properties
#   POST /crm/v3/objects/notes                    — create note
#   POST /crm/v4/associations/notes/companies/batch/create     — associate note → company
#   POST /crm/v3/objects/contacts                 — create contact
#   POST /crm/v4/associations/contacts/companies/batch/create  — associate contact → company
#   PATCH /crm/v3/objects/companies/{id}          — update company firmographics
# ---------------------------------------------------------------------------

BASE_URL = "https://api.hubapi.com"

COMPANIES_SEARCH        = "/crm/objects/2026-03/companies/search"
NOTES_BATCH_READ        = "/crm/v3/objects/notes/batch/read"
NOTES_CREATE            = "/crm/v3/objects/notes"
CONTACTS_CREATE         = "/crm/v3/objects/contacts"
COMPANIES_UPDATE        = "/crm/v3/objects/companies/{company_id}"
ASSOC_NOTES_COMPANIES   = "/crm/v4/associations/notes/companies/batch/create"
ASSOC_CONTACTS_COMPANIES = "/crm/v4/associations/contacts/companies/batch/create"
ASSOC_GET               = "/crm/v4/objects/companies/{company_id}/associations/{to_type}"

MOCK_PATH = Path(__file__).parent.parent / "mocks" / "hubspot_company.json"

RESEARCH_NOTE_TAG    = "account-research"
LOOKBACK_DAYS        = 30

# Association type IDs (HubSpot-defined)
ASSOC_NOTE_TO_COMPANY    = 190  # Note → Company
ASSOC_CONTACT_TO_COMPANY = 1    # Contact → Company

COMPANY_PROPERTIES = [
    "name", "domain", "industry", "phone", "address", "city", "state", "zip",
    "country", "numberofemployees", "annualrevenue", "submitted_by_ae", "ae_notes",
    "hubspot_owner_id", "createdate", "hs_lead_status", "lifecyclestage",
    "restoration_segment", "primary_work_type", "tpa_relationships",
    "carrier_relationships", "xactimate_user", "truerestore_enrichment_date",
]


# ---------------------------------------------------------------------------
# Tool 1 — get_hubspot_company
# ---------------------------------------------------------------------------

_GET_COMPANY_SCHEMA = {
    "name": "get_hubspot_company",
    "description": (
        "Fetch a company record from HubSpot CRM by name. Returns the company profile, "
        "AE submission notes, restoration segment, TPA/carrier relationships, lead status, "
        "and the hs_object_id needed for all subsequent HubSpot calls. Call this first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "The exact or partial company name the AE submitted in HubSpot.",
            }
        },
        "required": ["company_name"],
    },
}


def run_get_company(company_name: str) -> dict:
    company_name = company_name.strip()
    if not company_name:
        return _error("company_name is required and cannot be blank.", tool="get_hubspot_company")

    if config.SHADOW_MODE:
        return _mock_get_company()

    headers = _headers()
    body = {
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": company_name}]}],
        "properties": COMPANY_PROPERTIES,
        "limit": 5,
    }

    try:
        r = httpx.post(f"{BASE_URL}{COMPANIES_SEARCH}", headers=headers, json=body, timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _error(f"HubSpot API error {e.response.status_code}: {e.response.text}", tool="get_hubspot_company")
    except httpx.RequestError as e:
        return _error(f"HubSpot network error: {e}", tool="get_hubspot_company")

    results = r.json().get("results", [])
    if not results:
        return _error(f"No company found in HubSpot matching '{company_name}'.", tool="get_hubspot_company")

    company = results[0]
    return {
        "found": True,
        "hs_object_id": company.get("id"),
        "multiple_matches": len(results) > 1,
        "match_count": len(results),
        "properties": company.get("properties", {}),
        "source": "hubspot_live",
    }


def _mock_get_company() -> dict:
    if not MOCK_PATH.exists():
        return _error(f"Mock file not found at {MOCK_PATH}.", tool="get_hubspot_company")
    with open(MOCK_PATH) as f:
        mock = json.load(f)
    props = mock.get("properties", {})
    return {
        "found": True,
        "hs_object_id": props.get("hs_object_id", "mock_company_001"),
        "multiple_matches": False,
        "match_count": 1,
        "properties": props,
        "source": "mock",
    }


# ---------------------------------------------------------------------------
# Tool 2 — check_research_notes
# Checks if this company has a note tagged "account-research" in the last 30 days.
# ---------------------------------------------------------------------------

_CHECK_NOTES_SCHEMA = {
    "name": "check_research_notes",
    "description": (
        "Check if this company has already been researched in the last 30 days "
        "by looking for notes tagged 'account-research' in HubSpot. "
        "Returns already_researched as a boolean. "
        "If true, skip enrichment and go straight to building the battle card from cached data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hs_object_id": {
                "type": "string",
                "description": "HubSpot company object ID returned by get_hubspot_company.",
            }
        },
        "required": ["hs_object_id"],
    },
}


def run_check_research_notes(hs_object_id: str) -> dict:
    if not hs_object_id.strip():
        return _error("hs_object_id is required.", tool="check_research_notes")

    if config.SHADOW_MODE:
        # Shadow mode: pretend not yet researched so the full flow runs
        return {"already_researched": False, "last_researched_at": None, "note_count": 0, "source": "mock"}

    headers = _headers()

    # Step 1 — get note IDs associated with this company
    note_ids = _get_association_ids(hs_object_id, "notes", headers)
    if not note_ids:
        return {"already_researched": False, "last_researched_at": None, "note_count": 0, "source": "hubspot_live"}

    # Step 2 — batch-read note properties
    try:
        r = httpx.post(
            f"{BASE_URL}{NOTES_BATCH_READ}",
            headers=headers,
            json={"inputs": [{"id": nid} for nid in note_ids], "properties": ["hs_note_body", "hs_timestamp"]},
            timeout=10.0,
        )
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        return _error(f"Failed to batch-read notes: {e}", tool="check_research_notes")

    # Step 3 — filter for account-research tag within lookback window
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    matching_notes = []

    for note in r.json().get("results", []):
        props = note.get("properties", {})
        body = (props.get("hs_note_body") or "").lower()
        ts_raw = props.get("hs_timestamp") or "0"
        # hs_timestamp is returned as milliseconds string
        try:
            ts_ms = int(float(ts_raw))
        except ValueError:
            continue
        if RESEARCH_NOTE_TAG in body and ts_ms >= cutoff_ms:
            matching_notes.append({
                "note_id": note.get("id"),
                "timestamp_ms": ts_ms,
                "timestamp_iso": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
            })

    if matching_notes:
        latest = max(matching_notes, key=lambda n: n["timestamp_ms"])
        return {
            "already_researched": True,
            "last_researched_at": latest["timestamp_iso"],
            "note_count": len(matching_notes),
            "source": "hubspot_live",
        }

    return {"already_researched": False, "last_researched_at": None, "note_count": 0, "source": "hubspot_live"}


# ---------------------------------------------------------------------------
# Tool 3 — check_existing_enrichment
# Checks if contacts already exist for this company and whether firmographic
# enrichment has already been written back by a previous agent run.
# ---------------------------------------------------------------------------

_CHECK_ENRICHMENT_SCHEMA = {
    "name": "check_existing_enrichment",
    "description": (
        "Check if HubSpot already has contacts associated with this company "
        "and whether a previous agent run has written firmographic enrichment data back. "
        "Returns contacts_exist and firmographics_enriched as booleans. "
        "If both are true, skip Origami enrichment entirely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hs_object_id": {
                "type": "string",
                "description": "HubSpot company object ID returned by get_hubspot_company.",
            },
            "company_properties": {
                "type": "object",
                "description": "The properties dict already returned by get_hubspot_company — used to check truerestore_enrichment_date without an extra API call.",
            },
        },
        "required": ["hs_object_id", "company_properties"],
    },
}


def run_check_existing_enrichment(hs_object_id: str, company_properties: dict) -> dict:
    if not hs_object_id.strip():
        return _error("hs_object_id is required.", tool="check_existing_enrichment")

    if config.SHADOW_MODE:
        return {
            "contacts_exist": False,
            "contact_count": 0,
            "firmographics_enriched": False,
            "enrichment_date": None,
            "source": "mock",
        }

    headers = _headers()

    # Check contacts via associations
    contact_ids = _get_association_ids(hs_object_id, "contacts", headers)

    # Check firmographic enrichment via custom property already on the company
    enrichment_date = company_properties.get("truerestore_enrichment_date")
    firmographics_enriched = bool(enrichment_date)

    return {
        "contacts_exist": len(contact_ids) > 0,
        "contact_count": len(contact_ids),
        "firmographics_enriched": firmographics_enriched,
        "enrichment_date": enrichment_date,
        "source": "hubspot_live",
    }


# ---------------------------------------------------------------------------
# Tool 4 — write_enrichment_to_hubspot
# After a full Origami enrichment, writes decision maker as a contact,
# updates company firmographics, and creates a tagged note.
# Next run can then skip Origami entirely.
# WRITE operation — blocked in shadow mode.
# ---------------------------------------------------------------------------

_WRITE_ENRICHMENT_SCHEMA = {
    "name": "write_enrichment_to_hubspot",
    "description": (
        "After successful Origami enrichment, write results back to HubSpot so future runs "
        "skip re-enrichment. Creates the decision maker as a contact, updates the company "
        "with firmographic data, and creates a note tagged 'account-research'. "
        "Only call this after enrichment succeeds and confidence is above threshold."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hs_object_id": {
                "type": "string",
                "description": "HubSpot company object ID.",
            },
            "decision_maker": {
                "type": "object",
                "description": "The primary economic buyer to create as a contact.",
                "properties": {
                    "firstname": {"type": "string"},
                    "lastname":  {"type": "string"},
                    "email":     {"type": "string"},
                    "jobtitle":  {"type": "string"},
                    "phone":     {"type": "string"},
                    "role":      {"type": "string", "description": "economic_buyer | champion | end_user"},
                },
                "required": ["firstname", "lastname", "jobtitle"],
            },
            "firmographics": {
                "type": "object",
                "description": "Company firmographic fields to write back.",
                "properties": {
                    "numberofemployees":         {"type": "string"},
                    "annualrevenue":             {"type": "string"},
                    "industry":                  {"type": "string"},
                    "icp_fit":                   {"type": "string"},
                    "primary_service_lines":     {"type": "string"},
                    "certifications":            {"type": "string"},
                    "current_estimation_tool":   {"type": "string"},
                    "tpa_relationships":         {"type": "string"},
                },
            },
            "enrichment_summary": {
                "type": "string",
                "description": "1–3 sentence summary of enrichment findings for the note body.",
            },
        },
        "required": ["hs_object_id", "decision_maker", "firmographics", "enrichment_summary"],
    },
}


def run_write_enrichment(
    hs_object_id: str,
    decision_maker: dict,
    firmographics: dict,
    enrichment_summary: str,
) -> dict:
    # Guardrail: shadow mode blocks all writes
    if config.SHADOW_MODE:
        return _shadow_write_enrichment(hs_object_id, decision_maker, firmographics, enrichment_summary)

    # Guardrail: explicit allowlist check
    if not config.is_write_allowed("write_enrichment_to_hubspot"):
        return _error("Write to HubSpot is not permitted — write_enrichment_to_hubspot is not on the allowlist.", tool="write_enrichment_to_hubspot")

    headers = _headers()
    results = {}

    # Step 1 — create contact for decision maker
    contact_id = _create_contact(decision_maker, headers)
    results["contact_id"] = contact_id

    # Step 2 — associate contact → company
    if contact_id:
        _associate(
            endpoint=ASSOC_CONTACTS_COMPANIES,
            from_id=contact_id,
            to_id=hs_object_id,
            assoc_type_id=ASSOC_CONTACT_TO_COMPANY,
            headers=headers,
        )

    # Step 3 — update company with firmographics + enrichment date stamp
    company_props = {k: v for k, v in firmographics.items() if v}
    company_props["truerestore_enrichment_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        r = httpx.patch(
            f"{BASE_URL}{COMPANIES_UPDATE.format(company_id=hs_object_id)}",
            headers=headers,
            json={"properties": company_props},
            timeout=10.0,
        )
        r.raise_for_status()
        results["company_updated"] = True
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        results["company_update_error"] = str(e)

    # Step 4 — create note tagged account-research (two-step: create then associate)
    note_body = f"{RESEARCH_NOTE_TAG}\n\n{enrichment_summary}\n\nEnriched by TrueRestore agent on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    note_id = _create_note(note_body, headers)
    results["note_id"] = note_id

    # Step 5 — associate note → company
    if note_id:
        _associate(
            endpoint=ASSOC_NOTES_COMPANIES,
            from_id=note_id,
            to_id=hs_object_id,
            assoc_type_id=ASSOC_NOTE_TO_COMPANY,
            headers=headers,
        )

    return {"written": True, "source": "hubspot_live", **results}


def _shadow_write_enrichment(
    hs_object_id: str,
    decision_maker: dict,
    firmographics: dict,
    enrichment_summary: str,
) -> dict:
    print("\n" + "=" * 60)
    print("[SHADOW] HubSpot write — would perform:")
    print(f"  Company ID  : {hs_object_id}")
    print(f"  Contact     : {decision_maker.get('firstname')} {decision_maker.get('lastname')} ({decision_maker.get('jobtitle')})")
    print(f"  Firmographics written: {list(firmographics.keys())}")
    print(f"  Note body preview: {RESEARCH_NOTE_TAG} | {enrichment_summary[:80]}...")
    print("=" * 60 + "\n")
    return {
        "written": True,
        "contact_id": "shadow_contact_001",
        "note_id": "shadow_note_001",
        "company_updated": True,
        "source": "shadow",
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def _get_association_ids(company_id: str, to_type: str, headers: dict) -> list[str]:
    url = f"{BASE_URL}{ASSOC_GET.format(company_id=company_id, to_type=to_type)}"
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        r.raise_for_status()
        return [str(item["toObjectId"]) for item in r.json().get("results", [])]
    except Exception:
        return []


def _create_contact(decision_maker: dict, headers: dict) -> str | None:
    props = {k: v for k, v in decision_maker.items() if k != "role" and v}
    try:
        r = httpx.post(f"{BASE_URL}{CONTACTS_CREATE}", headers=headers, json={"properties": props}, timeout=10.0)
        r.raise_for_status()
        return r.json().get("id")
    except Exception:
        return None


def _create_note(body: str, headers: dict) -> str | None:
    ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    props = {"hs_note_body": body, "hs_timestamp": ts_ms}
    try:
        r = httpx.post(f"{BASE_URL}{NOTES_CREATE}", headers=headers, json={"properties": props}, timeout=10.0)
        r.raise_for_status()
        return r.json().get("id")
    except Exception:
        return None


def _associate(endpoint: str, from_id: str, to_id: str, assoc_type_id: int, headers: dict) -> None:
    payload = {
        "inputs": [{
            "from": {"id": from_id},
            "to":   {"id": to_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": assoc_type_id}],
        }]
    }
    try:
        r = httpx.post(f"{BASE_URL}{endpoint}", headers=headers, json=payload, timeout=10.0)
        r.raise_for_status()
    except Exception:
        pass  # Association failure is non-fatal — the object was still created


def _error(message: str, tool: str = "hubspot") -> dict:
    return {"found": False, "error": message, "source": tool}


# ---------------------------------------------------------------------------
# Tool registry — imported by agent.py
# ---------------------------------------------------------------------------

TOOLS = [
    {"schema": _GET_COMPANY_SCHEMA,       "fn": run_get_company},
    {"schema": _CHECK_NOTES_SCHEMA,       "fn": run_check_research_notes},
    {"schema": _CHECK_ENRICHMENT_SCHEMA,  "fn": run_check_existing_enrichment},
    {"schema": _WRITE_ENRICHMENT_SCHEMA,  "fn": run_write_enrichment},
]

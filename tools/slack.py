import httpx

import config

# ---------------------------------------------------------------------------
# Slack Web API — chat.postMessage
#
# Endpoint   : POST https://slack.com/api/chat.postMessage
# Permalink  : GET  https://slack.com/api/chat.getPermalink
# Auth       : Authorization: Bearer xoxb-...  (bot token, chat:write scope)
# Content-Type: application/json
# Docs       : https://docs.slack.dev/reference/methods/chat.postMessage
# ---------------------------------------------------------------------------

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
GET_PERMALINK_URL = "https://slack.com/api/chat.getPermalink"

# ---------------------------------------------------------------------------
# Tool schema — what Claude sees
# ---------------------------------------------------------------------------

SCHEMA = {
    "name": "post_battle_card",
    "description": (
        "Post the finished battle card to the #account-research Slack channel. "
        "Only call this when low_confidence is false and enrichment succeeded. "
        "Accepts structured battle card fields — do NOT call this with raw text. "
        "Returns the Slack message permalink so it can be logged to Linear."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Full company name.",
            },
            "ae_name": {
                "type": "string",
                "description": "AE who submitted this account.",
            },
            "icp_fit": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "ICP fit rating from Origami firmographic score.",
            },
            "icp_rationale": {
                "type": "string",
                "description": "One sentence explaining the ICP fit rating.",
            },
            "top_pain_points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2–4 specific pain points to lead the conversation with.",
            },
            "key_contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":  {"type": "string"},
                        "title": {"type": "string"},
                        "role":  {"type": "string", "description": "economic_buyer | champion | end_user | influencer"},
                        "note":  {"type": "string"},
                    },
                    "required": ["name", "title", "role"],
                },
                "description": "Contacts in order of who to call first.",
            },
            "buying_signals": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recent signals — intent, hiring, events, pain indicators.",
            },
            "likely_objections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "objection": {"type": "string"},
                        "counter":   {"type": "string"},
                    },
                    "required": ["objection", "counter"],
                },
                "description": "Top objections with pre-built counters.",
            },
            "recommended_opener": {
                "type": "string",
                "description": "One suggested opening line for the AE's first call or email.",
            },
            "confidence_score": {
                "type": "number",
                "description": "Origami enrichment confidence score (0.0–1.0).",
            },
        },
        "required": [
            "company_name", "ae_name", "icp_fit", "icp_rationale",
            "top_pain_points", "key_contacts", "buying_signals",
            "likely_objections", "recommended_opener",
        ],
    },
}


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def run(
    company_name: str,
    ae_name: str,
    icp_fit: str,
    icp_rationale: str,
    top_pain_points: list[str],
    key_contacts: list[dict],
    buying_signals: list[str],
    likely_objections: list[dict],
    recommended_opener: str,
    confidence_score: float | None = None,
) -> dict:
    """
    Guardrails applied:
    - Shadow mode  → prints formatted battle card to stdout, no Slack write.
    - Write check  → blocked unless post_to_slack is on the allowlist.
    - Empty inputs → rejected before any network call.
    """
    if not company_name.strip() or not ae_name.strip():
        return _error("company_name and ae_name are required.")

    blocks = _build_blocks(
        company_name=company_name,
        ae_name=ae_name,
        icp_fit=icp_fit,
        icp_rationale=icp_rationale,
        top_pain_points=top_pain_points,
        key_contacts=key_contacts,
        buying_signals=buying_signals,
        likely_objections=likely_objections,
        recommended_opener=recommended_opener,
        confidence_score=confidence_score,
    )

    # Fallback text for notifications / clients that don't render blocks
    fallback_text = f"Battle card ready: {company_name} — submitted by {ae_name}"

    if config.SHADOW_MODE:
        return _shadow_print(company_name, blocks, fallback_text)

    if not config.is_write_allowed("post_to_slack"):
        return _error("Write to Slack is not permitted — post_to_slack is not on the allowlist.")

    return _post_to_slack(blocks, fallback_text)


def _post_to_slack(blocks: list[dict], fallback_text: str) -> dict:
    channel = config.SLACK_CHANNEL_ID
    if not channel:
        return _error("SLACK_CHANNEL_ID is not set.")

    headers = {
        "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": channel,
        "text": fallback_text,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }

    try:
        response = httpx.post(POST_MESSAGE_URL, json=payload, headers=headers, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _error(f"Slack API error {e.response.status_code}: {e.response.text}")
    except httpx.RequestError as e:
        return _error(f"Slack network error: {str(e)}")

    body = response.json()
    if not body.get("ok"):
        return _error(f"Slack rejected the message: {body.get('error', 'unknown error')}")

    ts = body.get("ts")
    permalink = _get_permalink(channel, ts, headers)

    return {
        "posted": True,
        "channel": body.get("channel"),
        "ts": ts,
        "permalink": permalink,
        "source": "slack_live",
    }


def _get_permalink(channel: str, ts: str, headers: dict) -> str | None:
    try:
        response = httpx.get(
            GET_PERMALINK_URL,
            params={"channel": channel, "message_ts": ts},
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("ok"):
            return body.get("permalink")
    except Exception:
        pass
    return None


def _shadow_print(company_name: str, blocks: list[dict], fallback_text: str) -> dict:
    print("\n" + "=" * 60)
    print(f"[SHADOW] Slack → #{config.SLACK_CHANNEL_ID or 'account-research'}")
    print(f"Fallback : {fallback_text}")
    print("-" * 60)
    for block in blocks:
        btype = block.get("type")
        if btype == "header":
            print(f"\n### {block['text']['text']}")
        elif btype == "section":
            print(block["text"]["text"])
        elif btype == "divider":
            print("-" * 40)
        elif btype == "context":
            for el in block.get("elements", []):
                print(f"  [{el.get('text', '')}]")
    print("=" * 60 + "\n")

    return {
        "posted": True,
        "channel": config.SLACK_CHANNEL_ID or "shadow-channel",
        "ts": "0000000000.000000",
        "permalink": None,
        "source": "shadow",
    }


# ---------------------------------------------------------------------------
# Block Kit builder
# ---------------------------------------------------------------------------

_FIT_EMOJI = {"high": ":large_green_circle:", "medium": ":large_yellow_circle:", "low": ":red_circle:"}

def _build_blocks(
    company_name: str,
    ae_name: str,
    icp_fit: str,
    icp_rationale: str,
    top_pain_points: list[str],
    key_contacts: list[dict],
    buying_signals: list[str],
    likely_objections: list[dict],
    recommended_opener: str,
    confidence_score: float | None,
) -> list[dict]:
    fit_emoji = _FIT_EMOJI.get(icp_fit, ":white_circle:")
    blocks = []

    # Header
    blocks.append(_header(f"Battle Card — {company_name}"))
    blocks.append(_section(
        f"{fit_emoji} *ICP Fit: {icp_fit.upper()}* — {icp_rationale}"
    ))
    blocks.append(_divider())

    # Pain points
    blocks.append(_section("*:fire: Top Pain Points*"))
    pain_text = "\n".join(f"• {p}" for p in top_pain_points)
    blocks.append(_section(pain_text))
    blocks.append(_divider())

    # Key contacts
    blocks.append(_section("*:telephone_receiver: Who to Call (in order)*"))
    for c in key_contacts:
        role_label = c.get("role", "").replace("_", " ").title()
        note = f" — _{c['note']}_" if c.get("note") else ""
        blocks.append(_section(f"*{c['name']}* · {c['title']} · _{role_label}_{note}"))
    blocks.append(_divider())

    # Buying signals
    blocks.append(_section("*:signal_strength: Buying Signals*"))
    signals_text = "\n".join(f"• {s}" for s in buying_signals)
    blocks.append(_section(signals_text))
    blocks.append(_divider())

    # Objections
    blocks.append(_section("*:shield: Likely Objections + Counters*"))
    for obj in likely_objections:
        blocks.append(_section(
            f"*Objection:* {obj['objection']}\n*Counter:* {obj['counter']}"
        ))
    blocks.append(_divider())

    # Recommended opener
    blocks.append(_section("*:speech_balloon: Recommended Opener*"))
    blocks.append(_section(f'"{recommended_opener}"'))

    # Footer context
    confidence_text = f" · Confidence: {confidence_score:.0%}" if confidence_score is not None else ""
    blocks.append(_context(f"Submitted by {ae_name}{confidence_text} · Powered by TrueRestore Account Research"))

    return blocks


# ---------------------------------------------------------------------------
# Block Kit helpers
# ---------------------------------------------------------------------------

def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}

def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

def _divider() -> dict:
    return {"type": "divider"}

def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _error(message: str) -> dict:
    return {"posted": False, "error": message, "source": "slack"}

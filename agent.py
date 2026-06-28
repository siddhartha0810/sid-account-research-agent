import json
from dataclasses import dataclass, field

import anthropic

import config
from tools import cache, hubspot, origami, slack, linear


# ---------------------------------------------------------------------------
# Tool registry
# Unify the two export shapes:
#   - TOOLS list (cache.py, hubspot.py): [{"schema": ..., "fn": ...}, ...]
#   - Single export (origami/slack/linear): SCHEMA + run()
# ---------------------------------------------------------------------------

def _wrap(schema: dict, fn) -> dict:
    return {"schema": schema, "fn": fn}

_ALL_TOOLS: list[dict] = (
    cache.TOOLS
    + hubspot.TOOLS
    + [
        _wrap(origami.SCHEMA, origami.run),
        _wrap(slack.SCHEMA,   slack.run),
        _wrap(linear.SCHEMA,  linear.run),
    ]
)

# Flat name → fn dispatch table
_DISPATCH: dict[str, callable] = {t["schema"]["name"]: t["fn"] for t in _ALL_TOOLS}

# Schemas list passed to Claude
_SCHEMAS: list[dict] = [t["schema"] for t in _ALL_TOOLS]


# ---------------------------------------------------------------------------
# Agent state — tracks ordering invariants across tool calls
# ---------------------------------------------------------------------------

@dataclass
class ToolLogEntry:
    step: int
    tool_name: str
    status: str          # "ok" | "blocked" | "error"
    summary: str
    result: dict
    tool_input: dict = field(default_factory=dict)


@dataclass
class AgentState:
    company_name: str
    ae_name: str = ""
    hs_object_id: str = ""
    enrichment_done: bool = False
    low_confidence: bool = False
    confidence_score: float | None = None
    hubspot_write_done: bool = False
    slack_permalink: str | None = None
    mark_done: bool = False
    linear_done: bool = False
    run_status: str = "error"
    tool_log: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompt — encodes the full workflow and all guardrails
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the TrueRestore Account Research Agent — an expert B2B sales intelligence assistant
for a restoration estimation SaaS company. Your job is to research a restoration company submitted by an
Account Executive (AE) and produce a complete battle card so the AE can walk into the call prepared.

## Workflow — follow these steps in order

1. **check_cache** — Check local cache first. If the result shows cached=true and is_stale=false,
   skip all enrichment steps and go directly to posting the battle card using the cached data.
   If cached=true and is_stale=true, proceed with fresh enrichment.

2. **get_hubspot_company** — Look up the company in HubSpot CRM to get the hs_object_id and AE name.
   If the company is not found in HubSpot, log a Linear issue with run_status="error" and stop.

3. **check_research_notes** — Check if this company was researched via account-research notes in the
   last 30 days. If notes confirm recent research, skip enrichment and go to step 7.

4. **check_existing_enrichment** — Check if contacts and firmographic data already exist in HubSpot.
   If both exist and are recent, skip enrichment and go to step 7.

5. **enrich_company** — Call Origami to enrich the company. This is async and may take a few seconds.
   CHECK THE RESPONSE carefully:
   - If low_confidence=true: do NOT post a battle card. Warn the AE with a Linear issue instead.
     Set run_status="low_confidence_warning". Skip steps 6 and 7. Go directly to step 8.
   - If low_confidence=false: proceed normally.

6. **write_enrichment_to_hubspot** — After successful enrichment (low_confidence=false), write the
   decision maker, firmographics, and enrichment summary back to HubSpot. This ensures future runs
   skip Origami entirely. Only call this if enrichment succeeded and confidence was high enough.

7. **post_battle_card** — Build and post the battle card to Slack. Use ALL enrichment data to write
   compelling, specific pain points, objections, buying signals, and a recommended opener tailored
   to the restoration industry. ICP fit and confidence come from the Origami response.
   NEVER call this if low_confidence=true.

8. **mark_researched** — Record this run in the local cache. Always call this regardless of outcome
   (success, skip, low_confidence, error). This MUST be called before log_to_linear.

9. **log_to_linear** — Log the completed run as a Linear issue for audit. Always call this as the
   ABSOLUTE FINAL step. Never skip it. Never call it before mark_researched.

## Guardrails — enforce strictly

- **Low confidence block**: If enrich_company returns low_confidence=true, you MUST NOT call
  post_battle_card. Instead call mark_researched with run_status="low_confidence_warning", then
  log_to_linear with run_status="low_confidence_warning" and a summary explaining the low confidence.

- **Write-back after enrichment**: Always call write_enrichment_to_hubspot after a successful
  enrichment with high confidence. Do not skip this step.

- **Linear always fires last**: log_to_linear is the last tool call in every execution path.
  If you are about to call log_to_linear and mark_researched has not been called yet,
  call mark_researched first.

- **Ordering**: The battle card (post_battle_card) must come after write_enrichment_to_hubspot.
  mark_researched must come after post_battle_card (or after any terminal outcome).
  log_to_linear must come after mark_researched.

- **Error handling**: If any step fails unexpectedly, continue to mark_researched with
  run_status="error" and then log_to_linear with a clear summary of what failed and why.

- **Company name accuracy**: Use the exact company name returned by HubSpot in all subsequent calls,
  not the user-submitted version (in case of minor variations).

## Battle card quality standards

When building the battle card, use ALL available enrichment data:
- Pain points must be specific to restoration (Xactimate usage, supplement denial rates, carrier
  relationships, TPA headaches, seasonal cash flow, adjuster turnaround times)
- Buying signals must cite specific data points (renewal dates, hiring signals, tech stack gaps)
- Objections and counters must reflect restoration industry dynamics
- The recommended opener must be personalized to the prospect's specific situation

When in doubt, be specific over generic. A vague pain point ("efficiency issues") is worse than
no pain point at all.
"""


# ---------------------------------------------------------------------------
# Dispatch — executes a tool with guardrail checks and state updates
# ---------------------------------------------------------------------------

def _dispatch(tool_name: str, tool_input: dict, state: AgentState) -> dict:

    # --- Ordering guardrails ---

    if tool_name == "post_battle_card" and state.low_confidence:
        return {
            "blocked": True,
            "reason": (
                "post_battle_card is blocked because low_confidence=true. "
                "Enrichment confidence was too low to post a battle card. "
                "Call mark_researched with run_status='low_confidence_warning', "
                "then log_to_linear with the same status."
            ),
        }

    if tool_name == "log_to_linear" and not state.mark_done:
        return {
            "blocked": True,
            "reason": (
                "log_to_linear cannot be called before mark_researched. "
                "Call mark_researched first, then log_to_linear."
            ),
        }

    if tool_name == "log_to_linear" and state.linear_done:
        return {
            "blocked": True,
            "reason": "log_to_linear has already been called for this run. Do not call it again.",
        }

    # --- Resolve function ---

    fn = _DISPATCH.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool: {tool_name}"}

    # --- Execute ---

    try:
        result = fn(**tool_input)
    except TypeError as e:
        return {"error": f"Tool call failed — bad arguments: {e}"}
    except Exception as e:
        return {"error": f"Tool execution error: {e}"}

    # --- State updates ---

    if tool_name == "get_hubspot_company":
        if not result.get("error"):
            state.hs_object_id = str(result.get("hs_object_id", "") or "")
            props = result.get("properties", {})
            ae_raw = props.get("submitted_by_ae") or result.get("ae_name") or ""
            if ae_raw:
                state.ae_name = ae_raw

    elif tool_name == "enrich_company":
        if not result.get("error"):
            state.enrichment_done = True
            state.low_confidence = bool(result.get("low_confidence", False))
            conf = result.get("confidence")
            if conf is not None:
                state.confidence_score = float(conf)

    elif tool_name == "post_battle_card":
        if not result.get("error"):
            perm = result.get("permalink")
            if perm:
                state.slack_permalink = perm
            state.run_status = "battle_card_posted"

    elif tool_name == "mark_researched":
        if not result.get("error"):
            state.mark_done = True
            # Capture run_status from what Claude passed in
            if "run_status" in tool_input:
                state.run_status = tool_input["run_status"]

    elif tool_name == "log_to_linear":
        if not result.get("error"):
            state.linear_done = True

    # --- Log entry ---

    entry_status = "error" if result.get("error") else ("blocked" if result.get("blocked") else "ok")
    entry_summary = _summarize_result(tool_name, result, state)
    state.tool_log.append(ToolLogEntry(
        step       = len(state.tool_log) + 1,
        tool_name  = tool_name,
        status     = entry_status,
        summary    = entry_summary,
        result     = result,
        tool_input = tool_input,
    ))

    return result


def _summarize_result(tool_name: str, result: dict, state: AgentState) -> str:
    if result.get("blocked"):
        return f"Blocked — {result.get('reason', '')[:80]}"
    if result.get("error"):
        return f"Error — {result.get('error', '')[:80]}"

    if tool_name == "check_cache":
        if result.get("cached") and not result.get("is_stale"):
            return f"Cache hit — last run {result.get('days_since', '?')} days ago (fresh)"
        if result.get("cached") and result.get("is_stale"):
            return f"Cache hit — last run {result.get('days_since', '?')} days ago (stale, re-enriching)"
        return "No cache entry — fresh run"

    elif tool_name == "get_hubspot_company":
        if result.get("found"):
            props  = result.get("properties", {})
            ae     = props.get("submitted_by_ae") or "unknown AE"
            obj_id = result.get("hs_object_id", "?")
            return f"Found: ID {obj_id}, AE: {ae}"
        return "Company not found in HubSpot"

    elif tool_name == "check_research_notes":
        count = result.get("note_count", 0)
        if result.get("recently_researched"):
            return f"{count} research note(s) found in last 30 days — skipping enrichment"
        return f"No recent research notes ({count} total)"

    elif tool_name == "check_existing_enrichment":
        contacts = result.get("contact_count", 0)
        enriched = result.get("firmographics_enriched", False)
        if contacts and enriched:
            return f"{contacts} contact(s) + firmographics already in HubSpot"
        if contacts:
            return f"{contacts} contact(s) found, no firmographic enrichment"
        return "No existing contacts or enrichment"

    elif tool_name == "enrich_company":
        conf = result.get("confidence")
        low  = result.get("low_confidence", False)
        pct  = f"{conf:.0%}" if conf is not None else "?"
        flag = "  LOW — battle card blocked" if low else ""
        return f"Confidence {pct}{flag}"

    elif tool_name == "write_enrichment_to_hubspot":
        parts = []
        if result.get("contact_created"):  parts.append("contact")
        if result.get("company_updated"):  parts.append("firmographics")
        if result.get("note_created"):     parts.append("note")
        return "Wrote " + " + ".join(parts) + " to HubSpot" if parts else "Write acknowledged"

    elif tool_name == "post_battle_card":
        channel = result.get("channel") or "account-research"
        source  = result.get("source", "")
        suffix  = " (shadow)" if source == "shadow" else ""
        return f"Posted to #{channel}{suffix}"

    elif tool_name == "mark_researched":
        status = state.run_status
        source = result.get("source", "")
        suffix = " (shadow)" if source == "shadow" else ""
        return f"Cached as {status}{suffix}"

    elif tool_name == "log_to_linear":
        ident  = result.get("identifier") or result.get("issue_id") or "?"
        source = result.get("source", "")
        suffix = " (shadow)" if source == "shadow" else ""
        return f"Issue {ident} created{suffix}"

    return "OK"


# ---------------------------------------------------------------------------
# Main agent entry point
# ---------------------------------------------------------------------------

def run_agent(company_name: str, ae_name: str = "") -> dict:
    """
    Run the full account research workflow for a single company.

    Args:
        company_name: Company name as submitted by the AE.
        ae_name:      AE name if known upfront (optional — discovered from HubSpot otherwise).

    Returns a summary dict with final run status and key identifiers.
    """

    if config.SHADOW_MODE:
        print("\n" + "=" * 60)
        print(f"[AGENT] Shadow mode ON — no real writes will occur.")
        print(f"[AGENT] Researching: {company_name}")
        if ae_name:
            print(f"[AGENT] AE: {ae_name}")
        settings = config.summarize()
        print(f"[AGENT] Model: {settings['claude_model']}")
        print(f"[AGENT] Max iterations: {settings['max_loop_iterations']}")
        print("=" * 60 + "\n")

    state = AgentState(company_name=company_name, ae_name=ae_name)

    initial_msg = f"Research the following company for the sales team: {company_name}"
    if ae_name:
        initial_msg += f"\n\nThe AE who submitted this account is: {ae_name}"

    messages: list[dict] = [{"role": "user", "content": initial_msg}]

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    iteration = 0
    abort_reason: str | None = None

    for iteration in range(config.MAX_LOOP_ITERATIONS):
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            tools=_SCHEMAS,
            messages=messages,
        )

        if config.SHADOW_MODE:
            text_blocks = [b for b in response.content if b.type == "text"]
            for b in text_blocks:
                if b.text.strip():
                    print(f"[AGENT] {b.text.strip()}\n")

        # End of run — Claude is done
        if response.stop_reason == "end_turn":
            break

        # No tool use despite not being end_turn — unexpected, treat as done
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            break

        # Append Claude's full response turn to messages
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool and collect results
        tool_results = []
        for block in tool_uses:
            if config.SHADOW_MODE:
                print(f"[TOOL] {block.name}({json.dumps(block.input, indent=2)})\n")

            result = _dispatch(block.name, block.input, state)

            if config.SHADOW_MODE:
                print(f"[RESULT] {json.dumps(result, indent=2)}\n")

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result),
            })

        messages.append({"role": "user", "content": tool_results})

    else:
        # Loop exhausted without break — max iterations hit
        abort_reason = f"Agent hit max iterations ({config.MAX_LOOP_ITERATIONS}) without completing."
        print(f"\n[AGENT] WARNING: {abort_reason}")

    # ---------------------------------------------------------------------------
    # Abort path: max iterations exceeded — fire cleanup tools manually
    # ---------------------------------------------------------------------------

    if abort_reason:
        _emergency_log(client, state, abort_reason)

    if config.SHADOW_MODE:
        print(f"\n[AGENT] Run complete. Status: {state.run_status}  "
              f"Iterations used: {iteration + 1}/{config.MAX_LOOP_ITERATIONS}")

    return {
        "status":           state.run_status,
        "company_name":     company_name,
        "hs_object_id":     state.hs_object_id,
        "ae_name":          state.ae_name,
        "confidence_score": state.confidence_score,
        "slack_permalink":  state.slack_permalink,
        "mark_done":        state.mark_done,
        "linear_done":      state.linear_done,
        "iterations_used":  iteration + 1,
        "tool_log":         state.tool_log,
    }


# ---------------------------------------------------------------------------
# Emergency log — called when the agent loop is aborted by the iteration cap
# ---------------------------------------------------------------------------

def _emergency_log(client: anthropic.Anthropic, state: AgentState, reason: str) -> None:
    """
    Fire mark_researched + log_to_linear even when the main loop aborts.
    Keeps the audit trail intact regardless of how the run ends.
    """
    hs_id = state.hs_object_id or "unknown"
    ae    = state.ae_name or "unknown"
    company = state.company_name

    if not state.mark_done:
        result = cache.run_mark_researched(
            company_name    = company,
            hs_object_id    = hs_id,
            run_status      = "error",
            ae_name         = ae,
            confidence_score= state.confidence_score,
        )
        if config.SHADOW_MODE:
            print(f"[AGENT] Emergency mark_researched: {result}")
        state.mark_done = True

    if not state.linear_done:
        result = linear.run(
            company_name    = company,
            run_status      = "error",
            ae_name         = ae,
            summary         = f"Agent aborted: {reason}",
            confidence_score= state.confidence_score,
        )
        if config.SHADOW_MODE:
            print(f"[AGENT] Emergency log_to_linear: {result}")
        state.linear_done = True

# TrueRestore Account Research Agent

An agentic account research tool built for TrueRestore — a restoration estimation SaaS company. When an Account Executive submits a company name in HubSpot, Claude runs a full research loop and drops a battle card into Slack so the AE walks into every call prepared.

---

## How It Works

Claude is the brain. It decides the tool sequence, checks guardrails, and builds the battle card. Python executes what Claude asks for and nothing more. The intelligence lives in the system prompt — when the ICP shifts or the battle card format changes, you update the prompt, not the code.

```
AE submits company in HubSpot
        ↓
  Check local cache
        ↓
  Get HubSpot company record
        ↓
  Check research notes (last 30 days)
        ↓
  Check existing enrichment
        ↓
  Enrich via Origami.chat  ←── skipped if already researched
        ↓
  Write decision maker + firmographics back to HubSpot
        ↓
  Post battle card to Slack #account-research
        ↓
  Mark researched in local cache
        ↓
  Log run to Linear  ←── always fires last
```

---

## File Structure

```
├── agent.py              # Claude tool loop — the brain
├── runner.py             # CLI entry point + summary + markdown output
├── config.py             # All settings and guardrails in one place
├── requirements.txt
├── .env.example
├── tools/
│   ├── cache.py          # Local JSON cache (check + mark)
│   ├── hubspot.py        # HubSpot CRM (search, notes, contacts, write-back)
│   ├── origami.py        # Origami.chat async enrichment
│   ├── slack.py          # Slack Block Kit battle card poster
│   └── linear.py         # Linear issue logger
├── mocks/
│   ├── hubspot_company.json
│   └── origami_enrichment.json
└── output/               # Generated battle cards (.md)
```

---

## Guardrails

- **Shadow mode** — `SHADOW_MODE=true` gates all writes. Slack and Linear print to stdout. HubSpot and Origami load from mocks. Nothing real is touched until you flip the flag.
- **Low confidence block** — if Origami returns confidence below 0.6, the battle card is blocked. Claude warns the AE instead of posting bad data.
- **Write-back after enrichment** — after a successful Origami run, decision maker and firmographics are written back to HubSpot so the next run skips Origami entirely.
- **Linear always fires last** — every run (success, skip, low confidence, error) gets logged. The agent is blocked from calling `log_to_linear` before `mark_researched`.
- **Max iterations cap** — the Claude loop is hard-capped at 10 iterations. If it hits the cap, an emergency log fires automatically.
- **Write allowlist** — even outside shadow mode, only tools on the `WRITE_PERMITTED_TOOLS` list can perform writes.

---

## Setup

```bash
git clone https://github.com/siddhartha0810/sid-account-research-agent.git
cd sid-account-research-agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in ANTHROPIC_API_KEY (required) and the rest when going live
```

---

## Running

```bash
# Shadow mode — safe, uses mocks, no real writes
python3 runner.py "Summit Restoration & Reconstruction"

# With AE name override
python3 runner.py "Summit Restoration" --ae "Jordan Malone"
```

The runner prints a formatted summary to the terminal and writes the battle card to `output/<company-slug>.md`.

---

## Example Output

```
╔══════════════════════════════════════════════════════════════╗
║  TRUERESTORE  ACCOUNT RESEARCH  —  RUN SUMMARY              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║    Company     Summit Restoration & Reconstruction          ║
║    AE          Jordan Malone                                 ║
║    Confidence  91%                                           ║
║    Status      ✓  Battle Card Posted                         ║
║    Markdown    output/summit-restoration-reconstruction.md  ║
║    Mode        SHADOW — no real writes made                  ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║    TOOL EXECUTION LOG                                        ║
║                                                              ║
║     1.  ✓  check_cache                 No cache entry        ║
║     2.  ✓  get_hubspot_company         Found: ID 12345       ║
║     3.  ✓  check_research_notes        No recent notes       ║
║     4.  ✓  check_existing_enrichment   No existing data      ║
║     5.  ✓  enrich_company              Confidence 91%        ║
║     6.  ✓  write_enrichment_to_hubspot Wrote contact + firm  ║
║     7.  ✓  post_battle_card            Posted to #account-r  ║
║     8.  ✓  mark_researched             Cached as posted      ║
║     9.  ✓  log_to_linear               Issue SHADOW-1        ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Always | Claude API key |
| `SHADOW_MODE` | — | `true` by default — set `false` to go live |
| `HUBSPOT_API_KEY` | Live only | Private app token |
| `ORIGAMI_API_KEY` | Live only | `og_live_` prefixed key |
| `ORIGAMI_TABLE_ID` | Live only | Enrichment table ID |
| `SLACK_BOT_TOKEN` | Live only | `xoxb-` bot token |
| `SLACK_CHANNEL_ID` | Live only | Channel ID for #account-research |
| `LINEAR_API_KEY` | Live only | `lin_api_` personal key |
| `LINEAR_TEAM_ID` | Live only | Linear workspace team ID |

---

## What's Next

- Dedup gate so previously researched companies actually skip Origami, not just log a warning
- Test the low confidence path with a mock returning confidence 0.4 and confirm the Slack post is blocked
- Add a second mock company to prove the agent generalises beyond Summit Restoration

---

## Production Signals to Watch

**AE open rate on the Slack message** — if it drops, the cards aren't useful. That's the signal, not whether the agent ran, but whether the output changed behaviour.

**Origami credit usage per week** — if it spikes, the dedup gate broke and companies are being re-enriched unnecessarily.

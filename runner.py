"""
TrueRestore Account Research Agent — CLI runner.

Usage:
    python runner.py "Summit Restoration & Reconstruction"
    python runner.py "Summit Restoration" --ae "Jordan Malone"
    python runner.py --help
"""

import argparse
import re
import sys
import time
from datetime import date
from pathlib import Path

import config
from agent import run_agent, ToolLogEntry


# ---------------------------------------------------------------------------
# Status display helpers
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "battle_card_posted":     "Battle Card Posted",
    "skipped_cached":         "Skipped — Already Researched",
    "low_confidence_warning": "Low Confidence Warning",
    "enrichment_failed":      "Enrichment Failed",
    "error":                  "Error",
}

_STATUS_ICONS = {
    "battle_card_posted":     "✓",
    "skipped_cached":         "~",
    "low_confidence_warning": "!",
    "enrichment_failed":      "✗",
    "error":                  "✗",
}

_TOOL_ICONS = {
    "ok":      "✓",
    "blocked": "⊘",
    "error":   "✗",
}

W = 62  # box inner width


def _box_top() -> str:  return "╔" + "═" * W + "╗"
def _box_bot() -> str:  return "╚" + "═" * W + "╝"
def _box_mid() -> str:  return "╠" + "═" * W + "╣"
def _row(text: str) -> str:
    return "║  " + text.ljust(W - 2) + "║"
def _blank() -> str:    return "║" + " " * W + "║"


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------

def print_summary(result: dict, elapsed: float, md_path: Path | None) -> None:
    status     = result.get("status", "error")
    company    = result.get("company_name", "—")
    ae_name    = result.get("ae_name") or "—"
    confidence = result.get("confidence_score")
    hs_id      = result.get("hs_object_id") or "—"
    slack_url  = result.get("slack_permalink")
    iterations = result.get("iterations_used", 0)
    tool_log   = result.get("tool_log", [])

    status_label = _STATUS_LABELS.get(status, status)
    status_icon  = _STATUS_ICONS.get(status, "?")
    conf_str     = f"{confidence:.0%}" if confidence is not None else "—"

    print()
    print(_box_top())
    print(_row("TRUERESTORE  ACCOUNT RESEARCH  —  RUN SUMMARY"))
    print(_box_mid())
    print(_blank())
    print(_row(f"  Company     {company}"))
    print(_row(f"  AE          {ae_name}"))
    print(_row(f"  HubSpot ID  {hs_id}"))
    print(_row(f"  Confidence  {conf_str}"))
    print(_row(f"  Status      {status_icon}  {status_label}"))
    if slack_url:
        print(_row(f"  Battle card  {slack_url}"))
    if md_path:
        print(_row(f"  Markdown    {md_path}"))
    print(_row(f"  Elapsed     {elapsed:.1f}s   ({iterations}/{config.MAX_LOOP_ITERATIONS} iterations)"))
    if config.SHADOW_MODE:
        print(_row(f"  Mode        SHADOW — no real writes made"))
    print(_blank())

    if tool_log:
        print(_box_mid())
        print(_blank())
        print(_row("  TOOL EXECUTION LOG"))
        print(_blank())

        name_col = max((len(e.tool_name) for e in tool_log), default=26)
        name_col = max(name_col, 26)

        for entry in tool_log:
            icon  = _TOOL_ICONS.get(entry.status, "?")
            name  = entry.tool_name.ljust(name_col)
            line  = f"  {entry.step:>2}.  {icon}  {name}  {entry.summary}"
            print(_row(line[: W - 2]))

        print(_blank())

    print(_box_bot())
    print()


# ---------------------------------------------------------------------------
# Battle card markdown writer
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    """Convert a company name to a safe filename slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug.strip("-")


def write_battle_card_md(result: dict) -> Path | None:
    """
    Find the post_battle_card tool call in the tool log, extract its inputs,
    and render them as a markdown file in output/.

    Returns the Path written, or None if no battle card was posted this run.
    """
    tool_log: list[ToolLogEntry] = result.get("tool_log", [])

    bc_entry = next(
        (e for e in tool_log if e.tool_name == "post_battle_card" and e.status == "ok"),
        None,
    )
    if bc_entry is None:
        return None

    data = bc_entry.tool_input

    company_name      = data.get("company_name", result.get("company_name", "Unknown"))
    ae_name           = data.get("ae_name", result.get("ae_name", "—"))
    icp_fit           = data.get("icp_fit", "—").upper()
    icp_rationale     = data.get("icp_rationale", "")
    top_pain_points   = data.get("top_pain_points", [])
    key_contacts      = data.get("key_contacts", [])
    buying_signals    = data.get("buying_signals", [])
    likely_objections = data.get("likely_objections", [])
    recommended_opener= data.get("recommended_opener", "")
    confidence_score  = data.get("confidence_score") or result.get("confidence_score")

    conf_str  = f"{confidence_score:.0%}" if confidence_score is not None else "—"
    today     = date.today().strftime("%Y-%m-%d")

    lines = [
        f"# Battle Card — {company_name}",
        "",
        f"| | |",
        f"|---|---|",
        f"| **AE** | {ae_name} |",
        f"| **ICP Fit** | {icp_fit} |",
        f"| **Confidence** | {conf_str} |",
        f"| **Generated** | {today} |",
        "",
        "---",
        "",
        "## ICP Rationale",
        "",
        f"> {icp_rationale}",
        "",
        "---",
        "",
        "## Top Pain Points",
        "",
    ]
    for point in top_pain_points:
        lines.append(f"- {point}")

    lines += [
        "",
        "---",
        "",
        "## Key Contacts",
        "",
        "| Name | Title | Role | Notes |",
        "|------|-------|------|-------|",
    ]
    for c in key_contacts:
        role  = c.get("role", "").replace("_", " ").title()
        note  = c.get("note", "")
        lines.append(f"| **{c.get('name', '')}** | {c.get('title', '')} | {role} | {note} |")

    lines += [
        "",
        "---",
        "",
        "## Buying Signals",
        "",
    ]
    for signal in buying_signals:
        lines.append(f"- {signal}")

    lines += [
        "",
        "---",
        "",
        "## Likely Objections & Counters",
        "",
    ]
    for obj in likely_objections:
        lines += [
            f"**Objection:** {obj.get('objection', '')}  ",
            f"**Counter:** {obj.get('counter', '')}",
            "",
        ]

    lines += [
        "---",
        "",
        "## Recommended Opener",
        "",
        f'> "{recommended_opener}"',
        "",
        "---",
        "",
        f"*Generated by TrueRestore Account Research Agent · {today}*",
    ]

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    filename = f"{_slug(company_name)}.md"
    path     = output_dir / filename

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        description="TrueRestore Account Research Agent — research a company for an AE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python runner.py \"Summit Restoration & Reconstruction\"\n"
            "  python runner.py \"Summit Restoration\" --ae \"Jordan Malone\"\n"
        ),
    )
    parser.add_argument(
        "company_name",
        help="Company name to research (as submitted in HubSpot).",
    )
    parser.add_argument(
        "--ae",
        dest="ae_name",
        default="",
        metavar="NAME",
        help="AE name override (optional — discovered from HubSpot if omitted).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    company_name = args.company_name.strip()
    ae_name      = (args.ae_name or "").strip()

    ok, reason = config.validate_company_name(company_name)
    if not ok:
        print(f"\n  ERROR  Invalid company name: {reason}\n")
        sys.exit(1)

    if config.SHADOW_MODE:
        print(f"\n  [shadow mode] Starting research for: {company_name}")
    else:
        print(f"\n  Starting research for: {company_name}")

    start = time.monotonic()

    try:
        result = run_agent(company_name=company_name, ae_name=ae_name)
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user.\n")
        sys.exit(130)
    except Exception as e:
        print(f"\n  FATAL  Unhandled agent error: {e}\n")
        raise

    elapsed = time.monotonic() - start

    md_path = write_battle_card_md(result)

    print_summary(result, elapsed, md_path)

    status = result.get("status", "error")
    if status in ("battle_card_posted", "skipped_cached"):
        sys.exit(0)
    elif status == "low_confidence_warning":
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

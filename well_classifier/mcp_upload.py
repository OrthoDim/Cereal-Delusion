"""
MCP upload helper for single-cell cloning well classification.

Formats classification results into prompts for the Monomer Cloud MCP.
Designed to work with Claude Code or Cursor MCP integration.

Usage:
    python mcp_upload.py <results.json> [--plate-barcode BARCODE]
"""

import argparse
import json
import sys
from pathlib import Path


def generate_mcp_prompt(results: dict, plate_barcode: str = "<PLATE_BARCODE>") -> str:
    """Generate a natural language prompt for MCP upload via Claude/Cursor.

    This prompt can be pasted into an MCP-connected client to trigger
    bulk status updates and comment creation.
    """
    wells = results.get("wells", [])
    summary = results.get("summary", {})

    lines = [
        f"I have classification results for plate {plate_barcode}. "
        f"For each well, I have a label (empty, single, multiple, or uncertain) "
        f"and a confidence score.",
        "",
        "First, use list_culture_statuses() to find the status IDs that correspond to my labels.",
        "Then, for each well:",
        "1. Update the culture status to match the classification label using update_culture_status.",
        "2. For any well classified as 'uncertain' or with confidence below 0.8, add a comment to the",
        "   culture explaining what the algorithm detected and what the scientist should verify.",
        "",
        "Here are my results:",
    ]

    for well in wells:
        well_id = well["well_id"]
        label = well["label"]
        confidence = well["confidence"]
        reason = well.get("reason", "")

        entry = f"  {well_id}: {label} ({confidence:.2f})"
        if label == "uncertain" or confidence < 0.80:
            entry += f" — {reason}"
        lines.append(entry)

    lines.extend([
        "",
        f"Summary: {summary.get('single', '?')} single, "
        f"{summary.get('empty', '?')} empty, "
        f"{summary.get('multiple', '?')} multiple, "
        f"{summary.get('uncertain', '?')} uncertain. "
        f"Estimated λ = {summary.get('estimated_lambda', '?')}.",
    ])

    return "\n".join(lines)


def generate_status_script(results: dict) -> str:
    """Generate a Python script snippet for direct MCP API calls.

    This is a template — actual MCP client code depends on the integration.
    """
    wells = results.get("wells", [])

    lines = [
        '"""',
        "Auto-generated MCP upload script.",
        "Run within an MCP-connected environment.",
        '"""',
        "",
        "# Step 1: Get available culture statuses",
        "statuses = await mcp.list_culture_statuses()",
        "status_map = {s['name'].lower(): s['id'] for s in statuses}",
        "",
        "# Step 2: Update each well",
        "results = [",
    ]

    for well in wells:
        lines.append(
            f'    {{"well_id": "{well["well_id"]}", '
            f'"label": "{well["label"]}", '
            f'"confidence": {well["confidence"]:.2f}}},'
        )

    lines.extend([
        "]",
        "",
        "for r in results:",
        "    status_id = status_map.get(r['label'])",
        "    if status_id:",
        "        await mcp.update_culture_status(",
        "            culture_id=CULTURE_ID,",
        "            status_id=status_id,",
        "            wells=[r['well_id']],",
        "        )",
        "",
        "    # Add comments for uncertain/low-confidence wells",
        "    if r['label'] == 'uncertain' or r['confidence'] < 0.80:",
        "        await mcp.add_comment(",
        "            entity_type='culture',",
        "            entity_id=CULTURE_ID,",
        "            content=f\"Well {r['well_id']}: {r['label']} (confidence: {r['confidence']:.2f})\",",
        "        )",
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate MCP upload prompts from classification results."
    )
    parser.add_argument("results_json", type=Path,
                        help="Path to classification results JSON")
    parser.add_argument("--plate-barcode", default="<PLATE_BARCODE>",
                        help="Plate barcode for MCP identification")
    parser.add_argument("--format", choices=["prompt", "script", "both"], default="prompt",
                        help="Output format: 'prompt' for natural language, 'script' for Python")

    args = parser.parse_args()

    if not args.results_json.exists():
        print(f"Error: {args.results_json} not found", file=sys.stderr)
        sys.exit(1)

    results = json.loads(args.results_json.read_text())

    if args.format in ("prompt", "both"):
        print("=" * 60)
        print("MCP UPLOAD PROMPT (paste into Claude/Cursor with MCP)")
        print("=" * 60)
        print(generate_mcp_prompt(results, args.plate_barcode))

    if args.format in ("script", "both"):
        print("\n" + "=" * 60)
        print("MCP UPLOAD SCRIPT")
        print("=" * 60)
        print(generate_status_script(results))


if __name__ == "__main__":
    main()

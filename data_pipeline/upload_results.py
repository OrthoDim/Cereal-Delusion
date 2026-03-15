#!/usr/bin/env python3
"""Upload single-cell classification results to Monomer Cloud.

Reads a detailed_results JSON file and updates culture statuses + adds
comments in Monomer Cloud via MCP.

Usage:
    python -m data_pipeline.upload_results annotated_output/CerealDelusion_Run2B/detailed_results_CerealDelusion_Run2B.json
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .monomer_client import MonomerMCPClient

LABEL_TO_STATUS_NAME = {
    "empty": "Empty",
    "single": "Single Bead",
    "multiple": "Multiple Beads",
    "multiple_clusters": "Multiple Beads",
    "uncertain": "Uncertain",
}

CONFIDENCE_THRESHOLD = 0.80


def _build_status_map(client: MonomerMCPClient) -> dict[str, str]:
    """Fetch culture statuses and build name→id map (case-insensitive)."""
    data = client.call_tool("list_culture_statuses")
    if not data:
        print("Error: could not fetch culture statuses.", file=sys.stderr)
        sys.exit(1)

    items = data.get("items", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = [items]

    status_map = {}
    for s in items:
        name = s.get("name", "")
        status_map[name.lower()] = s["id"]

    return status_map


def _ensure_uncertain_status(client: MonomerMCPClient, status_map: dict[str, str]) -> dict[str, str]:
    """Create the 'Uncertain' status if it doesn't exist yet."""
    if "uncertain" in status_map:
        return status_map

    print("Creating 'Uncertain' culture status...")
    result = client.call_tool("create_culture_status", {
        "name": "Uncertain",
        "icon": "QUESTION",
        "category": "active",
    })
    if result and isinstance(result, dict) and "id" in result:
        status_map["uncertain"] = result["id"]
        print(f"  Created status: {result['id']}")
    else:
        print(f"  Warning: could not create Uncertain status: {result}", file=sys.stderr)

    return status_map


def _get_plate_cultures(client: MonomerMCPClient, barcode: str) -> tuple[str, dict[str, str]]:
    """Look up plate by barcode, return (plate_id, {well_id: culture_id})."""
    data = client.call_tool("get_plate_details", {
        "plate_queries": [{"by": "name", "value": barcode}],
    })

    # Response is a list of results; extract the first one
    if isinstance(data, list):
        if not data:
            print(f"Error: plate '{barcode}' not found in Monomer Cloud.", file=sys.stderr)
            sys.exit(1)
        result = data[0]
    elif isinstance(data, dict):
        result = data
    else:
        print(f"Error: unexpected response from get_plate_details: {data}", file=sys.stderr)
        sys.exit(1)

    if "error" in result:
        print(f"Error: plate '{barcode}' not found: {result['error']}", file=sys.stderr)
        sys.exit(1)

    plate = result.get("plate", result)
    plate_id = plate["id"]

    # Get cultures — plate_id here is the external ID
    cultures_data = client.call_tool("list_cultures", {"plate_id": plate_id})
    cultures = cultures_data.get("items", []) if cultures_data else []

    well_to_culture = {}
    for c in cultures:
        well_to_culture[c["well"]] = c["id"]

    return plate_id, well_to_culture


def upload_results(results_data: dict, barcode: str, client: MonomerMCPClient | None = None):
    """Upload classification results to Monomer Cloud.

    Args:
        results_data: Parsed detailed_results JSON (has "summary" and "wells" keys).
        barcode: Plate barcode string.
        client: Optional pre-initialized client. Created if None.
    """
    if client is None:
        print("Connecting to Monomer Cloud MCP...")
        client = MonomerMCPClient()

    # Resolve plate and cultures
    print(f"Looking up plate '{barcode}'...")
    plate_id, well_to_culture = _get_plate_cultures(client, barcode)
    print(f"  Plate ID: {plate_id}, {len(well_to_culture)} culture(s)")

    # Build status map
    print("Fetching culture statuses...")
    status_map = _build_status_map(client)
    status_map = _ensure_uncertain_status(client, status_map)

    wells = results_data.get("wells", [])
    summary = results_data.get("summary", {})

    updated = 0
    commented = 0
    skipped = 0

    for well in wells:
        well_id = well["well_id"]
        label = well["label"]
        confidence = well["confidence"]
        reason = well.get("reason", "")
        bead_count = well.get("bead_count", 0)

        culture_id = well_to_culture.get(well_id)
        if not culture_id:
            print(f"  {well_id}: no culture found, skipping")
            skipped += 1
            continue

        # Update status
        target_status_name = LABEL_TO_STATUS_NAME.get(label)
        if target_status_name:
            status_id = status_map.get(target_status_name.lower())
            if status_id:
                client.call_tool("update_culture_status", {
                    "culture_id": culture_id,
                    "status_id": status_id,
                    "wells": [well_id],
                })
                updated += 1
                print(f"  {well_id}: {label} -> {target_status_name}")
            else:
                print(f"  {well_id}: status '{target_status_name}' not found, skipping update")
        else:
            print(f"  {well_id}: unknown label '{label}', skipping update")

        # Add comment for uncertain or low-confidence wells
        if label == "uncertain" or confidence < CONFIDENCE_THRESHOLD:
            comment = f"Well {well_id}: {label} (confidence: {confidence:.2f}). {reason}"
            client.call_tool("add_comment", {
                "entity_type": "culture",
                "entity_id": culture_id,
                "content": comment,
            })
            commented += 1

    # Plate-level summary comment
    summary_lines = [f"Single-cell classification results ({date.today().isoformat()}):"]
    for well in wells:
        w = well["well_id"]
        l = well["label"]
        bc = well.get("bead_count", 0)
        conf = well["confidence"]
        summary_lines.append(f"  {w}: {l} ({bc} beads, conf={conf:.2f})")

    summary_lines.append(
        f"Summary: {summary.get('single_wells', '?')} single, "
        f"{summary.get('empty_wells', '?')} empty, "
        f"{summary.get('multiple_wells', 0) + summary.get('multiple_clusters_wells', 0)} multiple, "
        f"{summary.get('uncertain_wells', '?')} uncertain. "
        f"Estimated lambda = {summary.get('estimated_lambda', '?')}."
    )

    client.call_tool("add_comment", {
        "entity_type": "plate",
        "entity_id": plate_id,
        "content": "\n".join(summary_lines),
    })

    print(f"\nUpload complete: {updated} statuses updated, {commented} comments added, {skipped} skipped.")


def main():
    parser = argparse.ArgumentParser(
        description="Upload single-cell classification results to Monomer Cloud."
    )
    parser.add_argument("results_json", type=Path,
                        help="Path to detailed_results_<barcode>.json")
    parser.add_argument("--plate-barcode", default=None,
                        help="Override plate barcode (default: extracted from filename)")
    args = parser.parse_args()

    if not args.results_json.exists():
        print(f"Error: {args.results_json} not found", file=sys.stderr)
        sys.exit(1)

    results_data = json.loads(args.results_json.read_text())

    # Extract barcode from filename: detailed_results_<barcode>.json
    if args.plate_barcode:
        barcode = args.plate_barcode
    else:
        stem = args.results_json.stem  # e.g. "detailed_results_CerealDelusion_Run2B"
        prefix = "detailed_results_"
        if stem.startswith(prefix):
            barcode = stem[len(prefix):]
        else:
            print("Error: could not extract barcode from filename. Use --plate-barcode.", file=sys.stderr)
            sys.exit(1)

    upload_results(results_data, barcode)


if __name__ == "__main__":
    main()

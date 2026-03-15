"""Single-cell detection tool for 96-well plate fluorescence microscopy.

Classifies well images as:
  - empty: no beads detected
  - single: exactly one bead detected
  - multiple: two or more beads detected
  - multiple_clusters: beads plus unsplit clusters
  - uncertain: detection ambiguous (flagged for human review)

Usage:
    python -m well_classifier.classify_single_cells <image_dir> [--output-dir DIR] [--annotate] [--debug]
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .core import (
    BeadInfo, WellResult, REFERENCE_DIM, DEFAULTS, WELL_ROWS, WELL_COLS,
    preprocess_well_image, segment_beads, load_well_images,
    debug_overlay, annotated_bead_image, results_to_mcp_payload,
)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_well(
    image: np.ndarray,
    well_id: str = "unknown",
    min_area: int = 50,
    max_area: int = 8000,
    intensity_thresh: Optional[int] = None,
    min_circularity: float = 0.50,
    watershed_min_distance: int = 10,
    **_kwargs,
) -> WellResult:
    """Classify a single well image for single-cell detection."""
    gray, _quad_medians = preprocess_well_image(image)

    labeled, beads, clusters = segment_beads(
        gray, intensity_thresh,
        min_area, max_area, watershed_min_distance, min_circularity,
    )

    areas = [b.area for b in beads]
    bead_dicts = [asdict(b) for b in beads]
    cluster_dicts = [asdict(c) for c in clusters]
    bead_count = len(beads)
    cluster_count = len(clusters)
    total_objects = bead_count + cluster_count

    if total_objects == 0:
        mean_val = gray.mean()
        if mean_val < 15:
            return WellResult(well_id, "empty", 0.95, 0, 0, areas,
                              "No objects detected, low background",
                              bead_dicts, cluster_dicts)
        else:
            return WellResult(well_id, "uncertain", 0.40, 0, 0, areas,
                              f"No objects but elevated background (mean={mean_val:.1f})",
                              bead_dicts, cluster_dicts)

    parts = []
    if bead_count:
        parts.append(f"{bead_count} bead(s)")
    if cluster_count:
        parts.append(f"{cluster_count} cluster(s)")
    reason = ", ".join(parts)

    if total_objects == 1 and cluster_count == 0:
        conf = _single_confidence(beads[0])
        return WellResult(well_id, "single", conf, 1, 0, areas, reason,
                          bead_dicts, cluster_dicts)

    # Check for split artifact when exactly 2 beads, no clusters
    if bead_count == 2 and cluster_count == 0:
        dist = np.sqrt(
            (beads[0].centroid[0] - beads[1].centroid[0]) ** 2
            + (beads[0].centroid[1] - beads[1].centroid[1]) ** 2
        )
        avg_r = np.sqrt(np.mean([b.area for b in beads]) / np.pi)
        if dist < avg_r * 2.5:
            return WellResult(well_id, "uncertain", 0.50, 2, 0, areas,
                              f"Two objects very close (dist={dist:.0f}px), "
                              "may be one bead split by threshold",
                              bead_dicts, cluster_dicts)

    label = "multiple_clusters" if cluster_count > 0 else "multiple"
    return WellResult(well_id, label, 0.90, bead_count, cluster_count, areas,
                      reason, bead_dicts, cluster_dicts)


def _single_confidence(bead: BeadInfo) -> float:
    """Compute confidence for a single-bead classification."""
    conf = 0.70
    if bead.circularity > 0.85:
        conf += 0.15
    elif bead.circularity > 0.70:
        conf += 0.08
    if bead.solidity > 0.90:
        conf += 0.10
    elif bead.solidity > 0.80:
        conf += 0.05
    return min(conf, 0.98)


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def classify_plate(
    image_dir: Path,
    **kwargs,
) -> tuple[list[WellResult], list[Path]]:
    """Classify all well images in a directory."""
    loaded = load_well_images(image_dir)
    results = []
    source_paths = []

    for well_id, image, img_path in loaded:
        result = classify_well(image, well_id, **kwargs)
        results.append(result)
        source_paths.append(img_path)
        print(f"  {well_id}: {result.label} "
              f"(conf={result.confidence:.2f}, beads={result.bead_count}, "
              f"clusters={result.cluster_count})")

    return results, source_paths


# ---------------------------------------------------------------------------
# Summary & output
# ---------------------------------------------------------------------------

def summarize_results(results: list[WellResult]) -> dict:
    """Generate a summary of classification results with Poisson lambda."""
    total = len(results)
    if total == 0:
        return {"total": 0}

    counts = {"empty": 0, "single": 0, "multiple": 0, "multiple_clusters": 0, "uncertain": 0}
    for r in results:
        counts[r.label] += 1

    return {
        "total_wells": total,
        "empty_wells": counts["empty"],
        "single_wells": counts["single"],
        "multiple_wells": counts["multiple"],
        "multiple_clusters_wells": counts["multiple_clusters"],
        "uncertain_wells": counts["uncertain"],
        "single_pct": round(100 * counts["single"] / total, 1),
        "empty_pct": round(100 * counts["empty"] / total, 1),
        "estimated_lambda": round(
            -np.log(counts["empty"] / total)
            if 0 < counts["empty"] < total else 0,
            3,
        ),
    }


def results_to_plate_json(results: list[WellResult], plate_id: str = "PLT-001") -> dict:
    """Format results as single-cell detection JSON."""
    wells = {}
    for r in results:
        wells[r.well_id] = {
            "cell_count": r.bead_count + r.cluster_count,
            "label": r.label,
            "confidence": round(r.confidence, 2),
            "empty": r.label == "empty",
            "flagged_for_review": r.label in ("uncertain", "multiple_clusters"),
        }

    summary = summarize_results(results)

    return {
        "plate_id": plate_id,
        "analysis_type": "single_cell_detection",
        "date": date.today().isoformat(),
        "wells": wells,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Single-cell detection for 96-well plate images."
    )
    parser.add_argument("image_dir", type=Path, help="Directory of well images")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output directory (default: annotated_output_<barcode>/)")
    parser.add_argument("--annotate", action="store_true",
                        help="Save annotated images with color-coded beads")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug overlay images")
    parser.add_argument("--min-area", type=int, default=DEFAULTS["min_area"])
    parser.add_argument("--max-area", type=int, default=DEFAULTS["max_area"])
    parser.add_argument("--threshold", type=int, default=None,
                        help="Manual intensity threshold (default: Otsu)")
    parser.add_argument("--min-circularity", type=float,
                        default=DEFAULTS["min_circularity"])
    parser.add_argument("--mcp", action="store_true",
                        help="Include MCP upload payload in output")

    args = parser.parse_args()

    if not args.image_dir.is_dir():
        print(f"Error: {args.image_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Derive plate barcode from image directory name
    barcode = args.image_dir.name
    output_dir = args.output_dir or Path(f"annotated_output_{barcode}")
    output_dir.mkdir(parents=True, exist_ok=True)

    classify_kwargs = {
        "min_area": args.min_area,
        "max_area": args.max_area,
        "intensity_thresh": args.threshold,
        "min_circularity": args.min_circularity,
    }

    results, source_paths = classify_plate(args.image_dir, **classify_kwargs)

    if not results:
        print("No results generated.", file=sys.stderr)
        sys.exit(1)

    # Detailed results (full per-well bead/cluster data)
    detailed = {
        "summary": summarize_results(results),
        "wells": [asdict(r) for r in results],
    }
    if args.mcp:
        detailed["mcp_payload"] = results_to_mcp_payload(results)

    detailed_path = output_dir / f"detailed_results_{barcode}.json"
    detailed_path.write_text(json.dumps(detailed, indent=2))
    print(f"\nDetailed results written to {detailed_path}")

    # Single-cell results JSON
    plate_json = results_to_plate_json(results, plate_id=barcode)
    plate_json_path = output_dir / f"single_cell_results_{barcode}.json"
    plate_json_path.write_text(json.dumps(plate_json, indent=2))
    print(f"Single-cell results written to {plate_json_path}")

    # Debug overlays
    if args.debug:
        for r, img_path in zip(results, source_paths):
            img = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
            if img is not None:
                overlay = debug_overlay(img, r.well_id, result=r)
                cv2.imwrite(str(output_dir / f"{r.well_id}_{img_path.stem}_debug.png"), overlay)

    # Annotated bead images
    if args.annotate:
        for r, img_path in zip(results, source_paths):
            img = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
            if img is not None:
                ann = annotated_bead_image(img, r.well_id, result=r)
                out_name = f"{r.well_id}_{img_path.stem}_annotated.png"
                cv2.imwrite(str(output_dir / out_name), ann)
                print(f"  Annotated: {out_name}")

    # Summary
    summary = plate_json["summary"]
    print(f"\n--- Summary ---")
    print(f"Total wells:     {summary['total_wells']}")
    print(f"Single:          {summary['single_wells']} ({summary['single_pct']}%)")
    print(f"Empty:           {summary['empty_wells']} ({summary['empty_pct']}%)")
    print(f"Multiple:        {summary['multiple_wells']}")
    print(f"Multiple+Clust:  {summary['multiple_clusters_wells']}")
    print(f"Uncertain:       {summary['uncertain_wells']}")
    print(f"Estimated lambda: {summary['estimated_lambda']}")


if __name__ == "__main__":
    main()

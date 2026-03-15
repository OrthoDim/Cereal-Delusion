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
    check_multimodal_intensity,
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
    gray, _quad_medians, _gray_pre_seam = preprocess_well_image(image)

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

    result = None

    if total_objects == 0:
        mean_val = gray.mean()
        if mean_val < 15:
            result = WellResult(well_id, "empty", 0.95, 0, 0, areas,
                                "No objects detected, low background",
                                bead_dicts, cluster_dicts)
        else:
            result = WellResult(well_id, "uncertain", 0.40, 0, 0, areas,
                                f"No objects but elevated background (mean={mean_val:.1f})",
                                bead_dicts, cluster_dicts)

    if result is None:
        parts = []
        if bead_count:
            parts.append(f"{bead_count} bead(s)")
        if cluster_count:
            parts.append(f"{cluster_count} cluster(s)")
        reason = ", ".join(parts)

        if total_objects == 1 and cluster_count == 0:
            conf = _single_confidence(beads[0])
            # Check for merged beads via intensity profile
            peak_count = check_multimodal_intensity(
                gray, labeled, beads[0], 1, min_peak_distance=5,
            )
            if peak_count >= 2:
                conf -= 0.15
                reason += f" (multi-peak={peak_count})"
            if conf < 0.60:
                result = WellResult(well_id, "uncertain", conf, 1, 0, areas,
                                    reason, bead_dicts, cluster_dicts)
            else:
                result = WellResult(well_id, "single", conf, 1, 0, areas,
                                    reason, bead_dicts, cluster_dicts)

    if result is None:
        parts = []
        if bead_count:
            parts.append(f"{bead_count} bead(s)")
        if cluster_count:
            parts.append(f"{cluster_count} cluster(s)")
        reason = ", ".join(parts)

        # Check for split artifact when exactly 2 beads, no clusters
        if bead_count == 2 and cluster_count == 0:
            dist = np.sqrt(
                (beads[0].centroid[0] - beads[1].centroid[0]) ** 2
                + (beads[0].centroid[1] - beads[1].centroid[1]) ** 2
            )
            avg_r = np.sqrt(np.mean([b.area for b in beads]) / np.pi)
            if dist < avg_r * 2.5:
                result = WellResult(well_id, "uncertain", 0.50, 2, 0, areas,
                                    f"Two objects very close (dist={dist:.0f}px), "
                                    "may be one bead split by threshold",
                                    bead_dicts, cluster_dicts)

    if result is None:
        label = "multiple_clusters" if cluster_count > 0 else "multiple"
        result = WellResult(well_id, label, 0.90, bead_count, cluster_count,
                            areas, reason, bead_dicts, cluster_dicts)

    # Sensitive resegment pass — find faint objects missed by primary threshold
    primary_centroids = [b.centroid for b in beads + clusters]
    sensitive_beads, sensitive_clusters = _sensitive_resegment(gray)
    sensitive_all = sensitive_beads + sensitive_clusters
    faint_dicts = []
    for sb in sensitive_all:
        sc = sb.centroid
        is_new = True
        for pc in primary_centroids:
            dist = np.sqrt((sc[0] - pc[0]) ** 2 + (sc[1] - pc[1]) ** 2)
            if dist < 30:
                is_new = False
                break
        if is_new:
            faint_dicts.append(asdict(sb))
    result.faint_objects.extend(faint_dicts)

    sensitive_total = len(sensitive_all)
    # Reclassify empty/single if sensitive pass found objects
    if result.label == "empty" and sensitive_total > 0:
        result = WellResult(
            result.well_id, "uncertain", 0.45,
            result.bead_count, result.cluster_count, result.bead_areas,
            result.reason + f" (sensitive pass found {sensitive_total} object(s))",
            result.beads, result.clusters, faint_dicts,
        )
    elif result.label == "single" and sensitive_total > 1:
        result = WellResult(
            result.well_id, "uncertain", 0.50,
            result.bead_count, result.cluster_count, result.bead_areas,
            result.reason + f" (sensitive pass found {sensitive_total} object(s))",
            result.beads, result.clusters, faint_dicts,
        )

    return result


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
    # Penalize elongated objects — merged beads form ellipses
    if bead.eccentricity > 0.80:
        conf -= 0.15
    elif bead.eccentricity > 0.60:
        conf -= 0.07
    return min(conf, 0.98)


def _sensitive_resegment(
    gray: np.ndarray,
    min_area: int = 15,
    min_circularity: float = 0.40,
    sensitivity_factor: float = 0.5,
) -> tuple[list, list]:
    """Re-segment with a lower threshold to catch faint cells.

    Returns (beads, clusters) from the sensitive pass.
    """
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    otsu_val, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    sensitive_thresh = max(15, int(otsu_val * sensitivity_factor))
    _, beads, clusters = segment_beads(
        gray, intensity_thresh=sensitive_thresh,
        min_area=min_area, max_area=800,
        min_distance=4, min_circularity=min_circularity,
    )
    return beads, clusters


def _apply_area_anomaly_scoring(
    results: list,
    area_ratio_threshold: float = 1.5,
) -> None:
    """Post-process plate results: flag single wells with anomalously large area.

    Modifies results in place.
    """
    single_areas = []
    single_indices = []
    for i, r in enumerate(results):
        if r.label == "single" and r.bead_areas:
            single_areas.append(r.bead_areas[0])
            single_indices.append(i)

    if len(single_areas) < 3:
        return

    median_area = float(np.median(single_areas))

    for idx in single_indices:
        r = results[idx]
        if r.bead_areas[0] > median_area * area_ratio_threshold:
            new_conf = r.confidence - 0.15
            new_label = "uncertain" if new_conf < 0.60 else r.label
            results[idx] = WellResult(
                r.well_id, new_label, new_conf,
                r.bead_count, r.cluster_count, r.bead_areas,
                r.reason + f" (area {r.bead_areas[0]} > {area_ratio_threshold}x median {median_area:.0f})",
                r.beads, r.clusters,
            )


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

    _apply_area_anomaly_scoring(results)

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
                        help="Output directory (default: annotated_output/<barcode>/)")
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
    parser.add_argument("--upload", action="store_true",
                        help="Upload results to Monomer Cloud after classification")

    args = parser.parse_args()

    if not args.image_dir.is_dir():
        print(f"Error: {args.image_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Derive plate barcode from image directory name
    barcode = args.image_dir.name
    output_dir = args.output_dir or Path("annotated_output") / barcode
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

    # Upload to Monomer Cloud
    if args.upload:
        print("\nUploading results to Monomer Cloud...")
        from data_pipeline.upload_results import upload_results
        upload_results(detailed, barcode)


if __name__ == "__main__":
    main()

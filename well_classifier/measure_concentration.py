"""Concentration measurement from dilution series for 96-well plate microscopy.

Counts beads per well across a dilution series and estimates stock concentration
using median and linear-fit methods.

Usage:
    python -m well_classifier.measure_concentration <image_dir> \\
        --dilution-config <input.json> [--output-dir DIR] [--annotate]

    python -m well_classifier.measure_concentration <image_dir> \\
        --wells A1:H1 --dilution-ratio 2 --volume 200 [--output-dir DIR]
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
# Dilution config
# ---------------------------------------------------------------------------

def load_dilution_config(path: Path) -> dict:
    """Parse a dilution series configuration JSON."""
    with open(path) as f:
        config = json.load(f)
    required = ("plate_id", "dilution_series")
    for key in required:
        if key not in config:
            raise ValueError(f"Dilution config missing required key: {key}")
    for entry in config["dilution_series"]:
        for key in ("well_id", "dilution_factor", "volume_ul"):
            if key not in entry:
                raise ValueError(f"Dilution series entry missing key: {key}")
    return config


def build_dilution_config_from_args(
    wells_spec: str,
    dilution_ratio: float,
    volume_ul: float,
    plate_id: str = "unknown",
    stock_label: str = "bead stock",
) -> dict:
    """Build dilution config from CLI shorthand arguments.

    wells_spec: "A1:H1" means column 1, rows A-H
    dilution_ratio: fold-dilution between consecutive wells (e.g. 2 for 1:2 serial)
    """
    parts = wells_spec.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid wells spec '{wells_spec}', expected format like 'A1:H1'")

    start_well, end_well = parts[0].upper(), parts[1].upper()
    start_row, start_col = start_well[0], int(start_well[1:])
    end_row, end_col = end_well[0], int(end_well[1:])

    if start_col != end_col:
        raise ValueError("Shorthand wells spec only supports single-column ranges (e.g. A1:H1)")

    col = start_col
    start_idx = WELL_ROWS.index(start_row)
    end_idx = WELL_ROWS.index(end_row)

    series = []
    cumulative_factor = 1.0
    for i, row_idx in enumerate(range(start_idx, end_idx + 1)):
        if i == 0:
            cumulative_factor = dilution_ratio
        else:
            cumulative_factor = dilution_ratio ** (i + 1)
        series.append({
            "well_id": f"{WELL_ROWS[row_idx]}{col}",
            "dilution_factor": cumulative_factor,
            "volume_ul": volume_ul,
        })

    return {
        "plate_id": plate_id,
        "stock_label": stock_label,
        "dilution_series": series,
    }


# ---------------------------------------------------------------------------
# Per-well counting
# ---------------------------------------------------------------------------

def count_well(
    img: np.ndarray,
    well_id: str,
    min_area: int = 25,
    max_area: int = 800,
    intensity_thresh: Optional[int] = None,
    min_circularity: float = 0.50,
    watershed_min_distance: int = 4,
) -> WellResult:
    """Run preprocessing + segment_beads on a single well image."""
    gray, _quad_medians = preprocess_well_image(img)

    labeled, beads, clusters = segment_beads(
        gray, intensity_thresh,
        min_area, max_area, watershed_min_distance, min_circularity,
    )

    areas = [b.area for b in beads]
    bead_dicts = [asdict(b) for b in beads]
    cluster_dicts = [asdict(c) for c in clusters]
    bead_count = len(beads)
    cluster_count = len(clusters)

    parts = []
    if bead_count:
        parts.append(f"{bead_count} bead(s)")
    if cluster_count:
        parts.append(f"{cluster_count} cluster(s)")
    reason = ", ".join(parts) if parts else "No objects detected"

    label = "counted"
    if bead_count == 0 and cluster_count == 0:
        label = "empty"

    return WellResult(
        well_id=well_id,
        label=label,
        confidence=0.90,
        bead_count=bead_count,
        cluster_count=cluster_count,
        bead_areas=areas,
        reason=reason,
        beads=bead_dicts,
        clusters=cluster_dicts,
    )


# ---------------------------------------------------------------------------
# Concentration fitting
# ---------------------------------------------------------------------------

def fit_concentration(
    well_counts: list[dict],
    max_cluster_ratio: float = 0.2,
    max_reliable_count: int = 500,
) -> dict:
    """Estimate stock concentration from dilution series bead counts.

    well_counts: list of dicts with keys:
        well_id, bead_count, cluster_count, dilution_factor, volume_ul

    Returns concentration estimate dict.
    """
    # Mark exclusions
    for wc in well_counts:
        wc["excluded"] = False
        wc["exclusion_reason"] = None

        bc = wc["bead_count"]
        cc = wc["cluster_count"]

        if bc == 0 and cc == 0:
            wc["excluded"] = True
            wc["exclusion_reason"] = "zero beads"
        elif bc > max_reliable_count:
            wc["excluded"] = True
            wc["exclusion_reason"] = f"bead count ({bc}) exceeds max reliable ({max_reliable_count})"
        elif cc > 10 or (bc > 0 and cc / bc > max_cluster_ratio):
            wc["excluded"] = True
            wc["exclusion_reason"] = "high cluster ratio"

    # Per-well stock concentration estimate
    for wc in well_counts:
        if not wc["excluded"] and wc["bead_count"] > 0:
            wc["estimated_stock_conc"] = (
                wc["bead_count"] * wc["dilution_factor"] / wc["volume_ul"]
            )
        else:
            wc["estimated_stock_conc"] = None

    included = [wc for wc in well_counts if not wc["excluded"]]
    estimates = [wc["estimated_stock_conc"] for wc in included if wc["estimated_stock_conc"] is not None]

    if not estimates:
        return {
            "stock_concentration_cells_per_ul": {
                "median_estimate": None,
                "linear_fit_estimate": None,
                "r_squared": None,
            },
            "wells_used": 0,
            "wells_excluded": len(well_counts),
            "unit": "cells/uL",
        }

    # Median estimate
    median_est = float(np.median(estimates))

    # Linear fit through origin: bead_count = (stock_conc / dilution_factor) * volume_ul
    # i.e. bead_count = stock_conc * volume_ul / dilution_factor
    # x = volume_ul / dilution_factor, y = bead_count
    x_vals = np.array([wc["volume_ul"] / wc["dilution_factor"] for wc in included])
    y_vals = np.array([wc["bead_count"] for wc in included])

    # Least squares through origin: slope = sum(x*y) / sum(x*x)
    slope = float(np.sum(x_vals * y_vals) / np.sum(x_vals * x_vals))
    linear_est = slope

    # R-squared
    y_pred = slope * x_vals
    ss_res = np.sum((y_vals - y_pred) ** 2)
    ss_tot = np.sum((y_vals - np.mean(y_vals)) ** 2)
    r_squared = float(1 - ss_res / ss_tot) if ss_tot > 0 else 1.0

    return {
        "stock_concentration_cells_per_ul": {
            "median_estimate": round(median_est, 1),
            "linear_fit_estimate": round(linear_est, 1),
            "r_squared": round(r_squared, 4),
        },
        "wells_used": len(included),
        "wells_excluded": len(well_counts) - len(included),
        "unit": "cells/uL",
    }


def recommend_dilution(
    stock_conc: float,
    target_cells_per_well: float,
    volume_ul: float,
    well_counts: list[dict],
) -> dict:
    """Recommend dilution factor for a target cells-per-well."""
    if stock_conc <= 0:
        return {
            "target_cells_per_well": target_cells_per_well,
            "recommended_dilution_factor": None,
            "closest_existing_well": None,
            "closest_existing_well_count": None,
        }

    # target = stock_conc * volume / dilution => dilution = stock_conc * volume / target
    recommended_df = stock_conc * volume_ul / target_cells_per_well

    # Find the existing well with count closest to target
    counted_wells = [
        wc for wc in well_counts
        if wc["bead_count"] > 0 and not wc.get("excluded", False)
    ]
    closest_well = None
    closest_count = None
    if counted_wells:
        closest = min(counted_wells, key=lambda wc: abs(wc["bead_count"] - target_cells_per_well))
        closest_well = closest["well_id"]
        closest_count = closest["bead_count"]

    return {
        "target_cells_per_well": target_cells_per_well,
        "recommended_dilution_factor": round(recommended_df, 0),
        "closest_existing_well": closest_well,
        "closest_existing_well_count": closest_count,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def results_to_plate_json(
    well_counts: list[dict],
    concentration: dict,
    recommendation: dict,
    plate_id: str = "unknown",
) -> dict:
    """Format concentration results as JSON."""
    return {
        "plate_id": plate_id,
        "analysis_type": "dilution_series_concentration",
        "date": date.today().isoformat(),
        "dilution_series": well_counts,
        "concentration_estimate": concentration,
        "recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Measure concentration from a dilution series of well images."
    )
    parser.add_argument("image_dir", type=Path, help="Directory of well images")
    parser.add_argument("--dilution-config", type=Path, default=None,
                        help="Dilution series config JSON")
    parser.add_argument("--wells", type=str, default=None,
                        help="Shorthand well range (e.g. A1:H1)")
    parser.add_argument("--dilution-ratio", type=float, default=2,
                        help="Fold-dilution between consecutive wells (default: 2)")
    parser.add_argument("--volume", type=float, default=200,
                        help="Volume per well in uL (default: 200)")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output directory (default: annotated_output_<barcode>/)")
    parser.add_argument("--annotate", action="store_true",
                        help="Save annotated images")
    parser.add_argument("--max-cluster-ratio", type=float, default=0.2,
                        help="Max cluster/bead ratio before exclusion (default: 0.2)")
    parser.add_argument("--max-reliable-count", type=int, default=500,
                        help="Max reliable bead count (default: 500)")
    parser.add_argument("--min-area", type=int, default=DEFAULTS["min_area"])
    parser.add_argument("--max-area", type=int, default=DEFAULTS["max_area"])
    parser.add_argument("--threshold", type=int, default=None,
                        help="Manual intensity threshold (default: Otsu)")
    parser.add_argument("--min-circularity", type=float,
                        default=DEFAULTS["min_circularity"])

    args = parser.parse_args()

    if not args.image_dir.is_dir():
        print(f"Error: {args.image_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load or build dilution config
    if args.dilution_config:
        config = load_dilution_config(args.dilution_config)
    elif args.wells:
        barcode = args.image_dir.name
        config = build_dilution_config_from_args(
            args.wells, args.dilution_ratio, args.volume,
            plate_id=barcode,
        )
    else:
        print("Error: must provide either --dilution-config or --wells", file=sys.stderr)
        sys.exit(1)

    plate_id = config.get("plate_id", args.image_dir.name)
    barcode = plate_id
    output_dir = args.output_dir or Path(f"annotated_output_{barcode}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save dilution config into output directory
    config_dest = output_dir / f"dilution_series_input_{barcode}.json"
    config_dest.write_text(json.dumps(config, indent=2))
    print(f"Dilution config saved to {config_dest}")

    # Build well_id -> dilution info lookup
    dilution_map = {
        entry["well_id"]: entry for entry in config["dilution_series"]
    }

    # Load images
    loaded = load_well_images(args.image_dir)
    if not loaded:
        print("No images found.", file=sys.stderr)
        sys.exit(1)

    count_kwargs = {
        "min_area": args.min_area,
        "max_area": args.max_area,
        "intensity_thresh": args.threshold,
        "min_circularity": args.min_circularity,
    }

    # Count beads in dilution series wells
    well_counts = []
    all_results = []
    all_paths = []

    for well_id, image, img_path in loaded:
        result = count_well(image, well_id, **count_kwargs)
        all_results.append(result)
        all_paths.append(img_path)

        if well_id in dilution_map:
            entry = dilution_map[well_id]
            wc = {
                "well_id": well_id,
                "bead_count": result.bead_count,
                "cluster_count": result.cluster_count,
                "dilution_factor": entry["dilution_factor"],
                "volume_ul": entry["volume_ul"],
            }
            well_counts.append(wc)
            print(f"  {well_id}: {result.bead_count} beads, "
                  f"{result.cluster_count} clusters "
                  f"(1:{entry['dilution_factor']})")
        else:
            print(f"  {well_id}: {result.bead_count} beads (not in dilution series)")

    if not well_counts:
        print("Error: no dilution series wells found in images.", file=sys.stderr)
        sys.exit(1)

    # Fit concentration
    concentration = fit_concentration(
        well_counts,
        max_cluster_ratio=args.max_cluster_ratio,
        max_reliable_count=args.max_reliable_count,
    )

    # Recommendation
    median_est = concentration["stock_concentration_cells_per_ul"]["median_estimate"]
    stock_conc = median_est if median_est is not None else 0
    # Use median volume from series
    median_volume = float(np.median([wc["volume_ul"] for wc in well_counts]))
    recommendation = recommend_dilution(stock_conc, 1.0, median_volume, well_counts)

    # Detailed results (full per-well data)
    detailed = {
        "summary": {
            "total_wells_processed": len(all_results),
            "dilution_series_wells": len(well_counts),
        },
        "wells": [asdict(r) for r in all_results],
    }
    detailed_path = output_dir / f"detailed_results_{barcode}.json"
    detailed_path.write_text(json.dumps(detailed, indent=2))
    print(f"\nDetailed results written to {detailed_path}")

    # Concentration results JSON
    plate_json = results_to_plate_json(well_counts, concentration, recommendation, plate_id)
    conc_path = output_dir / f"concentration_results_{barcode}.json"
    conc_path.write_text(json.dumps(plate_json, indent=2))
    print(f"Concentration results written to {conc_path}")

    # Annotated images
    if args.annotate:
        for r, img_path in zip(all_results, all_paths):
            img = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
            if img is not None:
                ann = annotated_bead_image(img, r.well_id, result=r)
                out_name = f"{r.well_id}_{img_path.stem}_annotated.png"
                cv2.imwrite(str(output_dir / out_name), ann)

    # Print summary
    conc = concentration["stock_concentration_cells_per_ul"]
    print(f"\n--- Concentration Estimate ---")
    print(f"Wells used:       {concentration['wells_used']}")
    print(f"Wells excluded:   {concentration['wells_excluded']}")
    if conc["median_estimate"] is not None:
        print(f"Median estimate:  {conc['median_estimate']:.1f} cells/uL")
        print(f"Linear fit:       {conc['linear_fit_estimate']:.1f} cells/uL")
        print(f"R-squared:        {conc['r_squared']:.4f}")
    else:
        print("Could not estimate concentration (all wells excluded)")

    if recommendation["recommended_dilution_factor"]:
        print(f"\n--- Recommendation ---")
        print(f"For {recommendation['target_cells_per_well']} cell/well:")
        print(f"  Dilution factor: 1:{int(recommendation['recommended_dilution_factor'])}")
        if recommendation["closest_existing_well"]:
            print(f"  Closest existing well: {recommendation['closest_existing_well']} "
                  f"({recommendation['closest_existing_well_count']} beads)")


if __name__ == "__main__":
    main()

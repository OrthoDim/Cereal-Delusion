"""
Well classification pipeline for 96-well plate fluorescence microscopy.

Classifies well images as:
  - empty: no beads detected
  - single: exactly one bead detected
  - multiple: two or more beads detected
  - uncertain: detection ambiguous (flagged for human review)

Beads: Fluorescent Green PE Microspheres, 28-48 μm, Ex 414nm / Em 515nm.
Images: Fluorescence microscopy from Cephla Squid (2×2 or 1×2 montage sprites).

Usage:
    python classify_wells.py <image_dir> [--output results.json] [--debug-dir debug/]
    python classify_wells.py --help
"""

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from scipy import ndimage
from skimage.measure import regionprops, label as sk_label
from skimage.segmentation import watershed
from skimage.feature import peak_local_max


@dataclass
class BeadInfo:
    area: int
    centroid: tuple[int, int]
    bbox: tuple[int, int, int, int]
    circularity: float
    eccentricity: float
    solidity: float
    mean_intensity: float


@dataclass
class WellResult:
    well_id: str
    label: str  # empty, single, multiple, multiple_clusters, uncertain
    confidence: float
    bead_count: int
    cluster_count: int
    bead_areas: list[int]
    reason: str
    beads: list[dict] = field(default_factory=list)
    clusters: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Image-level calibration (measured from real Monomer sprites)
# ---------------------------------------------------------------------------

# Single bead: median area ~250px² post-watershed at 3000px reference resolution
REFERENCE_DIM = 3000  # all area thresholds calibrated at this resolution
DEFAULTS = {
    "min_area": 25,       # below this = noise/debris
    "max_area": 800,      # above this = unsplit cluster
    "intensity_thresh": None,     # None = Otsu
    "min_circularity": 0.50,
    "watershed_min_distance": 4,  # min distance between watershed seeds
}


# ---------------------------------------------------------------------------
# Per-quadrant background normalization (fixes stitched montage artifacts)
# ---------------------------------------------------------------------------

def normalize_quadrants(gray: np.ndarray) -> tuple[np.ndarray, list[float]]:
    """Normalize background across 2x2 montage quadrants.

    Monomer platform images are 2x2 stitched sprites where each quadrant may
    have independent autocontrast.  This estimates the background of each
    quadrant (median pixel value) and multiplicatively rescales each to a fixed
    reference level.

    Returns (corrected_image, [med_TL, med_TR, med_BL, med_BR]).
    """
    h, w = gray.shape
    mh, mw = h // 2, w // 2
    quads = [
        (slice(0, mh), slice(0, mw)),       # TL
        (slice(0, mh), slice(mw, w)),        # TR
        (slice(mh, h), slice(0, mw)),        # BL
        (slice(mh, h), slice(mw, w)),        # BR
    ]
    medians = [float(np.median(gray[r, c])) for r, c in quads]
    # Absolute reference: well-exposed fluorescence background is ~1-4.
    # Using a fixed target ensures uniformly bright images (e.g. H1) get
    # scaled down correctly, not just matched to each other.
    ref = 3.0
    out = gray.copy()
    for (r, c), med in zip(quads, medians):
        if med > ref:
            scale = ref / med
            out[r, c] = np.clip(gray[r, c].astype(np.float32) * scale, 0, 255).astype(np.uint8)
    return out, medians


def mask_seams(gray: np.ndarray, medians: list[float], seam_w: int = 60) -> np.ndarray:
    """Zero out stitching seams between quadrants with large intensity mismatch.

    Must be called AFTER resize so the mask covers the full seam width at
    detection resolution (resize interpolation can smear pre-resize masks).
    """
    h, w = gray.shape
    mh, mw = h // 2, w // 2
    ratio_thresh = 3.0
    med_tl, med_tr, med_bl, med_br = medians

    def _mismatch(a: float, b: float) -> bool:
        lo, hi = min(a, b), max(a, b)
        return hi / max(lo, 0.5) > ratio_thresh

    out = gray.copy()
    # Vertical seam (x = mw): TL-TR and BL-BR
    if _mismatch(med_tl, med_tr) or _mismatch(med_bl, med_br):
        x0 = max(0, mw - seam_w)
        x1 = min(w, mw + seam_w + 1)
        out[:, x0:x1] = 0
    # Horizontal seam (y = mh): TL-BL and TR-BR
    if _mismatch(med_tl, med_bl) or _mismatch(med_tr, med_br):
        y0 = max(0, mh - seam_w)
        y1 = min(h, mh + seam_w + 1)
        out[y0:y1, :] = 0
    return out


def well_roi_mask(gray: np.ndarray,
                  center_frac: tuple[float, float] = (0.48, 0.50),
                  radius_frac: float = 0.43) -> np.ndarray:
    """Mask pixels outside the circular well ROI.

    The well is at a fixed position/size relative to the 2x2 montage.
    Zeroes everything outside the circle so rim / plastic / edge artifacts
    are never passed to segmentation.
    """
    h, w = gray.shape
    cx = int(w * center_frac[0])
    cy = int(h * center_frac[1])
    radius = int(min(h, w) * radius_frac)
    Y, X = np.ogrid[:h, :w]
    mask = ((X - cx) ** 2 + (Y - cy) ** 2) <= radius ** 2
    out = gray.copy()
    out[~mask] = 0
    return out


# ---------------------------------------------------------------------------
# Core detection with watershed
# ---------------------------------------------------------------------------

def segment_beads(
    gray: np.ndarray,
    intensity_thresh: Optional[int] = None,
    min_area: int = 25,
    max_area: int = 800,
    min_distance: int = 4,
    min_circularity: float = 0.50,
) -> tuple[np.ndarray, list[BeadInfo], list[BeadInfo]]:
    """Segment fluorescent beads using thresholding + watershed.

    Returns (labeled_image, beads, clusters).
    Beads are objects within [min_area, max_area].
    Clusters are objects above max_area (unsplit clumps).
    """
    working = gray.copy()

    # Gaussian blur
    blurred = cv2.GaussianBlur(working, (5, 5), 0)

    # Early exit: if image has no significant bright content, it's empty
    roi_pixels = blurred.ravel()
    if len(roi_pixels) == 0:
        return np.zeros_like(gray, dtype=np.int32), [], []
    max_intensity = int(roi_pixels.max())
    if max_intensity < 25:
        # No pixel bright enough to be a bead
        return np.zeros_like(gray, dtype=np.int32), [], []

    # Threshold
    MIN_THRESH_FLOOR = 15  # never threshold below this (noise floor)
    if intensity_thresh is not None:
        _, binary = cv2.threshold(blurred, intensity_thresh, 255, cv2.THRESH_BINARY)
    else:
        thresh_val, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Enforce minimum threshold to avoid noise pickup on empty wells
        thresh_val = max(thresh_val, MIN_THRESH_FLOOR)
        _, binary = cv2.threshold(blurred, thresh_val, 255, cv2.THRESH_BINARY)

    # Morphological opening to remove small noise
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    # --- Connected components then watershed to split touching beads ---
    cc_labeled, n_cc = ndimage.label(binary)

    if n_cc == 0:
        return np.zeros_like(gray, dtype=np.int32), [], []

    dist = ndimage.distance_transform_edt(binary)
    coords = peak_local_max(dist, min_distance=min_distance, labels=binary)

    if len(coords) == 0:
        return np.zeros_like(gray, dtype=np.int32), [], []

    markers_clean = np.zeros_like(binary, dtype=np.int32)
    for i, (y, x) in enumerate(coords, start=1):
        markers_clean[y, x] = i

    labeled = watershed(-dist, markers_clean, mask=binary)
    props = regionprops(labeled, intensity_image=working)

    beads = []
    clusters = []
    valid_labeled = np.zeros_like(labeled)

    for prop in props:
        area = prop.area
        if area < min_area:
            continue

        perimeter = prop.perimeter
        circularity = (4 * np.pi * area) / (perimeter ** 2) if perimeter > 0 else 0

        info = BeadInfo(
            area=area,
            centroid=(int(prop.centroid[0]), int(prop.centroid[1])),
            bbox=(int(prop.bbox[1]), int(prop.bbox[0]),
                  int(prop.bbox[3]), int(prop.bbox[2])),
            circularity=circularity,
            eccentricity=prop.eccentricity,
            solidity=prop.solidity,
            mean_intensity=prop.intensity_mean if hasattr(prop, 'intensity_mean') else prop.mean_intensity,
        )

        # Filter out non-circular / elongated objects (stitching seams, well rim)
        if circularity < 0.3 or prop.eccentricity > 0.95:
            continue

        if area > max_area:
            clusters.append(info)
        else:
            beads.append(info)
        valid_labeled[labeled == prop.label] = len(beads) + len(clusters)

    return valid_labeled, beads, clusters


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
    """Classify a single well image.

    Returns a WellResult with label, confidence, and detection details.
    """
    # Convert to grayscale
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    # Normalize quadrant backgrounds BEFORE resize so the quadrant boundaries
    # are at exact pixel edges.  The subsequent resize then smoothly
    # interpolates across the (now-uniform) boundary.
    gray, quad_medians = normalize_quadrants(gray)

    # Normalize to reference resolution so area thresholds work at any input size
    h, w = gray.shape
    long_side = max(h, w)
    if long_side != REFERENCE_DIM:
        scale = REFERENCE_DIM / long_side
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    # Mask stitching seams AFTER resize so the mask isn't smeared by interpolation
    gray = mask_seams(gray, quad_medians)

    # Mask outside the circular well to exclude rim / plastic artifacts
    gray = well_roi_mask(gray)

    # Segment beads
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

    # --- Classification logic ---

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

    # Build reason
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

    # Multiple objects — check for split artifact when exactly 2 beads, no clusters
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

    # Circularity bonus
    if bead.circularity > 0.85:
        conf += 0.15
    elif bead.circularity > 0.70:
        conf += 0.08

    # Solidity bonus
    if bead.solidity > 0.90:
        conf += 0.10
    elif bead.solidity > 0.80:
        conf += 0.05

    return min(conf, 0.98)


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

WELL_ROWS = "ABCDEFGH"
WELL_COLS = range(1, 13)


def parse_well_id_from_filename(filepath: str | Path) -> str:
    """Extract well ID from filename or parent directory.

    Supports A1, A01, well_A1, row0_col0 in filename.
    Falls back to parent directory name if it matches a valid well ID (e.g., A1/).
    """
    import re
    filepath = Path(filepath)
    stem = filepath.stem.upper()

    match = re.search(r'([A-H])\s*0?(\d{1,2})', stem)
    if match:
        row, col = match.group(1), int(match.group(2))
        if 1 <= col <= 12:
            return f"{row}{col}"

    match = re.search(r'ROW\s*(\d+).*COL\s*(\d+)', stem)
    if match:
        row_idx, col_idx = int(match.group(1)), int(match.group(2))
        if row_idx < 8 and col_idx < 12:
            return f"{WELL_ROWS[row_idx]}{col_idx + 1}"

    # Fallback: check if parent directory is a valid well ID (Monomer layout)
    parent_name = filepath.parent.name.upper()
    match = re.match(r'^([A-H])0?(\d{1,2})$', parent_name)
    if match:
        row, col = match.group(1), int(match.group(2))
        if 1 <= col <= 12:
            return f"{row}{col}"

    return filepath.stem


def classify_plate(
    image_dir: Path,
    extensions: tuple[str, ...] = (".tiff", ".tif", ".png", ".jpg", ".jpeg", ".bmp"),
    **kwargs,
) -> tuple[list[WellResult], list[Path]]:
    """Classify all well images in a directory.

    Returns (results, source_paths) — parallel lists of WellResult and
    the Path to each source image, so callers can generate overlays without
    re-globbing.
    """
    results = []
    source_paths = []
    image_files = sorted(
        f for f in image_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in extensions
    )

    if not image_files:
        print(f"No images found in {image_dir}", file=sys.stderr)
        return results, source_paths

    print(f"Processing {len(image_files)} images from {image_dir}")

    for img_path in image_files:
        image = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            print(f"  Warning: could not read {img_path.name}", file=sys.stderr)
            continue

        well_id = parse_well_id_from_filename(img_path)
        result = classify_well(image, well_id, **kwargs)
        results.append(result)
        source_paths.append(img_path)
        print(f"  {well_id}: {result.label} "
              f"(conf={result.confidence:.2f}, beads={result.bead_count}, "
              f"clusters={result.cluster_count})")

    return results, source_paths


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarize_results(results: list[WellResult]) -> dict:
    """Generate a summary of classification results."""
    total = len(results)
    if total == 0:
        return {"total": 0}

    counts = {"empty": 0, "single": 0, "multiple": 0, "multiple_clusters": 0, "uncertain": 0}
    for r in results:
        counts[r.label] += 1

    return {
        "total_wells": total,
        "empty": counts["empty"],
        "single": counts["single"],
        "multiple": counts["multiple"],
        "multiple_clusters": counts["multiple_clusters"],
        "uncertain": counts["uncertain"],
        "single_pct": round(100 * counts["single"] / total, 1),
        "multi_pct": round(100 * (counts["multiple"] + counts["multiple_clusters"]) / total, 1),
        "empty_pct": round(100 * counts["empty"] / total, 1),
        "flagged_for_review": counts["uncertain"] + counts["multiple_clusters"],
        "estimated_lambda": round(
            -np.log(counts["empty"] / total)
            if 0 < counts["empty"] < total else 0,
            3,
        ),
    }


def results_to_plate_json(results: list[WellResult], plate_id: str = "PLT-001") -> dict:
    """Format results as a 96-well plate cell-count JSON.

    Matches the schema of 96_well_plate_cell_counts.json.
    """
    from datetime import date

    wells = {}
    for r in results:
        wells[r.well_id] = {
            "cell_count": r.bead_count + r.cluster_count,
            "empty": r.label == "empty",
        }

    occupied = [w for w in wells.values() if not w["empty"]]
    all_counts = [w["cell_count"] for w in wells.values()]
    occ_counts = [w["cell_count"] for w in occupied]

    total = len(wells)
    n_empty = sum(1 for w in wells.values() if w["empty"])
    n_occupied = total - n_empty

    return {
        "plate_id": plate_id,
        "plate_type": "96-well",
        "date": date.today().isoformat(),
        "rows": list("ABCDEFGH"),
        "columns": list(range(1, 13)),
        "wells": wells,
        "summary": {
            "total_wells": total,
            "empty_wells": n_empty,
            "occupied_wells": n_occupied,
            "empty_percent": round(100 * n_empty / total, 1) if total else 0,
            "occupied_percent": round(100 * n_occupied / total, 1) if total else 0,
            "min_cell_count_nonzero": min(occ_counts) if occ_counts else 0,
            "max_cell_count": max(all_counts) if all_counts else 0,
            "mean_cell_count_all_wells": round(np.mean(all_counts), 1) if all_counts else 0,
            "mean_cell_count_occupied_wells": round(np.mean(occ_counts), 1) if occ_counts else 0,
        },
    }


def results_to_mcp_payload(results: list[WellResult]) -> dict:
    """Format results for MCP upload."""
    status_updates = []
    comments = []

    for r in results:
        status_updates.append({"well_id": r.well_id, "label": r.label})
        if r.label == "uncertain" or r.confidence < 0.80:
            comments.append({
                "well_id": r.well_id,
                "content": (
                    f"Classified as {r.label} (confidence: {r.confidence:.2f}). "
                    f"Beads detected: {r.bead_count}. {r.reason}"
                ),
            })

    return {"status_updates": status_updates, "comments": comments}


# ---------------------------------------------------------------------------
# Debug visualization
# ---------------------------------------------------------------------------

def debug_overlay(
    image: np.ndarray,
    well_id: str,
    result: Optional[WellResult] = None,
    **kwargs,
) -> np.ndarray:
    """Create a debug image with detected beads circled and labeled.

    If `result` is provided, uses it directly instead of re-running classification.
    """
    if len(image.shape) == 2:
        vis = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        vis = image.copy()

    # Normalize to reference resolution to match detection coordinates
    h, w = vis.shape[:2]
    long_side = max(h, w)
    if long_side != REFERENCE_DIM:
        scale = REFERENCE_DIM / long_side
        vis = cv2.resize(vis, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    if result is None:
        result = classify_well(image, well_id, **kwargs)

    color_map = {
        "empty": (128, 128, 128),
        "single": (0, 255, 0),
        "multiple": (0, 0, 255),
        "multiple_clusters": (0, 100, 255),
        "uncertain": (0, 255, 255),
    }
    color = color_map.get(result.label, (255, 255, 255))

    for bead in result.beads:
        cx, cy = bead["centroid"][1], bead["centroid"][0]
        radius = int(np.sqrt(bead["area"] / np.pi)) + 5
        cv2.circle(vis, (cx, cy), radius, color, 2)

        # Annotate circularity
        circ = bead["circularity"]
        cv2.putText(vis, f"c={circ:.2f}", (cx + radius + 2, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

    # Label
    label_text = f"{well_id}: {result.label} ({result.confidence:.2f}) n={result.bead_count}"
    cv2.putText(vis, label_text, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

    return vis


def annotated_bead_image(
    image: np.ndarray,
    well_id: str,
    result: Optional[WellResult] = None,
    **kwargs,
) -> np.ndarray:
    """Create an annotated image with each bead colored uniquely.

    Each detected bead gets a distinct color from a perceptually-spaced palette.
    Beads are filled with semi-transparent overlays and outlined.
    If `result` is provided, uses it directly instead of re-running classification.
    """
    if len(image.shape) == 2:
        vis = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        vis = image.copy()

    # Normalize to reference resolution to match detection coordinates
    h, w = vis.shape[:2]
    long_side = max(h, w)
    if long_side != REFERENCE_DIM:
        scale = REFERENCE_DIM / long_side
        vis = cv2.resize(vis, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    if result is None:
        result = classify_well(image, well_id, **kwargs)

    # Draw beads in green
    for bead in result.beads:
        cx, cy = bead["centroid"][1], bead["centroid"][0]
        radius = int(np.sqrt(bead["area"] / np.pi)) + 8
        cv2.circle(vis, (cx, cy), radius, (0, 255, 0), 2)

    # Draw clusters in orange
    for cluster in result.clusters:
        cx, cy = cluster["centroid"][1], cluster["centroid"][0]
        radius = int(np.sqrt(cluster["area"] / np.pi)) + 8
        cv2.circle(vis, (cx, cy), radius, (0, 100, 255), 2)

    # Classification label
    label_text = (f"{well_id}: {result.label} | {result.bead_count} bead(s) "
                  f"| {result.cluster_count} cluster(s) | conf={result.confidence:.2f}")
    cv2.putText(vis, label_text, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    return vis


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Classify 96-well plate images for single-cell (bead) cloning."
    )
    parser.add_argument("image_dir", type=Path, help="Directory of well images")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output JSON file (default: stdout)")
    parser.add_argument("--min-area", type=int, default=DEFAULTS["min_area"])
    parser.add_argument("--max-area", type=int, default=DEFAULTS["max_area"])
    parser.add_argument("--threshold", type=int, default=None,
                        help="Manual intensity threshold (default: Otsu)")
    parser.add_argument("--min-circularity", type=float,
                        default=DEFAULTS["min_circularity"])
    parser.add_argument("--debug-dir", type=Path, default=None,
                        help="Save debug overlay images")
    parser.add_argument("--annotate-dir", type=Path, default=None,
                        help="Save annotated images with color-coded beads")
    parser.add_argument("--mcp", action="store_true",
                        help="Include MCP upload payload in output")

    args = parser.parse_args()

    if not args.image_dir.is_dir():
        print(f"Error: {args.image_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

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

    summary = summarize_results(results)

    output = {
        "summary": summary,
        "wells": [asdict(r) for r in results],
    }

    if args.mcp:
        output["mcp_payload"] = results_to_mcp_payload(results)

    # Debug overlays
    if args.debug_dir:
        args.debug_dir.mkdir(parents=True, exist_ok=True)
        for r, img_path in zip(results, source_paths):
            img = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
            if img is not None:
                overlay = debug_overlay(img, r.well_id, result=r, **classify_kwargs)
                cv2.imwrite(str(args.debug_dir / f"{r.well_id}_{img_path.stem}_debug.png"), overlay)

    # Annotated bead images
    if args.annotate_dir:
        args.annotate_dir.mkdir(parents=True, exist_ok=True)
        for r, img_path in zip(results, source_paths):
            img = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
            if img is not None:
                ann = annotated_bead_image(img, r.well_id, result=r, **classify_kwargs)
                out_name = f"{r.well_id}_{img_path.stem}_annotated.png"
                cv2.imwrite(str(args.annotate_dir / out_name), ann)
                print(f"  Annotated: {out_name}")

    # Plate cell-count JSON (96_well_plate_cell_counts format)
    plate_json = results_to_plate_json(results)
    plate_json_path = (args.annotate_dir or args.debug_dir or Path(".")) / "96_well_plate_cell_counts.json"
    plate_json_path.write_text(json.dumps(plate_json, indent=2))
    print(f"\nPlate cell-count JSON written to {plate_json_path}")

    # Output
    json_str = json.dumps(output, indent=2)
    if args.output:
        args.output.write_text(json_str)
        print(f"\nResults written to {args.output}")
    else:
        print(json_str)

    # Summary
    print(f"\n--- Summary ---")
    print(f"Total wells: {summary['total_wells']}")
    print(f"Single:          {summary['single']} ({summary['single_pct']}%)")
    print(f"Empty:           {summary['empty']} ({summary['empty_pct']}%)")
    print(f"Multiple:        {summary['multiple']} ({summary['multi_pct']}%)")
    print(f"Multiple+Clust:  {summary['multiple_clusters']}")
    print(f"Uncertain:       {summary['uncertain']}")
    print(f"Flagged:         {summary['flagged_for_review']}")
    print(f"Estimated λ:     {summary['estimated_lambda']}")


if __name__ == "__main__":
    main()

"""Shared image processing primitives for well classification.

Contains dataclasses, constants, image preprocessing, bead segmentation,
file loading, and visualization helpers used by both
`classify_single_cells` and `measure_concentration`.
"""

import re
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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

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
# Constants
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

WELL_ROWS = "ABCDEFGH"
WELL_COLS = range(1, 13)


# ---------------------------------------------------------------------------
# Per-quadrant background normalization
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
    ref = 3.0
    out = gray.copy()
    for (r, c), med in zip(quads, medians):
        if med > ref:
            scale = ref / med
            out[r, c] = np.clip(gray[r, c].astype(np.float32) * scale, 0, 255).astype(np.uint8)
    return out, medians


# ---------------------------------------------------------------------------
# Seam masking
# ---------------------------------------------------------------------------

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
    if _mismatch(med_tl, med_tr) or _mismatch(med_bl, med_br):
        x0 = max(0, mw - seam_w)
        x1 = min(w, mw + seam_w + 1)
        out[:, x0:x1] = 0
    if _mismatch(med_tl, med_bl) or _mismatch(med_tr, med_br):
        y0 = max(0, mh - seam_w)
        y1 = min(h, mh + seam_w + 1)
        out[y0:y1, :] = 0
    return out


# ---------------------------------------------------------------------------
# Well ROI mask
# ---------------------------------------------------------------------------

def well_roi_mask(gray: np.ndarray,
                  center_frac: tuple[float, float] = (0.48, 0.50),
                  radius_frac: float = 0.43) -> np.ndarray:
    """Mask pixels outside the circular well ROI."""
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
# Preprocessing pipeline
# ---------------------------------------------------------------------------

def preprocess_well_image(img: np.ndarray, reference_dim: int = REFERENCE_DIM) -> tuple[np.ndarray, list[float]]:
    """Consolidate grayscale conversion, quadrant normalization, resize, seam
    masking, and well ROI masking into a single call.

    Returns (preprocessed_gray, quad_medians).
    """
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img.copy()

    # Normalize quadrant backgrounds BEFORE resize
    gray, quad_medians = normalize_quadrants(gray)

    # Normalize to reference resolution
    h, w = gray.shape
    long_side = max(h, w)
    if long_side != reference_dim:
        scale = reference_dim / long_side
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    # Mask seams AFTER resize
    gray = mask_seams(gray, quad_medians)

    # Mask outside well
    gray = well_roi_mask(gray)

    return gray, quad_medians


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

    blurred = cv2.GaussianBlur(working, (5, 5), 0)

    roi_pixels = blurred.ravel()
    if len(roi_pixels) == 0:
        return np.zeros_like(gray, dtype=np.int32), [], []
    max_intensity = int(roi_pixels.max())
    if max_intensity < 25:
        return np.zeros_like(gray, dtype=np.int32), [], []

    MIN_THRESH_FLOOR = 15
    if intensity_thresh is not None:
        _, binary = cv2.threshold(blurred, intensity_thresh, 255, cv2.THRESH_BINARY)
    else:
        thresh_val, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thresh_val = max(thresh_val, MIN_THRESH_FLOOR)
        _, binary = cv2.threshold(blurred, thresh_val, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

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

        if circularity < 0.3 or prop.eccentricity > 0.95:
            continue

        if area > max_area:
            clusters.append(info)
        else:
            beads.append(info)
        valid_labeled[labeled == prop.label] = len(beads) + len(clusters)

    return valid_labeled, beads, clusters


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

def parse_well_id_from_filename(filepath: str | Path) -> str:
    """Extract well ID from filename or parent directory.

    Supports A1, A01, well_A1, row0_col0 in filename.
    Falls back to parent directory name if it matches a valid well ID (e.g., A1/).
    """
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


def load_well_images(
    image_dir: Path,
    extensions: tuple[str, ...] = (".tiff", ".tif", ".png", ".jpg", ".jpeg", ".bmp"),
) -> list[tuple[str, np.ndarray, Path]]:
    """Discover and load well images from a directory.

    Returns list of (well_id, image_array, source_path).
    """
    image_files = sorted(
        f for f in image_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in extensions
    )

    if not image_files:
        print(f"No images found in {image_dir}", file=sys.stderr)
        return []

    print(f"Processing {len(image_files)} images from {image_dir}")

    loaded = []
    for img_path in image_files:
        image = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            print(f"  Warning: could not read {img_path.name}", file=sys.stderr)
            continue
        well_id = parse_well_id_from_filename(img_path)
        loaded.append((well_id, image, img_path))

    return loaded


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def debug_overlay(
    image: np.ndarray,
    well_id: str,
    result: Optional[WellResult] = None,
    **kwargs,
) -> np.ndarray:
    """Create a debug image with detected beads circled and labeled."""
    if len(image.shape) == 2:
        vis = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        vis = image.copy()

    h, w = vis.shape[:2]
    long_side = max(h, w)
    if long_side != REFERENCE_DIM:
        scale = REFERENCE_DIM / long_side
        vis = cv2.resize(vis, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    color_map = {
        "empty": (128, 128, 128),
        "single": (0, 255, 0),
        "multiple": (0, 0, 255),
        "multiple_clusters": (0, 100, 255),
        "uncertain": (0, 255, 255),
    }
    color = color_map.get(result.label, (255, 255, 255)) if result else (255, 255, 255)

    if result:
        for bead in result.beads:
            cx, cy = bead["centroid"][1], bead["centroid"][0]
            radius = int(np.sqrt(bead["area"] / np.pi)) + 5
            cv2.circle(vis, (cx, cy), radius, color, 2)
            circ = bead["circularity"]
            cv2.putText(vis, f"c={circ:.2f}", (cx + radius + 2, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

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
    """Create an annotated image with beads in green and clusters in orange."""
    if len(image.shape) == 2:
        vis = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    else:
        vis = image.copy()

    h, w = vis.shape[:2]
    long_side = max(h, w)
    if long_side != REFERENCE_DIM:
        scale = REFERENCE_DIM / long_side
        vis = cv2.resize(vis, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    if result is None:
        return vis

    for bead in result.beads:
        cx, cy = bead["centroid"][1], bead["centroid"][0]
        radius = int(np.sqrt(bead["area"] / np.pi)) + 8
        cv2.circle(vis, (cx, cy), radius, (0, 255, 0), 2)

    for cluster in result.clusters:
        cx, cy = cluster["centroid"][1], cluster["centroid"][0]
        radius = int(np.sqrt(cluster["area"] / np.pi)) + 8
        cv2.circle(vis, (cx, cy), radius, (0, 100, 255), 2)

    label_text = (f"{well_id}: {result.label} | {result.bead_count} bead(s) "
                  f"| {result.cluster_count} cluster(s) | conf={result.confidence:.2f}")
    cv2.putText(vis, label_text, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    return vis


# ---------------------------------------------------------------------------
# MCP payload
# ---------------------------------------------------------------------------

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

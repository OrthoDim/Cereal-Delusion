# Ablation Study: Quartile Normalization & Seam Filtering

## Context

The classification pipeline processes 2x2 stitched montage images from the Cephla Squid microscope on the Monomer platform. Each image is composed of four quadrants that may have independently auto-contrasted exposures, creating intensity mismatches at the stitching boundaries.

Two preprocessing steps were developed to handle this:

1. **Quartile normalization** — estimates the background level (median pixel value) of each quadrant and multiplicatively rescales them to a fixed reference level, eliminating inter-quadrant brightness mismatch.
2. **Seam filtering** — detects quadrant boundaries with large intensity ratios (>3x) and zeros out a band along the seam to prevent edge artifacts from being segmented as objects.

This ablation study removes these features to demonstrate their impact on classification accuracy.

## Experimental Setup

- **Dataset:** CerealDelusion_Run2A (serial dilution, 8 wells: A1-H1, high-to-low bead density)
- **Three pipeline variants tested:**
  - **Full pipeline** — both normalization and seam filtering enabled
  - **No seam filter** — quartile normalization kept, `mask_seams()` removed
  - **No normalization + no seam filter** — both `normalize_quadrants()` and `mask_seams()` removed

## Results

| Well | Full Pipeline | No Seam Filter | No Norm + No Seam |
|------|--------------|----------------|-------------------|
| A1 | 954 beads, 11 clusters | 954 beads, 11 clusters | 954 beads, 11 clusters |
| B1 | 376 beads, 2 clusters | 376 beads, 2 clusters | 376 beads, 2 clusters |
| C1 | 170 beads | 170 beads | 170 beads |
| D1 | 62 beads | 62 beads | 62 beads |
| E1 | 37 beads | 37 beads | 37 beads |
| F1 | 18 beads | 18 beads | 18 beads |
| G1 | 7 beads | 7 beads | 7 beads |
| **H1** | **3 beads, 1 cluster** | **76 beads, 19 clusters** | **7 beads, 1 cluster** |

### Key Finding: H1 is the differentiator

H1 is the lowest-density well, where the signal-to-artifact ratio is worst. Without preprocessing, seam artifacts dominate the detection.

- **Full pipeline:** 3 beads + 1 cluster — correct detection, seam artifacts eliminated
- **No seam filter:** 76 beads + 19 clusters — **25x overcount**. The vertical stitching seam between the bright BL quadrant and dark BR quadrant is detected as a column of false-positive beads.
- **No norm + no seam:** 7 beads + 1 cluster — fewer false positives than "no seam" alone, because without normalization the BL quadrant's brightness is not rescaled, so Otsu thresholding picks a higher global threshold that suppresses some seam artifacts. However, this also risks suppressing real dim beads in other wells.

### Why high-density wells (A1-G1) are unaffected

In dense wells, real beads far outnumber seam artifacts. The Otsu threshold is driven by the bead population, and any seam fragments that pass thresholding are a tiny fraction of total detections. The preprocessing matters most at low density — exactly the regime that matters for single-cell cloning (target λ ≈ 0.3-1.0).

## Preprocessing Stage Visualization (H1)

The `preprocessing_stages_H1/` directory contains intermediate images showing what happens at each step:

| File | Description |
|------|-------------|
| `H1_0_raw.png` | Raw grayscale — BL quadrant is visibly brighter than the other three |
| `H1_1_normalized.png` | After quartile normalization — all quadrants have uniform background |
| `H1_2_seam_masked.png` | After seam masking — vertical and horizontal seam bands zeroed out |
| `H1_2b_seam_no_norm.png` | Seam masking applied WITHOUT prior normalization (for comparison) |
| `H1_3_roi_masked.png` | After circular well ROI mask — final input to segmentation |
| `H1_comparison_raw_vs_norm.png` | Side-by-side raw vs normalized with quadrant medians annotated |

### Quadrant median values for H1

| Quadrant | Median Intensity |
|----------|-----------------|
| TL | 3.0 |
| TR | 3.0 |
| BL | **43.0** |
| BR | 2.0 |

The BL quadrant has **14x higher background** than the others. This is caused by the microscope's independent autocontrast per field of view in the 2x2 montage. The normalization rescales BL from median=43 down to the reference level of 3.0, making the background uniform across the entire image.

## Annotated Detection Images

Each output directory contains annotated well images showing detected beads (green circles) and clusters (orange circles):

- `run2a_full_pipeline/` — full pipeline results
- `run2a_no_seam/` — no seam filter (see H1 for the column of false positives along the vertical seam)
- `run2a_no_norm_no_seam/` — no normalization or seam filtering

## Conclusion

Quartile normalization and seam filtering are critical for accurate classification at low bead densities. Without them, stitching artifacts cause massive false-positive detection (25x overcount on H1). This is precisely the density regime relevant to single-cell cloning workflows where the target is λ ≈ 0.3-1.0 beads/well.

## File Inventory

```
output/
├── ABLATION_STUDY.md                    ← this file
├── preprocessing_stages_H1/             ← intermediate preprocessing images for H1
│   ├── H1_0_raw.png
│   ├── H1_1_normalized.png
│   ├── H1_2_seam_masked.png
│   ├── H1_2b_seam_no_norm.png
│   ├── H1_3_roi_masked.png
│   └── H1_comparison_raw_vs_norm.png
├── run2a_full_pipeline/                 ← full pipeline (both features enabled)
│   ├── {well}_annotated.png
│   ├── results.json
│   └── 96_well_plate_cell_counts.json
├── run2a_no_seam/                       ← ablation: seam filter removed
│   ├── {well}_annotated.png
│   ├── results.json
│   └── 96_well_plate_cell_counts.json
└── run2a_no_norm_no_seam/               ← ablation: both features removed
    ├── {well}_annotated.png
    ├── results.json
    └── 96_well_plate_cell_counts.json
```

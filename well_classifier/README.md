# Well Classifier

Image analysis tools for 96-well plate fluorescence microscopy. Detects fluorescent beads (28-48 um PE microspheres, Ex 414nm / Em 515nm) in stitched montage images from the Cephla Squid microscope via Monomer Cloud.

## Workflows

### Concentration Measurement

Determine the right dilution for single-cell seeding by counting beads across a dilution series.

```bash
# 1. Download latest images from Monomer Cloud
python -m data_pipeline.fetch_images

# 2. Run concentration analysis
python -m well_classifier.measure_concentration monomer_images/<barcode> \
    --dilution-config well_classifier/templates/dilution_series_input.json \
    --output-dir annotated_output/<barcode> \
    --annotate
```

**Output:** `concentration_results_<barcode>.json` with per-well bead counts, stock concentration estimate (median + linear fit), R-squared, and recommended dilution factor for 1 cell/well.

**Next step:** Use the recommended dilution to seed a single-cell plate.

### Single-Cell Analysis

After seeding, classify each well and push results to Monomer Cloud.

```bash
# 1. Download latest images
python -m data_pipeline.fetch_images

# 2. Classify + annotate + upload to Monomer Cloud (one command)
python -m well_classifier.classify_single_cells monomer_images/<barcode> \
    --annotate --upload
```

This:
1. Classifies every well as `empty`, `single`, `multiple`, `multiple_clusters`, or `uncertain`
2. Writes `detailed_results_<barcode>.json` and `single_cell_results_<barcode>.json`
3. Saves annotated images (green/yellow/orange bead circles)
4. Uploads to Monomer Cloud:
   - Sets each culture's status (`Empty`, `Single Bead`, `Multiple Beads`, `Uncertain`)
   - Adds comments on uncertain/low-confidence wells explaining what was detected
   - Adds a plate-level summary comment with counts and estimated lambda

To re-upload without re-running classification:
```bash
python -m data_pipeline.upload_results annotated_output/<barcode>/detailed_results_<barcode>.json
```

## Tool Reference

### 1. Single-Cell Detection (`classify_single_cells`)

Classifies each well as `empty`, `single`, `multiple`, `multiple_clusters`, or `uncertain`. Use this after seeding to assess clonality.

```bash
python -m well_classifier.classify_single_cells monomer_images/<barcode> \
    --output-dir annotated_output/<barcode> \
    --annotate [--upload] [--debug]
```

**Outputs** (all in `--output-dir`):
- `single_cell_results_<barcode>.json` — per-well labels, confidence, and plate summary with Poisson lambda estimate
- `detailed_results_<barcode>.json` — full per-well bead/cluster geometry data
- `*_annotated.png` — images with beads circled (green = confident, yellow = faint/uncertain, orange = cluster)
- `*_debug.png` — debug overlays with circularity and per-bead confidence labels (with `--debug`)

### 2. Concentration Measurement (`measure_concentration`)

Counts beads across a dilution series and estimates stock concentration. Use this to determine the right dilution for single-cell seeding.

```bash
python -m well_classifier.measure_concentration monomer_images/<barcode> \
    --dilution-config well_classifier/templates/dilution_series_input.json \
    --output-dir annotated_output/<barcode> \
    --annotate
```

Or use CLI shorthand for a simple serial dilution:

```bash
python -m well_classifier.measure_concentration monomer_images/<barcode> \
    --wells A1:H1 --dilution-ratio 2 --volume 100
```

**Outputs** (all in `--output-dir`):
- `concentration_results_<barcode>.json` — per-well counts, exclusion flags, median + linear-fit concentration estimates, R-squared, and recommended dilution factor for 1 cell/well
- `detailed_results_<barcode>.json` — full per-well bead/cluster geometry data

### 3. Result Upload (`upload_results`)

Uploads classification results to Monomer Cloud without re-running detection.

```bash
python -m data_pipeline.upload_results annotated_output/<barcode>/detailed_results_<barcode>.json \
    [--plate-barcode <override>]
```

### Dilution config format

See `templates/dilution_series_input.json`. The `dilution_factor` is **cumulative** relative to stock (not per-step). For a 1:2 serial dilution starting at neat stock: 1, 2, 4, 8, 16, 32, 64, 128.

```json
{
  "plate_id": "CerealDelusion_Run1_A",
  "stock_label": "bead stock",
  "dilution_series": [
    {"well_id": "A1", "dilution_factor": 1,  "volume_ul": 100},
    {"well_id": "B1", "dilution_factor": 2,  "volume_ul": 100},
    {"well_id": "C1", "dilution_factor": 4,  "volume_ul": 100}
  ]
}
```

## How detection works

1. **Grayscale + quadrant normalization** — Monomer images are 2x2 stitched sprites where each quadrant may have different autocontrast. Each quadrant's background is multiplicatively rescaled to a fixed reference level.

2. **Resize to reference resolution** — All area thresholds are calibrated at 3000px (long side). Images are rescaled so detection parameters work at any input size.

3. **Per-half seam masking** — Stitching boundaries between quadrants with extreme intensity mismatch (>5x ratio) are zeroed out to prevent false detections. Each half of each seam is evaluated independently — a TL/TR mismatch only masks the top half of the vertical seam, leaving the bottom half (BL/BR) intact if its ratio is low. This prevents destroying real objects near seams that don't need filtering.

4. **Well ROI masking** — A circular mask removes rim, plastic, and edge artifacts outside the well area.

5. **Otsu thresholding + morphological opening** — Segments bright objects from background. A minimum threshold floor (15) prevents noise pickup on empty wells.

6. **Watershed segmentation** — Distance-transform + peak-local-max seeds split touching beads that threshold alone would merge.

7. **Object filtering** — Each segmented region is filtered by:
   - Area: `[min_area, max_area]` — objects below min are noise, above max are unsplit clusters
   - Circularity: rejects elongated artifacts (seam remnants, well rim fragments)
   - Eccentricity: rejects highly eccentric shapes (>0.95)

8. **Merged-bead detection** — For single-bead wells, an intensity profile check (`peak_local_max` within the bead mask) looks for multiple intensity peaks. Two or more peaks suggest merged beads that passed watershed as one object; confidence is reduced and the well may be reclassified as `uncertain`.

9. **Eccentricity penalty** — Single-bead confidence is penalized for elongated shapes (eccentricity >0.60: -0.07, >0.80: -0.15). Two merged beads form an ellipse with eccentricity ~0.7+; single round beads rarely exceed 0.5.

10. **Sensitive resegment pass** — After primary classification, a second segmentation runs at 0.5x the Otsu threshold (floor of 15) with relaxed area/circularity params. Objects found by the sensitive pass but not near any primary detection are recorded as faint candidates:
    - `empty` wells with faint objects → reclassified as `uncertain` (conf 0.45)
    - `single` wells with >1 faint objects → reclassified as `uncertain` (conf 0.50)
    - `multiple` wells — faint objects are recorded for annotation but don't change the label

11. **Plate-level area anomaly scoring** — After all wells are classified, `single` wells whose bead area exceeds 1.5x the median single-bead area are penalized (conf -0.15) and may be reclassified as `uncertain`. Requires at least 3 single wells to compute a reliable median.

12. **Classification** (single-cell mode only):
    - 0 objects + low background = `empty`
    - 1 bead = `single` (confidence based on circularity, solidity, eccentricity, and intensity profile)
    - 2 very close beads = `uncertain` (possible watershed split artifact)
    - 2+ beads = `multiple`
    - Any clusters present = `multiple_clusters`

13. **Concentration fitting** (concentration mode only):
   - Wells with >500 beads, 0 beads, or high cluster ratios are excluded
   - Per-well estimate: `stock_conc = bead_count * dilution_factor / volume_ul`
   - Median and least-squares-through-origin fit reported with R-squared

## Annotation colors

Annotated images use per-bead confidence scoring to color-code detections:

| Color | Meaning |
|-------|---------|
| **Green** | Confident bead (per-bead confidence >= 70%) |
| **Yellow** | Uncertain bead — low circularity, high eccentricity, or dim intensity |
| **Yellow + "faint"** | Object found only by the sensitive resegment pass, not the primary threshold |
| **Orange** | Unsplit cluster (area above max_area) |

Each bead is labeled with its confidence percentage. The header shows well classification, bead/cluster counts, faint object count, and overall well confidence.

## File structure

```
well_classifier/
    core.py                    # Shared: dataclasses, preprocessing, segmentation, visualization
    classify_single_cells.py   # Tool 1: single-cell detection (--upload for Monomer Cloud)
    measure_concentration.py   # Tool 2: dilution series concentration
    classify_wells.py          # Deprecated wrapper (delegates to classify_single_cells)
    download_plates.py         # Download plate images
    requirements.txt           # Dependencies (opencv, scipy, scikit-image)
    templates/
        dilution_series_input.json     # Input template for concentration tool
        concentration_results.json     # Example output from concentration tool
        single_cell_results.json       # Example output from single-cell tool

data_pipeline/
    monomer_client.py          # Shared Monomer Cloud MCP client (OAuth + JSON-RPC)
    fetch_images.py            # Download plate images from Monomer Cloud
    upload_results.py          # Upload classification results to Monomer Cloud
```

## Dependencies

```
opencv-python-headless
numpy
scipy
scikit-image
```

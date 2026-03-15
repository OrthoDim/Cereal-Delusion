# Well Classifier

Image analysis tools for 96-well plate fluorescence microscopy. Detects fluorescent beads (28-48 um PE microspheres, Ex 414nm / Em 515nm) in stitched montage images from the Cephla Squid microscope via Monomer Cloud.

## Tools

### 1. Single-Cell Detection (`classify_single_cells`)

Classifies each well as `empty`, `single`, `multiple`, `multiple_clusters`, or `uncertain`. Use this after seeding to assess clonality.

```bash
python -m well_classifier.classify_single_cells monomer_images/<barcode> \
    --output-dir annotated_output_<barcode> \
    --annotate
```

**Outputs** (all in `--output-dir`):
- `single_cell_results_<barcode>.json` — per-well labels, confidence, and plate summary with Poisson lambda estimate
- `detailed_results_<barcode>.json` — full per-well bead/cluster geometry data
- `*_annotated.png` — images with beads circled (green = bead, orange = cluster)

### 2. Concentration Measurement (`measure_concentration`)

Counts beads across a dilution series and estimates stock concentration. Use this to determine the right dilution for single-cell seeding.

```bash
python -m well_classifier.measure_concentration monomer_images/<barcode> \
    --dilution-config well_classifier/templates/dilution_series_input.json \
    --output-dir annotated_output_<barcode> \
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

3. **Seam masking** — Stitching boundaries between quadrants with large intensity mismatch are zeroed out to prevent false detections at seam edges.

4. **Well ROI masking** — A circular mask removes rim, plastic, and edge artifacts outside the well area.

5. **Otsu thresholding + morphological opening** — Segments bright objects from background. A minimum threshold floor (15) prevents noise pickup on empty wells.

6. **Watershed segmentation** — Distance-transform + peak-local-max seeds split touching beads that threshold alone would merge.

7. **Object filtering** — Each segmented region is filtered by:
   - Area: `[min_area, max_area]` — objects below min are noise, above max are unsplit clusters
   - Circularity: rejects elongated artifacts (seam remnants, well rim fragments)
   - Eccentricity: rejects highly eccentric shapes (>0.95)

8. **Classification** (single-cell mode only):
   - 0 objects + low background = `empty`
   - 1 bead = `single` (confidence based on circularity + solidity)
   - 2 very close beads = `uncertain` (possible watershed split artifact)
   - 2+ beads = `multiple`
   - Any clusters present = `multiple_clusters`

9. **Concentration fitting** (concentration mode only):
   - Wells with >500 beads, 0 beads, or high cluster ratios are excluded
   - Per-well estimate: `stock_conc = bead_count * dilution_factor / volume_ul`
   - Median and least-squares-through-origin fit reported with R-squared

## File structure

```
well_classifier/
    core.py                    # Shared: dataclasses, preprocessing, segmentation, visualization
    classify_single_cells.py   # Tool 1: single-cell detection
    measure_concentration.py   # Tool 2: dilution series concentration
    classify_wells.py          # Deprecated wrapper (delegates to classify_single_cells)
    mcp_upload.py              # Upload results to Monomer Cloud
    download_plates.py         # Download plate images
    requirements.txt           # Dependencies (opencv, scipy, scikit-image)
    templates/
        dilution_series_input.json     # Input template for concentration tool
        concentration_results.json     # Example output from concentration tool
        single_cell_results.json       # Example output from single-cell tool
```

## Dependencies

```
opencv-python-headless
numpy
scipy
scikit-image
```

---
name: download-plates
description: Browse and download plate images from the Monomer Cloud MCP. Use when the user wants to list available plates, download well images, or fetch new imaging data from Monomer.
user_invocable: true
allowed-tools: Bash, Read, Write, Glob, Grep, mcp__monomer-cloud__list_plates, mcp__monomer-cloud__list_cultures, mcp__monomer-cloud__get_observation_image_access, mcp__monomer-cloud__get_plate_details, mcp__monomer-cloud__get_plate_observations, AskUserQuestion
---

# Download Plate Images from Monomer Cloud

Interactive tool to browse available plates and download stitched well images.

## Step 1: List available plates

Call `mcp__monomer-cloud__list_plates` with no filters.

Present the results as a **numbered table**, sorted by most recently observed first:

```
 #  Barcode                   Wells  Datasets  Last Observed
──  ────────────────────────  ─────  ────────  ──────────────
 1  CerealDelusion_Run1_A       52        6   Mar 15 01:45
 2  ET_FL_Run_2                 48        1   Mar 15 01:49
...
```

- Skip plates with 0 active cultures or 0 datasets (no images to download).
- Show `dataset_count` as "Datasets" — this tells the user how many timepoints exist.

Then ask: **"Which plate(s) would you like to download? Enter number(s), barcode, or 'all'."**

Wait for the user's response before continuing.

## Step 2: Get cultures for the selected plate

Call `mcp__monomer-cloud__list_cultures` with the selected `plate_id` and `limit: 500`.

Report how many cultures were found.

If the plate has multiple datasets (timepoints), ask the user if they want the latest or a specific one. Call `mcp__monomer-cloud__get_plate_observations` to show available timepoints if needed.

## Step 3: Download images

Create the output directory:
```bash
mkdir -p "monomer_images/<plate_barcode>"
```

For each culture:
1. Call `mcp__monomer-cloud__get_observation_image_access` with the `culture_id` (and `dataset_id` if a specific timepoint was chosen).
2. Extract the presigned URL — prefer the **large sprite** URL if available, otherwise standard.
3. Download using curl. Batch multiple downloads in parallel for speed:
   ```bash
   curl -sS -o "monomer_images/<barcode>/A1.jpg" "<url1>" &
   curl -sS -o "monomer_images/<barcode>/A2.jpg" "<url2>" &
   # ... etc, ~10 at a time
   wait
   ```
4. Name files by well position from the culture's `well` field (e.g., `A1.jpg`, `B3.jpg`).

**Parallelism**: Process `get_observation_image_access` calls in batches. Use parallel curl downloads (~10 concurrent).

## Step 4: Summary

After all downloads complete, report:
- Total images downloaded vs total cultures
- Output directory path
- Any skipped/failed wells
- File sizes (run `ls -lh` on the directory)

Then ask: **"Would you like to download another plate, or run analysis on these images?"**

If the user wants to analyze, proceed to Step 5.

## Step 5: Run analysis

Ask the user: **"What type of analysis do you want to run?"**

1. **Single-cell detection** — classifies each well as empty/single/multiple/multiple_clusters/uncertain
2. **Concentration measurement** — counts beads across a dilution series to estimate stock concentration

### Option 1: Single-cell detection

```bash
python -m well_classifier.classify_single_cells monomer_images/<barcode> --output-dir annotated_output_<barcode> --annotate
```

Outputs `single_cell_results_<barcode>.json` and `detailed_results_<barcode>.json` into the output directory.

### Option 2: Concentration measurement

Ask the user: **"Do you have a dilution series config JSON, or should we build one? I need to know:"**
- Which wells are in the dilution series (e.g., A1 through H1)
- The cumulative dilution factor for each well (relative to stock)
- The volume per well in uL

If the user provides a JSON file path, use it directly:
```bash
python -m well_classifier.measure_concentration monomer_images/<barcode> \
    --dilution-config <path_to_config.json> \
    --output-dir annotated_output_<barcode> --annotate
```

If the user describes their setup (e.g., "1:2 serial dilution, A1 through H1, 100uL per well"), use the `--wells` shorthand or create a config JSON (it will be automatically saved into the output directory). Format:
```json
{
  "plate_id": "<barcode>",
  "stock_label": "bead stock",
  "dilution_series": [
    {"well_id": "A1", "dilution_factor": 1,   "volume_ul": 100},
    {"well_id": "B1", "dilution_factor": 2,   "volume_ul": 100}
  ]
}
```
- `dilution_factor` is **cumulative** relative to stock (not per-step)
- For a 1:2 serial dilution starting at neat: 1, 2, 4, 8, 16, 32, 64, 128

Then run:
```bash
python -m well_classifier.measure_concentration monomer_images/<barcode> \
    --dilution-config well_classifier/templates/dilution_series_input_<barcode>.json \
    --output-dir annotated_output_<barcode> --annotate
```

Outputs `concentration_results_<barcode>.json` and `detailed_results_<barcode>.json` into the output directory.

## Notes
- If downloading multiple plates, process them one at a time.
- If a culture has no observation/image, skip it and note it in the summary.
- Always use the latest observation unless the user specifies a timepoint.
- The presigned URLs expire — download promptly after fetching them.

# Liquid Handler Protocol — Track B Single-Cell Cloning
**OT-2 + PyLabRobot | P300 left (channel 0) | 200µL filter tips | Min transfer: 20µL**

---

## Deck Layout

| Slot | Labware | Contents |
|------|---------|----------|
| 1 | 24-well deepwell (10 mL) | **A1** = bead/cell stock, **B1** = buffer (Phase 1), **C1** = buffer (Phase 2), **D1** = working dilution (built in Phase 2) |
| 2 | *(unused)* | — |
| 3 | 96-well flat bottom (360 µL) | **Col 1 (A1–H1)** = serial dilution for imaging, **Cols 2–12** = seeded wells |
| 6 | 200 µL filter tip rack | P300, left mount, channel 0 — serial dilution + seeding |
| 9 | 1000 µL filter tip rack | P1000, right mount, channel 1 — buffer pre-fill (single tip, repeat dispense) |

---

## Phase 1: Serial Dilution Down Column 1 (~5 min)

**Goal:** Create a 1:10 serial dilution down column 1 of the flat bottom plate (A1→H1) to estimate stock concentration.

| Step | Action | Volume | From → To | Notes |
|------|--------|--------|-----------|-------|
| 1 | Add buffer to wells A1–H1 | 100 µL each | 24-well B1 → 96wp col 1 | **P1000 single tip, repeat dispense** — one tip for all 8 wells |
| 2 | Add stock → A1, mix 5× | 100 µL | 24-well A1 → 96wp A1 | **P300 single tip** — well A1 = **1:2** (50% stock) |
| 3 | Transfer A1 → B1, mix 5× | 100 µL | 96wp A1 → B1 | well B1 = **1:4** (25% stock) |
| 4 | Transfer B1 → C1, mix 5× | 100 µL | 96wp B1 → C1 | well C1 = **1:8** (12.5% stock) |
| 5 | Transfer C1 → D1, mix 5× | 100 µL | 96wp C1 → D1 | well D1 = **1:16** (6.25% stock) |
| 6 | Transfer D1 → E1, mix 5× | 100 µL | 96wp D1 → E1 | well E1 = **1:32** (3.1% stock) |
| 7 | Transfer E1 → F1, mix 5× | 100 µL | 96wp E1 → F1 | well F1 = **1:64** (1.6% stock) |
| 8 | Transfer F1 → G1, mix 5× | 100 µL | 96wp F1 → G1 | well G1 = **1:128** (0.8% stock) |
| 9 | Transfer G1 → H1, mix 5× | 100 µL | 96wp G1 → H1 | well H1 = **1:256** (0.4% stock) |
| 10 | Discard tip | — | — | **Single tip used for all 8 serial steps** |

🔬 **Hand flat bottom plate to Squid. Image column 1 (A1–H1). AI estimates bead count per well. Identify which well has ~1 bead.**

---

## Phase 2: Make Working Dilution in 24-well D1 (~2 min)

**Goal:** Recreate the identified concentration at scale in 24-well deepwell D1.

| Step | Action | Notes |
|------|--------|-------|
| 11 | Identify which col 1 well had ~1 bead from imaging | e.g. 96wp D1 = 1:16 |
| 12 | Add buffer to 24-well D1 via OT-2 (180 µL trips from C1) | ~2.5–5 mL total |
| 13 | Transfer 20 µL from chosen 96wp well → 24-well D1, mix 10× | — |
| 14 | Discard tips | — |

---

## Phase 2.5: Make Working Dilution in C1 + Aliquot (~5 min)

**Goal:** Use imaging result to calculate exact dilution needed for 1 cell per 80µL well. Build that dilution in 24-well C1, then aliquot into 8 wells of the 96wp.

| Step | Action | Volume | Notes |
|------|--------|--------|-------|
| 15 | Calculate stock transfer volume | X µL | From JSON: `STOCK_CONC` cells/µL in A1 → target = 1 cell/80µL |
| 16 | P300: mix A1 5×, transfer X µL from A1 → C1 | X µL (min 20µL) | Fresh tip |
| 17 | P300: add buffer from B1 → C1 (180µL trips) | ~1100 µL total | Multiple trips if needed |
| 18 | P1000: mix C1 5× at 800µL | 800 µL | Breaks up clumps |
| 19 | P300 single tip: aspirate 80µL from C1 → each of 8 wells (A1–H1) | 80 µL/well | Mix C1 every 8 wells |
| 20 | Discard tip | — | — |

```bash
# Read directly from classifier JSON (recommended):
JSON=annotated_output_*/concentration_results_*.json \
OT2_HOST=192.168.68.101 python aliquot_wells.py

# Manual override:
STOCK_CONC=4.0 OT2_HOST=192.168.68.101 python aliquot_wells.py

# Dry run to verify math first:
DRY_RUN=true JSON=annotated_output_*/concentration_results_*.json python aliquot_wells.py
```

🔬 **Hand plate to Squid. Image col 1 (A1–H1). AI classifies: empty / single / multiple / uncertain.**

---

## Phase 3: Seed Remaining 88 Wells (Cols 2–12) (~5 min)

**Goal:** Dispense working dilution from 24-well B1 into all remaining wells of the flat bottom plate.

| Step | Action | Volume | Notes |
|------|--------|--------|-------|
| 15 | For each column 2–12 (11 cols × 8 rows = 88 wells): pick up tip | — | One tip per column |
| 16 | Mix 24-well B1 × 3 before each column | 150 µL | Beads settle fast! |
| 17 | Aspirate + dispense into rows A–H | 100 µL/well | 8 wells per column |
| 18 | Discard tip after each column | — | — |

🔬 **Hand flat bottom plate to Squid. Full plate imaging. AI classifies each well: empty / single / multiple / uncertain.**

---

## Phase 4: Iterate — Re-seed Empty Wells (~3 min)

**Goal:** Re-seed empty wells to maximize single-cell occupancy.

| Step | Action | Notes |
|------|--------|-------|
| 19 | Read classifier JSON → identify all `empty` wells | e.g. 24 of 96 empty |
| 20 | **P1000**: pick up tip, mix C1 5× at 800µL, discard | Break up clumps before reseeding |
| 21 | **P300 single tip**: pick up ONE tip for all empty wells | — |
| 22 | Mix C1 3× every 8 wells | Beads settle fast! |
| 23 | Aspirate 80µL from C1 → dispense into each empty well | Same tip throughout |
| 24 | Discard tip at end | — |

```bash
# ALWAYS dry run first to verify:
DRY_RUN=true JSON=96_well_plate_cell_counts.json python reseed_empty_wells.py

# Demo (8 empty wells — hackathon POC):
MAX_WELLS=8 JSON=96_well_plate_cell_counts.json OT2_HOST=192.168.68.101 python reseed_empty_wells.py

# Demo v2 (24 empty wells — fuller demo):
MAX_WELLS=24 JSON=96_well_plate_cell_counts.json OT2_HOST=192.168.68.101 python reseed_empty_wells.py

# Production (all empty wells — full 96wp):
JSON=96_well_plate_cell_counts.json OT2_HOST=192.168.68.101 python reseed_empty_wells.py
```

🔬 **Re-image. Repeat until >60% single-cell occupancy or time budget reached.**

---

## Phase 5: Human QC via Monomer Culture Monitor (~5 min)

| Step | Action |
|------|--------|
| 22 | MCP uploads all 96 well labels + confidence scores |
| 23 | Uncertain wells get auto-comments from ML |
| 24 | Scientist reviews flagged wells, confirms or overrides |

---

## Time Budget

### Robot timing (based on ~35s per tip operation on OT-2)

| Phase | Robot Time | Notes |
|-------|-----------|-------|
| Phase 1: Serial dilution (8 buffer + 8 transfers) | ~10 min | 16 tip operations |
| Phase 2: Build working stock in 24-well B1 | ~3 min | ~5 tip operations |
| Phase 3: Seed 88 wells (11 columns) | ~8 min | 11 tip operations |
| **Total robot time** | **~21 min** | |

### Full cycle (robot + imaging)

| Step | Time | What Happens |
|------|------|-------------|
| Phase 1: Serial dilution | ~10 min | OT-2 fills col 1 A1→H1 |
| Squid imaging (col 1) | ~5 min | Estimate stock concentration |
| Phase 2: Working dilution | ~3 min | OT-2 scales up in 24-well B1 |
| Phase 3: Seed 88 wells | ~8 min | OT-2 fills cols 2–12 |
| Settle + full plate image | ~10 min | Squid images all 96 wells |
| Classify + iterate | ~5 min | ML classifies, OT-2 re-seeds empty wells |
| Human QC | ~5 min | Scientist reviews Culture Monitor |
| **Total end-to-end** | **~46 min** | First cycle (subsequent cycles faster) |

> ✅ Fits within the 40–50 min target window.

---

## Key Constraints

- **Minimum pipette volume: 20 µL** (P300 + 200 µL tips)
- **Aspirate height: 0.5mm** from well bottom — ensures tip reaches liquid
- **Dispense height: 2.0mm** — comfortable clearance above bottom
- **Maximum pipette volume: 180 µL** per aspiration
- **Mix before every transfer** — beads/cells settle within seconds
- **Serial dilution is down column 1** of the flat bottom (A1→H1), not in a separate plate
- **Working stock lives in 24-well deepwell B1** — made fresh after imaging

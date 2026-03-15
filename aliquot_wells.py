"""
aliquot_wells.py — Concentration-aware single-cell aliquoting

Reads the concentration JSON from the well_classifier (ImageClassifier branch)
and automatically calculates the correct dilution to achieve 1 cell per well.

Protocol:
  Step 1: Read JSON → extract stock concentration (cells/uL)
  Step 2: Calculate X uL stock (A1) + buffer (B1) needed in C1 for 1 cell/80uL
  Step 3: Transfer X uL from A1 -> C1 (P300, mix stock first)
  Step 4: Add buffer from B1 -> C1 (P300, 180uL trips)
  Step 5: P1000 mix C1 (5x at 800uL)
  Step 6: P300 single tip — 80uL from C1 into 8 wells (A1-H1 of 96wp)

Deck layout:
  Slot 1 — 24-well deepwell:
    A1 = cell/bead stock
    B1 = buffer
    C1 = working dilution (built here)
  Slot 3 — 96-well flat bottom: col 1 = target wells (A1-H1)
  Slot 6 — 200 uL filter tip rack: P300, left mount, channel 0
  Slot 9 — 1000 uL filter tip rack: P1000, right mount, channel 1

Usage:
  # Read from JSON (recommended):
  JSON=annotated_output_CerealDelusion_Run1_A/concentration_results_CerealDelusion_Run1_A.json \\
  OT2_HOST=192.168.68.101 python aliquot_wells.py

  # Manual stock concentration override:
  STOCK_CONC=4.0 OT2_HOST=192.168.68.101 python aliquot_wells.py

  # Dry run:
  DRY_RUN=true JSON=<path> python aliquot_wells.py
"""
import asyncio
import json
import logging
import os

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import OpentronsOT2Backend
from pylabrobot.liquid_handling.standard import Mix
from pylabrobot.resources import (
    OTDeck,
    Cor_96_wellplate_360ul_Fb,
    Cor_Axy_24_wellplate_10mL_Vb,
)
from pylabrobot.resources.opentrons.load import load_ot_tip_rack

logger = logging.getLogger(__name__)

# ── Load concentration from JSON or env var ───────────────────────────────────
def get_stock_conc() -> float:
    json_path = os.environ.get("JSON")
    if json_path:
        with open(json_path) as f:
            data = json.load(f)
        conc = data["concentration_estimate"]["stock_concentration_cells_per_ul"]["median_estimate"]
        logger.info("Loaded stock concentration from JSON: %.2f cells/uL", conc)
        logger.info("  Plate: %s", data.get("plate_id", "unknown"))
        logger.info("  Wells used: %d, R²: %.3f",
                    data["concentration_estimate"]["wells_used"],
                    data["concentration_estimate"]["stock_concentration_cells_per_ul"]["r_squared"])
        return conc
    elif os.environ.get("STOCK_CONC"):
        conc = float(os.environ["STOCK_CONC"])
        logger.info("Using manual STOCK_CONC=%.2f cells/uL", conc)
        return conc
    else:
        raise ValueError("Set JSON=<path> or STOCK_CONC=<value>")

# ── Configuration ─────────────────────────────────────────────────────────────
OT2_HOST  = os.environ.get("OT2_HOST")
DRY_RUN   = os.environ.get("DRY_RUN", "false").lower() == "true"

N_WELLS        = 8
ALIQUOT_VOL    = 80.0    # uL per well
DEAD_VOL       = 500.0   # uL dead volume in C1
C1_TOTAL_VOL   = N_WELLS * ALIQUOT_VOL + DEAD_VOL  # 1140 uL
DEMO_MODE      = True    # use 1mL P1000 buffer trip for demo
MIN_TRANSFER   = 20.0    # uL minimum P300 transfer

ASP_H  = 0.5   # mm aspirate height
DISP_H = 2.0   # mm dispense height
ROWS   = list("ABCDEFGH")
TARGET_WELLS = [f"{r}1" for r in ROWS]  # A1-H1


def calc_dilution(stock_conc: float):
    """Calculate stock transfer vol and buffer vol for 1 cell/ALIQUOT_VOL."""
    target_conc = 1.0 / ALIQUOT_VOL  # cells/uL = 0.0125
    stock_transfer = (target_conc * C1_TOTAL_VOL) / stock_conc

    # Enforce minimum 20uL transfer by scaling up C1 volume if needed
    if stock_transfer < MIN_TRANSFER:
        scale = MIN_TRANSFER / stock_transfer
        stock_transfer = MIN_TRANSFER
        total = C1_TOTAL_VOL * scale
        buffer_vol = total - stock_transfer
    else:
        stock_transfer = min(stock_transfer, 180.0)  # cap at P300 max
        buffer_vol = C1_TOTAL_VOL - stock_transfer

    return stock_transfer, buffer_vol


async def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    stock_conc = get_stock_conc()
    stock_transfer, buffer_vol = calc_dilution(stock_conc)

    print("=" * 58)
    print("aliquot_wells.py — Concentration-Aware Single Cell Aliquoting")
    print(f"  Stock conc (A1) : {stock_conc:.2f} cells/uL")
    print(f"  Target          : 1 cell per {ALIQUOT_VOL:.0f} uL well")
    print(f"  Stock A1 -> C1  : {stock_transfer:.1f} uL")
    print(f"  Buffer B1 -> C1 : {buffer_vol:.1f} uL")
    print(f"  C1 total        : {stock_transfer + buffer_vol:.0f} uL")
    print(f"  Aliquot         : {ALIQUOT_VOL:.0f} uL from C1 -> {N_WELLS} wells (A1-H1)")
    print(f"  Heights         : aspirate {ASP_H}mm | dispense {DISP_H}mm")
    print(f"  Dry run         : {DRY_RUN}")
    print("=" * 58)

    if DRY_RUN:
        logger.info("DRY RUN — no robot movement")
        logger.info("Step 1: Mix A1, transfer %.1f uL A1 -> C1", stock_transfer)
        logger.info("Step 2: P1000 — 1000 uL buffer B1 -> C1 (1 trip, demo mode)")
        logger.info("Step 3: P1000 mix C1 5x at 800uL")
        logger.info("Step 4: P300 single tip — %.0f uL x %d wells", ALIQUOT_VOL, N_WELLS)
        for well in TARGET_WELLS:
            logger.info("  %.0f uL -> 96wp %s", ALIQUOT_VOL, well)
        logger.info("Done! Math verified.")
        return

    if not OT2_HOST:
        raise ValueError("Set OT2_HOST: export OT2_HOST=192.168.68.101")

    # ── Deck setup ────────────────────────────────────────────────────────────
    deck    = OTDeck()
    backend = OpentronsOT2Backend(host=OT2_HOST)
    lh      = LiquidHandler(backend=backend, deck=deck)

    tip_200  = load_ot_tip_rack("opentrons_96_filtertiprack_200ul",  "200uL")
    tip_1000 = load_ot_tip_rack("opentrons_96_filtertiprack_1000ul", "1000uL")
    deck.assign_child_at_slot(tip_200,  6)
    deck.assign_child_at_slot(tip_1000, 9)

    plate_24 = Cor_Axy_24_wellplate_10mL_Vb(name="plate_24")
    plate_96 = Cor_96_wellplate_360ul_Fb(name="plate_96")
    deck.assign_child_at_slot(plate_24, 1)
    deck.assign_child_at_slot(plate_96, 3)

    tip_idx = 0
    def next_tip():
        nonlocal tip_idx
        row = ROWS[tip_idx % 8]
        col = tip_idx // 8 + 1
        tip_idx += 1
        return tip_200[f"{row}{col}"]

    async def cleanup():
        try:
            await lh.discard_tips()
        except Exception:
            pass
        try:
            await backend.home()
        except Exception:
            pass

    await lh.setup(skip_home=False)

    try:
        # ── Step 1: Add 1mL buffer B1 -> C1 via P1000 ───────────────────────────
        logger.info("=== STEP 1: P1000 — 1000 uL buffer B1 -> C1 ===")
        await lh.pick_up_tips(tip_1000["A1"], use_channels=[1])
        await lh.aspirate(plate_24["B1"], vols=[1000], liquid_height=[ASP_H], use_channels=[1])
        await lh.dispense(plate_24["C1"], vols=[1000], liquid_height=[DISP_H], use_channels=[1])
        await lh.discard_tips()
        logger.info("Step 1 complete.")

        # ── Step 2: Transfer stock A1 -> C1 ──────────────────────────────────
        logger.info("=== STEP 2: Stock A1 -> C1 (%.1f uL) ===", stock_transfer)
        await lh.pick_up_tips(next_tip(), use_channels=[0])
        # Mix stock first
        await lh.aspirate(
            plate_24["A1"], vols=[150],
            mix=[Mix(volume=150, repetitions=5, flow_rate=150)],
            liquid_height=[ASP_H], use_channels=[0],
        )
        await lh.dispense(plate_24["A1"], vols=[150], liquid_height=[DISP_H], use_channels=[0])
        await lh.aspirate(plate_24["A1"], vols=[stock_transfer], liquid_height=[ASP_H], use_channels=[0])
        await lh.dispense(plate_24["C1"], vols=[stock_transfer], liquid_height=[DISP_H], use_channels=[0])
        await lh.discard_tips()
        logger.info("Step 2 complete.")

        # ── Step 3: P1000 mix C1 ─────────────────────────────────────────────
        logger.info("=== STEP 3: P1000 mix C1 (5x at 800uL) ===")
        await lh.pick_up_tips(tip_1000["A2"], use_channels=[1])
        await lh.aspirate(
            plate_24["C1"], vols=[800],
            mix=[Mix(volume=800, repetitions=5, flow_rate=400)],
            liquid_height=[ASP_H], use_channels=[1],
        )
        await lh.dispense(plate_24["C1"], vols=[800], liquid_height=[DISP_H], use_channels=[1])
        await lh.discard_tips()
        logger.info("Step 3 complete.")

        # ── Step 4: P300 single tip — 80uL from C1 -> 8 wells ────────────────
        logger.info("=== STEP 4: P300 — %.0f uL from C1 -> %d wells ===", ALIQUOT_VOL, N_WELLS)
        await lh.pick_up_tips(next_tip(), use_channels=[0])
        for i, well in enumerate(TARGET_WELLS):
            logger.info("  %d/%d: %.0f uL -> 96wp %s", i+1, N_WELLS, ALIQUOT_VOL, well)
            if i % 8 == 0:
                await lh.aspirate(
                    plate_24["C1"], vols=[150],
                    mix=[Mix(volume=150, repetitions=3, flow_rate=150)],
                    liquid_height=[ASP_H], use_channels=[0],
                )
                await lh.dispense(plate_24["C1"], vols=[150], liquid_height=[DISP_H], use_channels=[0])
            await lh.aspirate(plate_24["C1"], vols=[ALIQUOT_VOL], liquid_height=[ASP_H], use_channels=[0])
            await lh.dispense(plate_96[well], vols=[ALIQUOT_VOL], liquid_height=[DISP_H], use_channels=[0])
        await lh.discard_tips()
        logger.info("Step 4 complete.")

        logger.info("=== Done! %d wells filled with %.0f uL each. ===", N_WELLS, ALIQUOT_VOL)
        logger.info(">>> Move plate to Squid for imaging.")
        await backend.home()

    except BaseException:
        logger.warning("Protocol interrupted — running cleanup")
        await cleanup()
        raise


if __name__ == "__main__":
    asyncio.run(run())

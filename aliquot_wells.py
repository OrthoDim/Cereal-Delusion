"""
aliquot_wells.py — Two-step aliquoting for single-cell isolation

Protocol:
  Step 1: P300 single tip — aliquot 80uL into 24 wells (cols 1-3 of 96wp)
  Step 2: P1000 — mix D1 5x at 800uL to resuspend beads
  Step 3: P300 single tip — aliquot 20uL into same 24 wells

  Total per well: 80uL + 20uL = 100uL

Deck layout:
  Slot 1 — 24-well deepwell: D1 = working dilution
  Slot 3 — 96-well flat bottom: destination plate
  Slot 6 — 200 uL filter tip rack: P300, left mount, channel 0
  Slot 9 — 1000 uL filter tip rack: P1000, right mount, channel 1

Usage:
  OT2_HOST=192.168.68.101 python aliquot_wells.py
  DRY_RUN=true python aliquot_wells.py
"""
import asyncio
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

# ── Configuration ─────────────────────────────────────────────────────────────
OT2_HOST     = os.environ.get("OT2_HOST")
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"
WORKING_WELL = os.environ.get("WORKING_WELL", "D1")

N_WELLS      = 24       # first 24 wells = cols 1-3 of 96wp
VOL_STEP1    = 80.0     # uL — first aliquot
VOL_STEP3    = 20.0     # uL — second aliquot (same wells)
ROWS         = list("ABCDEFGH")

# Heights — aspirate always 0.5mm, dispense always 2.0mm
ASP_H  = 0.5
DISP_H = 2.0

# First 24 wells: A1-H1, A2-H2, A3-H3
TARGET_WELLS = [f"{r}{c}" for c in range(1, 4) for r in ROWS]

print("=" * 55)
print("aliquot_wells.py — Two-Step Single Cell Aliquoting")
print(f"  Source         : 24-well {WORKING_WELL}")
print(f"  Target wells   : {N_WELLS} wells (cols 1-3: {TARGET_WELLS[0]}-{TARGET_WELLS[-1]})")
print(f"  Step 1         : {VOL_STEP1:.0f} uL/well (P300 single tip)")
print(f"  Step 2         : P1000 mix {WORKING_WELL} 5x at 800uL")
print(f"  Step 3         : {VOL_STEP3:.0f} uL/well (P300 single tip)")
print(f"  Total/well     : {VOL_STEP1 + VOL_STEP3:.0f} uL")
print(f"  Aspirate height: {ASP_H}mm | Dispense height: {DISP_H}mm")
print(f"  Dry run        : {DRY_RUN}")
print("=" * 55)


async def run():
    if DRY_RUN:
        logger.info("DRY RUN — no robot movement")
        logger.info("Step 1: P300 tip 1 — %.0f uL into %d wells", VOL_STEP1, N_WELLS)
        for well in TARGET_WELLS:
            logger.info("  %.0f uL -> %s", VOL_STEP1, well)
        logger.info("Step 2: P1000 — mix %s 5x at 800uL", WORKING_WELL)
        logger.info("Step 3: P300 tip 2 — %.0f uL into %d wells", VOL_STEP3, N_WELLS)
        for well in TARGET_WELLS:
            logger.info("  %.0f uL -> %s", VOL_STEP3, well)
        logger.info("Done — %.0f uL total per well", VOL_STEP1 + VOL_STEP3)
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
        # ── Step 1: P300 single tip — 80uL into 24 wells ─────────────────────
        logger.info("=== STEP 1: P300 single tip — %.0f uL into %d wells ===",
                    VOL_STEP1, N_WELLS)
        await lh.pick_up_tips(next_tip(), use_channels=[0])
        for i, well in enumerate(TARGET_WELLS):
            logger.info("  %d/%d: %.0f uL -> 96wp %s", i+1, N_WELLS, VOL_STEP1, well)
            # Mix every 8 wells
            if i % 8 == 0:
                await lh.aspirate(
                    plate_24[WORKING_WELL], vols=[150],
                    mix=[Mix(volume=150, repetitions=3, flow_rate=150)],
                    liquid_height=[ASP_H], use_channels=[0],
                )
                await lh.dispense(plate_24[WORKING_WELL], vols=[150],
                                  liquid_height=[DISP_H], use_channels=[0])
            await lh.aspirate(plate_24[WORKING_WELL], vols=[VOL_STEP1],
                              liquid_height=[ASP_H], use_channels=[0])
            await lh.dispense(plate_96[well], vols=[VOL_STEP1],
                              liquid_height=[DISP_H], use_channels=[0])
        await lh.discard_tips()
        logger.info("Step 1 complete.")

        # ── Step 2: P1000 mix D1 ─────────────────────────────────────────────
        logger.info("=== STEP 2: P1000 — mixing %s 5x at 800uL ===", WORKING_WELL)
        await lh.pick_up_tips(tip_1000["A1"], use_channels=[1])
        await lh.aspirate(
            plate_24[WORKING_WELL], vols=[800],
            mix=[Mix(volume=800, repetitions=5, flow_rate=400)],
            liquid_height=[ASP_H], use_channels=[1],
        )
        await lh.dispense(plate_24[WORKING_WELL], vols=[800],
                          liquid_height=[DISP_H], use_channels=[1])
        await lh.discard_tips()
        logger.info("Step 2 complete.")

        # ── Step 3: P300 single tip — 20uL into same 24 wells ────────────────
        logger.info("=== STEP 3: P300 single tip — %.0f uL into same %d wells ===",
                    VOL_STEP3, N_WELLS)
        await lh.pick_up_tips(next_tip(), use_channels=[0])
        for i, well in enumerate(TARGET_WELLS):
            logger.info("  %d/%d: %.0f uL -> 96wp %s", i+1, N_WELLS, VOL_STEP3, well)
            if i % 8 == 0:
                await lh.aspirate(
                    plate_24[WORKING_WELL], vols=[150],
                    mix=[Mix(volume=150, repetitions=3, flow_rate=150)],
                    liquid_height=[ASP_H], use_channels=[0],
                )
                await lh.dispense(plate_24[WORKING_WELL], vols=[150],
                                  liquid_height=[DISP_H], use_channels=[0])
            await lh.aspirate(plate_24[WORKING_WELL], vols=[VOL_STEP3],
                              liquid_height=[ASP_H], use_channels=[0])
            await lh.dispense(plate_96[well], vols=[VOL_STEP3],
                              liquid_height=[DISP_H], use_channels=[0])
        await lh.discard_tips()
        logger.info("Step 3 complete.")

        logger.info("=== Aliquoting done! %d wells filled with %.0f uL each. ===",
                    N_WELLS, VOL_STEP1 + VOL_STEP3)
        logger.info(">>> Move plate to Squid for imaging.")
        await backend.home()

    except BaseException:
        logger.warning("Protocol interrupted — running cleanup")
        await cleanup()
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run())

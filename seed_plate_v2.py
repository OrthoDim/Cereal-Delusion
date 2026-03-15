"""
seed_plate_v2.py — Serial dilution + 96-well seeding for single-cell cloning (Track B)

Protocol:
  Phase 1: 8-step serial dilution (1:2 each) DOWN column 1 of the 96wp flat bottom
           100uL buffer pre-filled, 100uL transfers → A1=1:2, B1=1:4 ... H1=1:256
           → image col 1 on Squid to find best concentration
  Phase 2: Make working dilution in 24-well D1 at scale (buffer from C1)
  Phase 3: Seed cols 2-12 of flat bottom (88 wells, 100 uL each)

Deck layout:
  Slot 1 — 24-well deepwell (10 mL):
    A1 = bead/cell stock
    B1 = buffer (Phase 1 pre-fill)
    C1 = buffer (Phase 2 scale-up)
    D1 = working dilution destination (Phase 2)
  Slot 3 — 96-well flat bottom: col 1 = serial dilution, cols 2-12 = seeded
  Slot 6 — 200 uL filter tip rack: P300, left mount, channel 0
  Slot 9 — 1000 uL filter tip rack: P1000, right mount, channel 1 (unused now)

Usage:
  Phase 1 only:
    export OT2_HOST=192.168.68.101
    python seed_plate_v2.py

  Phase 2+3 (after imaging, set which well had ~1 bead):
    WORKING_WELL=D1 OT2_HOST=192.168.68.101 python seed_plate_v2.py
    (comment out phase1, uncomment phase2+3 in main())
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
OT2_HOST = os.environ.get("OT2_HOST")
if not OT2_HOST:
    raise ValueError("Set OT2_HOST: export OT2_HOST=192.168.68.101")

# 24-well source wells
STOCK_WELL        = "A1"   # bead/cell stock
BUFFER_WELL_P1    = "B1"   # buffer for Phase 1 pre-fill
BUFFER_WELL_P2    = "C1"   # buffer for Phase 2 scale-up
WORKING_DEST_WELL = "D1"   # working dilution destination

# Serial dilution settings — all using P300 (200uL tips)
SERIAL_BUFFER_VOL = 100.0  # uL buffer pre-filled per well
SERIAL_STOCK_VOL  = 100.0  # uL transferred each step → 1:2 per step
SERIAL_MIX_VOL    = 150.0  # uL for mixing (75% of 200uL total)
SERIAL_ROWS       = list("ABCDEFGH")

# Heights
ASPIRATE_24WELL   = 0.5    # mm — aspirate from 24-well deepwell
DISPENSE_24WELL   = 2.0    # mm — dispense into 24-well deepwell
ASPIRATE_96WP     = 0.7    # mm — aspirate from 96wp
DISPENSE_96WP     = 2.0    # mm — dispense into 96wp flat bottom (between 0.5-1.0)

# Seeding settings
FINAL_VOL_UL      = 100.0
SEED_COLS         = range(2, 13)
ROWS              = list("ABCDEFGH")

WORKING_WELL      = os.environ.get("WORKING_WELL", "D1")
SEED_WELLS_COUNT  = len(list(SEED_COLS)) * len(ROWS)   # 88
WORKING_VOL_TOTAL = SEED_WELLS_COUNT * FINAL_VOL_UL + 500  # ~9300 uL

print("=" * 58)
print("seed_plate_v2.py — Track B Single-Cell Cloning")
print(f"  Serial dilution : down col 1 (A1->H1), 1:2 per step")
print(f"  Buffer/well     : {SERIAL_BUFFER_VOL:.0f} uL (from 24-well {BUFFER_WELL_P1})")
print(f"  Transfer/step   : {SERIAL_STOCK_VOL:.0f} uL — P300 (200uL tip)")
print(f"  Working well    : 96wp {WORKING_WELL} -> 24-well {WORKING_DEST_WELL}")
print(f"  Seed wells      : {SEED_WELLS_COUNT} wells, {FINAL_VOL_UL:.0f} uL each")
print(f"  Heights (24wp)  : aspirate {ASPIRATE_24WELL}mm | dispense {DISPENSE_24WELL}mm")
print(f"  Heights (96wp)  : aspirate {ASPIRATE_96WP}mm | dispense {DISPENSE_96WP}mm")
print("=" * 58)

# ── Deck setup ────────────────────────────────────────────────────────────────
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

# P300 tip tracker
tip_idx = 0

def next_tip():
    global tip_idx
    row = ROWS[tip_idx % 8]
    col = tip_idx // 8 + 1
    tip_idx += 1
    return tip_200[f"{row}{col}"]


# ── Cleanup ───────────────────────────────────────────────────────────────────
async def cleanup():
    logger.info("Cleanup: discarding tips and homing")
    try:
        await lh.discard_tips()
    except Exception as e:
        logger.warning("Could not discard tips: %s", e)
    try:
        await backend.home()
    except Exception as e:
        logger.warning("Home failed: %s", e)


# ── Phase 1: Serial dilution down col 1 ──────────────────────────────────────
async def phase1_serial_dilution():
    logger.info("=== PHASE 1: Serial dilution down 96wp col 1 (A1->H1) ===")

    # P300: one tip, pre-fill all 8 wells with 100uL buffer each
    logger.info("  P300: pre-filling col 1 with %suL buffer (one tip)", int(SERIAL_BUFFER_VOL))
    await lh.pick_up_tips(next_tip(), use_channels=[0])
    for row in SERIAL_ROWS:
        well = f"{row}1"
        logger.info("    Buffer -> 96wp %s", well)
        await lh.aspirate(plate_24[BUFFER_WELL_P1], vols=[SERIAL_BUFFER_VOL],
                          liquid_height=[ASPIRATE_24WELL], use_channels=[0])
        await lh.dispense(plate_96[well], vols=[SERIAL_BUFFER_VOL],
                          liquid_height=[DISPENSE_96WP], use_channels=[0])
    await lh.discard_tips()

    # P1000: dedicated mix of stock A1 — beads are clumpy!
    logger.info("  P1000: mixing stock A1 (10x at 800uL) before protocol")
    await lh.pick_up_tips(tip_1000["A1"], use_channels=[1])
    await lh.aspirate(
        plate_24[STOCK_WELL], vols=[800],
        mix=[Mix(volume=800, repetitions=5, flow_rate=400)],
        liquid_height=[ASPIRATE_24WELL], use_channels=[1],
    )
    await lh.dispense(plate_24[STOCK_WELL], vols=[800],
                      liquid_height=[DISPENSE_24WELL], use_channels=[1])
    await lh.discard_tips()
    logger.info("  P1000: stock mixed, tip discarded")

    # P300: one tip for all serial transfers
    logger.info("  P300: single tip for all serial transfers")
    await lh.pick_up_tips(next_tip(), use_channels=[0])
    for i, row in enumerate(SERIAL_ROWS):
        dest_well = f"{row}1"
        source    = plate_24[STOCK_WELL] if i == 0 else plate_96[f"{SERIAL_ROWS[i-1]}1"]
        dilution  = 2 ** (i + 1)
        logger.info("    Step %d: 1:%d -> 96wp %s (%suL + mix 5x)",
                    i+1, dilution, dest_well, int(SERIAL_STOCK_VOL))

        # Extra P300 mix of stock before first aspirate
        if i == 0:
            logger.info("    P300 mix stock (A1) 5x before first aspirate")
            await lh.aspirate(
                plate_24[STOCK_WELL], vols=[150],
                mix=[Mix(volume=150, repetitions=5, flow_rate=150)],
                liquid_height=[ASPIRATE_24WELL], use_channels=[0],
            )
            await lh.dispense(plate_24[STOCK_WELL], vols=[150],
                              liquid_height=[DISPENSE_24WELL], use_channels=[0])

        await lh.aspirate(source, vols=[SERIAL_STOCK_VOL],
                          liquid_height=[ASPIRATE_24WELL if i == 0 else ASPIRATE_96WP],
                          use_channels=[0])
        await lh.dispense(plate_96[dest_well], vols=[SERIAL_STOCK_VOL],
                          liquid_height=[DISPENSE_96WP], use_channels=[0])
        # Mix dest 5x
        await lh.aspirate(
            plate_96[dest_well], vols=[SERIAL_MIX_VOL],
            mix=[Mix(volume=SERIAL_MIX_VOL, repetitions=5, flow_rate=150)],
            liquid_height=[ASPIRATE_96WP], use_channels=[0],
        )
        await lh.dispense(plate_96[dest_well], vols=[SERIAL_MIX_VOL],
                          liquid_height=[DISPENSE_96WP], use_channels=[0])

    await lh.discard_tips()
    logger.info("  P300: tip discarded after all 8 serial steps")
    logger.info("Phase 1 complete!")
    logger.info(">>> Hand flat bottom plate (slot 3) to Squid.")
    logger.info(">>> Image column 1 (A1-H1). Find well with ~1 bead/field.")
    logger.info(">>> Set WORKING_WELL=<that well> and re-run with phase2+3 uncommented.")


# ── Phase 2: Scale up working dilution into 24-well D1 ───────────────────────
async def phase2_working_dilution():
    logger.info("=== PHASE 2: Working dilution -> 24-well %s (source: 96wp %s) ===",
                WORKING_DEST_WELL, WORKING_WELL)

    source_vol = 20.0
    buffer_vol = WORKING_VOL_TOTAL - source_vol

    # Add buffer from C1 into D1 in 180uL trips
    remaining = buffer_vol
    trip = 1
    while remaining > 0:
        vol = min(180.0, remaining)
        logger.info("  Buffer trip %d: %.0f uL (%s -> %s)", trip, vol, BUFFER_WELL_P2, WORKING_DEST_WELL)
        await lh.pick_up_tips(next_tip(), use_channels=[0])
        await lh.aspirate(plate_24[BUFFER_WELL_P2],    vols=[vol],
                          liquid_height=[ASPIRATE_24WELL], use_channels=[0])
        await lh.dispense(plate_24[WORKING_DEST_WELL], vols=[vol],
                          liquid_height=[DISPENSE_24WELL], use_channels=[0])
        await lh.discard_tips()
        remaining -= vol
        trip += 1

    # Add 20uL from chosen serial well + mix
    logger.info("  Source: 96wp %s -> 24-well %s (%.0f uL + mix 10x)",
                WORKING_WELL, WORKING_DEST_WELL, source_vol)
    await lh.pick_up_tips(next_tip(), use_channels=[0])
    await lh.aspirate(plate_96[WORKING_WELL],          vols=[source_vol],
                      liquid_height=[ASPIRATE_96WP], use_channels=[0])
    await lh.dispense(plate_24[WORKING_DEST_WELL],     vols=[source_vol],
                      liquid_height=[DISPENSE_24WELL], use_channels=[0])
    await lh.aspirate(
        plate_24[WORKING_DEST_WELL], vols=[150],
        mix=[Mix(volume=150, repetitions=10, flow_rate=150)],
        liquid_height=[ASPIRATE_24WELL], use_channels=[0],
    )
    await lh.dispense(plate_24[WORKING_DEST_WELL], vols=[150],
                      liquid_height=[DISPENSE_24WELL], use_channels=[0])
    await lh.discard_tips()
    logger.info("Phase 2 complete. Working dilution ready in 24-well %s.", WORKING_DEST_WELL)


# ── Phase 3: Seed cols 2-12 of flat bottom ───────────────────────────────────
async def phase3_seed_plate():
    logger.info("=== PHASE 3: Seeding %d wells (%.0f uL/well, cols 2-12) ===",
                SEED_WELLS_COUNT, FINAL_VOL_UL)

    for col in SEED_COLS:
        logger.info("  Column %02d/%02d", col, max(SEED_COLS))
        await lh.pick_up_tips(next_tip(), use_channels=[0])

        # Mix working dilution before each column — beads settle!
        await lh.aspirate(
            plate_24[WORKING_DEST_WELL], vols=[150],
            mix=[Mix(volume=150, repetitions=3, flow_rate=150)],
            liquid_height=[ASPIRATE_24WELL], use_channels=[0],
        )
        await lh.dispense(plate_24[WORKING_DEST_WELL], vols=[150],
                          liquid_height=[DISPENSE_24WELL], use_channels=[0])

        for row in ROWS:
            await lh.aspirate(plate_24[WORKING_DEST_WELL], vols=[FINAL_VOL_UL],
                              liquid_height=[ASPIRATE_24WELL], use_channels=[0])
            await lh.dispense(plate_96[f"{row}{col}"],      vols=[FINAL_VOL_UL],
                              liquid_height=[DISPENSE_96WP], use_channels=[0])

        await lh.discard_tips()

    logger.info("Phase 3 complete! Move flat plate to Squid for full-plate imaging.")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Setup: homing robot")
    await lh.setup(skip_home=False)

    try:
        # ── PHASE 1: Serial dilution down col 1, then image ─────────────────
        await phase1_serial_dilution()

        # ── PHASE 2+3: Uncomment after imaging, set WORKING_WELL env var ────
        # await phase2_working_dilution()
        # await phase3_seed_plate()

        logger.info("Protocol finished successfully")
        await backend.home()

    except BaseException:
        logger.warning("Protocol interrupted or failed — running cleanup")
        await cleanup()
        raise


if __name__ == "__main__":
    asyncio.run(main())

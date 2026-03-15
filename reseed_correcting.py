"""
reseed_correcting.py — Re-seed empty wells AND move multiples to new wells

Reads classifier JSON and:
  1. Re-seeds EMPTY wells with working dilution (same as reseed_empty_wells.py)
  2. For MULTIPLE wells — dispenses into a fresh unused well to try for single

JSON format expected (per well):
  "A1": { "cell_count": 0, "empty": true }
  "A2": { "cell_count": 1, "single": true }
  "A3": { "cell_count": 3, "multiple": true }

Usage:
  JSON=96_well_plate_cell_counts.json OT2_HOST=192.168.68.101 python reseed_correcting.py
  DRY_RUN=true JSON=96_well_plate_cell_counts.json python reseed_correcting.py
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

OT2_HOST    = os.environ.get("OT2_HOST")
DRY_RUN     = os.environ.get("DRY_RUN", "false").lower() == "true"
SOURCE_WELL = os.environ.get("SOURCE_WELL", "C1")
RESEED_VOL  = float(os.environ.get("RESEED_VOL", "80"))
ASP_H       = 0.5
DISP_H      = 2.0
ROWS        = list("ABCDEFGH")

# All 96 wells in plate order
ALL_WELLS = [f"{r}{c}" for c in range(1, 13) for r in ROWS]


def load_wells(json_path: str):
    with open(json_path) as f:
        data = json.load(f)

    empty_wells    = []
    multiple_wells = []
    occupied_wells = set()

    for well_id, info in data["wells"].items():
        count = info.get("cell_count", 0)
        if info.get("empty", False) or count == 0:
            empty_wells.append(well_id)
        elif count > 1:
            multiple_wells.append(well_id)
        else:
            occupied_wells.add(well_id)

    # Find unused wells for relocating multiples
    used = set(empty_wells) | set(multiple_wells) | occupied_wells
    spare_wells = [w for w in ALL_WELLS if w not in used]

    def sort_key(w):
        return (int(w[1:]), w[0])

    empty_wells.sort(key=sort_key)
    multiple_wells.sort(key=sort_key)

    print("=" * 58)
    print("reseed_correcting.py — Re-seed + Correct")
    print(f"  Empty wells     : {len(empty_wells)} → will be re-seeded")
    print(f"  Multiple wells  : {len(multiple_wells)} → will be re-tried in new wells")
    print(f"  Single wells    : {len(occupied_wells)} → untouched ✅")
    print(f"  Spare wells     : {len(spare_wells)} available for relocation")
    print(f"  Source          : 24-well {SOURCE_WELL}")
    print(f"  Volume/well     : {RESEED_VOL:.0f} uL")
    print(f"  Dry run         : {DRY_RUN}")
    print("=" * 58)

    # Pair each multiple well with a spare destination
    relocation_pairs = list(zip(multiple_wells, spare_wells))

    return empty_wells, relocation_pairs


async def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    json_path = os.environ.get("JSON")
    if not json_path:
        # Demo mode
        logger.info("DEMO MODE — hardcoded wells")
        empty_wells = ["A1", "D2"]
        relocation_pairs = [("B3", "H12"), ("C4", "G11")]
    else:
        empty_wells, relocation_pairs = load_wells(json_path)

    total_actions = len(empty_wells) + len(relocation_pairs)
    if total_actions == 0:
        logger.info("Nothing to do — all wells are singles!")
        return

    if DRY_RUN:
        logger.info("DRY RUN — no robot movement")
        logger.info("--- Re-seeding %d empty wells ---", len(empty_wells))
        for well in empty_wells:
            logger.info("  %.0f uL -> 96wp %s", RESEED_VOL, well)
        logger.info("--- Re-trying %d multiple wells in new locations ---", len(relocation_pairs))
        for src, dst in relocation_pairs:
            logger.info("  Multiple in %s -> new attempt at %s (%.0f uL)", src, dst, RESEED_VOL)
        return

    if not OT2_HOST:
        raise ValueError("Set OT2_HOST: export OT2_HOST=192.168.68.101")

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
        try: await lh.discard_tips()
        except Exception: pass
        try: await backend.home()
        except Exception: pass

    await lh.setup(skip_home=False)

    try:
        # P1000: mix source C1 before starting
        logger.info("P1000: mixing %s (5x at 800uL)", SOURCE_WELL)
        await lh.pick_up_tips(tip_1000["A1"], use_channels=[1])
        await lh.aspirate(plate_24[SOURCE_WELL], vols=[800],
            mix=[Mix(volume=800, repetitions=5, flow_rate=400)],
            liquid_height=[ASP_H], use_channels=[1])
        await lh.dispense(plate_24[SOURCE_WELL], vols=[800], liquid_height=[DISP_H], use_channels=[1])
        await lh.discard_tips()

        # P300 single tip for all dispensing
        await lh.pick_up_tips(next_tip(), use_channels=[0])

        # Re-seed empty wells
        logger.info("=== Re-seeding %d empty wells ===", len(empty_wells))
        for i, well in enumerate(empty_wells):
            logger.info("  Empty %d/%d: -> %s", i+1, len(empty_wells), well)
            if i % 8 == 0 and i > 0:
                await lh.aspirate(plate_24[SOURCE_WELL], vols=[150],
                    mix=[Mix(volume=150, repetitions=3, flow_rate=150)],
                    liquid_height=[ASP_H], use_channels=[0])
                await lh.dispense(plate_24[SOURCE_WELL], vols=[150], liquid_height=[DISP_H], use_channels=[0])
            await lh.aspirate(plate_24[SOURCE_WELL], vols=[RESEED_VOL], liquid_height=[ASP_H], use_channels=[0])
            await lh.dispense(plate_96[well], vols=[RESEED_VOL], liquid_height=[DISP_H], use_channels=[0])

        # Relocate multiples to new wells
        logger.info("=== Re-trying %d multiple wells in new locations ===", len(relocation_pairs))
        for i, (src, dst) in enumerate(relocation_pairs):
            logger.info("  Multiple %d/%d: %s -> new well %s", i+1, len(relocation_pairs), src, dst)
            await lh.aspirate(plate_24[SOURCE_WELL], vols=[RESEED_VOL], liquid_height=[ASP_H], use_channels=[0])
            await lh.dispense(plate_96[dst], vols=[RESEED_VOL], liquid_height=[DISP_H], use_channels=[0])

        await lh.discard_tips()
        logger.info("=== Done! %d empty re-seeded, %d multiples relocated. ===",
                    len(empty_wells), len(relocation_pairs))
        logger.info(">>> Move plate to Squid for re-imaging.")
        await backend.home()

    except BaseException:
        logger.warning("Interrupted — cleanup")
        await cleanup()
        raise


if __name__ == "__main__":
    asyncio.run(run())

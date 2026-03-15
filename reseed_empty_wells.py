"""
reseed_empty_wells.py — Re-seed empty wells based on classifier JSON output

Reads the well classification JSON, identifies empty wells, and dispenses
working dilution (from 24-well C1) into only those wells.

This is the iterative step that maximizes single-cell occupancy.

JSON format expected:
  {
    "wells": {
      "A1": { "cell_count": 0, "empty": true },
      "A2": { "cell_count": 14, "empty": false },
      ...
    },
    "summary": { "empty_wells": 24, ... }
  }

Deck layout:
  Slot 1 — 24-well deepwell: C1 = working dilution (from aliquot_wells.py)
  Slot 3 — 96-well flat bottom: plate to re-seed
  Slot 6 — 200 uL filter tip rack: P300, left mount, channel 0
  Slot 9 — 1000 uL filter tip rack: P1000, right mount, channel 1

Usage:
  # Re-seed all empty wells (up to 96):
  JSON=96_well_plate_cell_counts.json OT2_HOST=192.168.68.101 python reseed_empty_wells.py

  # Demo mode — only re-seed first 8 empty wells:
  JSON=96_well_plate_cell_counts.json MAX_WELLS=8 OT2_HOST=192.168.68.101 python reseed_empty_wells.py

  # Dry run:
  DRY_RUN=true JSON=96_well_plate_cell_counts.json python reseed_empty_wells.py
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

# ── Configuration ─────────────────────────────────────────────────────────────
OT2_HOST     = os.environ.get("OT2_HOST")
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"
SOURCE_WELL  = os.environ.get("SOURCE_WELL", "C1")   # working dilution in 24-well
RESEED_VOL   = float(os.environ.get("RESEED_VOL", "80"))  # uL per empty well
MAX_WELLS    = int(os.environ.get("MAX_WELLS", "96"))  # cap for demo: 8, 24, or 96

ASP_H  = 0.5
DISP_H = 2.0
ROWS   = list("ABCDEFGH")


def load_empty_wells(json_path: str) -> list:
    with open(json_path) as f:
        data = json.load(f)

    # Find all empty wells, sorted in plate order (A1, A2... H12)
    all_wells = [
        well_id for well_id, info in data["wells"].items()
        if info.get("empty", False)
    ]

    # Sort by column then row (plate order)
    def well_sort_key(w):
        row = w[0]
        col = int(w[1:])
        return (col, row)

    all_wells.sort(key=well_sort_key)
    empty_wells = all_wells[:MAX_WELLS]

    summary = data.get("summary", {})
    print("=" * 58)
    print(f"reseed_empty_wells.py — Re-seeding Empty Wells")
    print(f"  Plate           : {data.get('plate_id', 'unknown')} ({data.get('date', '')})")
    print(f"  Total wells     : {summary.get('total_wells', 96)}")
    print(f"  Occupied        : {summary.get('occupied_wells', '?')} ({summary.get('occupied_percent', '?')}%)")
    print(f"  Empty (total)   : {summary.get('empty_wells', len(all_wells))}")
    print(f"  Re-seeding      : {len(empty_wells)} wells (MAX_WELLS={MAX_WELLS})")
    print(f"  Volume/well     : {RESEED_VOL:.0f} uL from 24-well {SOURCE_WELL}")
    print(f"  Empty wells     : {', '.join(empty_wells)}")
    print(f"  Dry run         : {DRY_RUN}")
    print("=" * 58)
    return empty_wells


async def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    json_path = os.environ.get("JSON")
    if not json_path:
        raise ValueError("Set JSON=<path to well classification JSON>")

    empty_wells = load_empty_wells(json_path)

    if not empty_wells:
        logger.info("No empty wells found — plate fully occupied! Nothing to do.")
        return

    if DRY_RUN:
        logger.info("DRY RUN — no robot movement")
        logger.info("Would re-seed %d wells:", len(empty_wells))
        for i, well in enumerate(empty_wells):
            logger.info("  %d/%d: %.0f uL -> 96wp %s", i+1, len(empty_wells), RESEED_VOL, well)
        logger.info("Total volume needed: %.0f uL from 24-well %s",
                    len(empty_wells) * RESEED_VOL, SOURCE_WELL)
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
        # P1000: mix source well before starting
        logger.info("P1000: mixing source %s (5x at 800uL) before reseeding", SOURCE_WELL)
        await lh.pick_up_tips(tip_1000["A1"], use_channels=[1])
        await lh.aspirate(
            plate_24[SOURCE_WELL], vols=[800],
            mix=[Mix(volume=800, repetitions=5, flow_rate=400)],
            liquid_height=[ASP_H], use_channels=[1],
        )
        await lh.dispense(plate_24[SOURCE_WELL], vols=[800],
                          liquid_height=[DISP_H], use_channels=[1])
        await lh.discard_tips()

        # P300: single tip for ALL empty wells
        logger.info("=== Re-seeding %d empty wells (%.0f uL each) ===",
                    len(empty_wells), RESEED_VOL)
        await lh.pick_up_tips(next_tip(), use_channels=[0])

        for i, well in enumerate(empty_wells):
            logger.info("  %d/%d: %.0f uL -> 96wp %s", i+1, len(empty_wells), RESEED_VOL, well)

            # Mix source every 8 wells — beads settle!
            if i % 8 == 0 and i > 0:
                logger.info("  Mixing source %s 3x", SOURCE_WELL)
                await lh.aspirate(
                    plate_24[SOURCE_WELL], vols=[150],
                    mix=[Mix(volume=150, repetitions=3, flow_rate=150)],
                    liquid_height=[ASP_H], use_channels=[0],
                )
                await lh.dispense(plate_24[SOURCE_WELL], vols=[150],
                                  liquid_height=[DISP_H], use_channels=[0])

            await lh.aspirate(plate_24[SOURCE_WELL], vols=[RESEED_VOL],
                              liquid_height=[ASP_H], use_channels=[0])
            await lh.dispense(plate_96[well], vols=[RESEED_VOL],
                              liquid_height=[DISP_H], use_channels=[0])

        await lh.discard_tips()
        logger.info("=== Reseeding complete! %d empty wells filled. ===", len(empty_wells))
        logger.info(">>> Move plate to Squid for re-imaging.")
        logger.info(">>> Run classifier again to check occupancy improvement.")
        await backend.home()

    except BaseException:
        logger.warning("Protocol interrupted — running cleanup")
        await cleanup()
        raise


if __name__ == "__main__":
    asyncio.run(run())

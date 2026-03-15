"""Well classification pipeline for 96-well plate fluorescence microscopy.

DEPRECATED: This module is a thin wrapper around classify_single_cells.
Use `python -m well_classifier.classify_single_cells` instead.

Maintained for backwards compatibility only.
"""

import sys
import warnings

# Re-export everything from the new modules for import compatibility
from .core import (
    BeadInfo,
    WellResult,
    REFERENCE_DIM,
    DEFAULTS,
    WELL_ROWS,
    WELL_COLS,
    normalize_quadrants,
    mask_seams,
    well_roi_mask,
    segment_beads,
    parse_well_id_from_filename,
    debug_overlay,
    annotated_bead_image,
    results_to_mcp_payload,
)

from .classify_single_cells import (
    classify_well,
    classify_plate,
    summarize_results,
    results_to_plate_json,
    _single_confidence,
)


def main():
    warnings.warn(
        "classify_wells.py is deprecated. "
        "Use 'python -m well_classifier.classify_single_cells' instead.",
        DeprecationWarning,
        stacklevel=1,
    )
    print(
        "WARNING: classify_wells.py is deprecated. "
        "Use 'python -m well_classifier.classify_single_cells' instead.",
        file=sys.stderr,
    )
    from .classify_single_cells import main as _main
    _main()


if __name__ == "__main__":
    main()

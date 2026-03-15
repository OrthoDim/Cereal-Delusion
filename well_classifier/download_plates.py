"""
Download plate images from Monomer Cloud MCP.

This is a reference script showing how to download well images using the
Monomer Cloud MCP tools. It is designed to be used with Claude Code's MCP
integration — see the SKILL.md file for the interactive workflow.

For automated use, the MCP tools are called via Claude Code skills:
  - list_plates: Browse available plates
  - list_cultures: Get wells on a plate
  - get_observation_image_access: Get presigned download URLs
  - get_plate_observations: List imaging timepoints

Typical workflow:
  1. List plates to find the one you want
  2. List cultures on the plate to get well IDs
  3. For each culture, get the image access URL
  4. Download images with curl (parallel for speed)
  5. Run classify_wells.py on the downloaded images

Example using Claude Code:
  > /download-plates
  (interactive prompt to select plates and download images)

  > python well_classifier/classify_wells.py monomer_images/<barcode> \\
      --output results.json --output-dir annotated_output/<barcode>
"""

# This file serves as documentation. The actual download logic is
# implemented as a Claude Code skill (see skills/download-plates/SKILL.md)
# because it requires MCP tool access which runs through the AI assistant.
#
# For programmatic access to Monomer Cloud, see the Monomer API documentation.

#!/usr/bin/env python3
"""Fetch new observation images from Monomer Cloud MCP for Cereal Delusion plates."""

from pathlib import Path
from urllib.request import Request, urlopen

from .monomer_client import MonomerMCPClient

PLATE_PREFIX = "Cereal"


def download_image(url, dest):
    req = Request(url)
    with urlopen(req) as resp:
        dest.write_bytes(resp.read())


def main():
    images_dir = Path(__file__).parent.parent / "monomer_images"

    print("Connecting to Monomer Cloud MCP...")
    client = MonomerMCPClient()

    print("Fetching Cereal Delusion plates...")
    plates_data = client.call_tool("list_plates", {
        "plate_filters": [{"field": "plate_barcode", "operator": {"operator_type": "contains_string", "value": PLATE_PREFIX}}],
    })
    plates = plates_data.get("items", []) if plates_data else []

    if not plates:
        print("No plates found.")
        return

    print(f"Found {len(plates)} plate(s).")
    new_count = 0

    for plate in plates:
        plate_id = plate["id"]
        barcode = plate["barcode"]
        plate_dir = images_dir / barcode
        plate_dir.mkdir(parents=True, exist_ok=True)

        print(f"\nPlate: {barcode}")

        # Get observations
        obs_data = client.call_tool("get_plate_observations", {"plate_id": plate_id})
        if not obs_data:
            print("  No observations found.")
            continue

        items = obs_data.get("result", obs_data).get("items", []) if isinstance(obs_data, dict) else []
        if not items and isinstance(obs_data, dict):
            items = [obs_data]
        datasets = []
        for item in items:
            datasets.extend(item.get("datasets", []))

        # Get cultures
        cultures_data = client.call_tool("list_cultures", {"plate_id": plate_id})
        cultures = cultures_data.get("items", []) if cultures_data else []
        print(f"  {len(cultures)} culture(s), {len(datasets)} dataset(s)")

        # Use the latest dataset only
        latest_dataset = datasets[-1]
        dataset_id = latest_dataset["dataset_id"]

        for culture in cultures:
            culture_id = culture["id"]
            well = culture["well"]
            filename = f"{well}.jpg"
            dest = plate_dir / filename

            if dest.exists():
                print(f"  Skipping {filename} (exists)")
                continue

            print(f"  Downloading {filename}...", end=" ")
            try:
                access = client.call_tool("get_observation_image_access", {
                    "culture_id": culture_id,
                    "dataset_id": dataset_id,
                })
                if isinstance(access, dict) and "download_urls" in access:
                    url = access["download_urls"].get("large_url") or access["download_urls"].get("standard_url")
                    if url:
                        download_image(url, dest)
                        print("OK")
                        new_count += 1
                    else:
                        print("no URL")
                else:
                    print("no access")
            except Exception as e:
                print(f"error: {e}")

    print(f"\nDone. {new_count} new image(s) downloaded.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fetch new observation images from Monomer Cloud MCP for Cereal Delusion plates."""

import json
import os
import platform
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

MCP_URL = "https://backend-staging.monomerbio.com/mcp"
TOKEN_URL = "https://backend-staging.monomerbio.com/token"
PLATE_PREFIX = "Cereal"


def find_credentials_path():
    """Find .claude/.credentials.json across platforms."""
    candidates = []

    # Standard: ~/.claude/.credentials.json
    candidates.append(Path.home() / ".claude" / ".credentials.json")

    # Windows: %APPDATA%/claude/.credentials.json
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "claude" / ".credentials.json")

    # Windows: %LOCALAPPDATA%/claude/.credentials.json
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "claude" / ".credentials.json")

    # Windows: %USERPROFILE%/.claude/.credentials.json
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / ".claude" / ".credentials.json")

    for path in candidates:
        if path.exists():
            return path

    print("Could not find Claude credentials file. Searched:")
    for p in candidates:
        print(f"  {p}")
    print("\nMake sure you have authenticated with Monomer Cloud via Claude Code:")
    print("  claude mcp add --scope user --transport http monomer-cloud https://backend-staging.monomerbio.com/mcp")
    sys.exit(1)


def find_mcp_key(creds):
    """Find the monomer-cloud MCP key in credentials (key suffix may vary per user)."""
    mcp_oauth = creds.get("mcpOAuth", {})
    for key in mcp_oauth:
        if key.startswith("monomer-cloud"):
            return key
    print("No monomer-cloud MCP token found in credentials.")
    print("Authenticate first: claude mcp add --scope user --transport http monomer-cloud https://backend-staging.monomerbio.com/mcp")
    sys.exit(1)


class MonomerMCPClient:
    def __init__(self):
        self.credentials_path = find_credentials_path()
        self.creds = json.loads(self.credentials_path.read_text())
        self.mcp_key = find_mcp_key(self.creds)
        self.session_id = None
        self._request_id = 0
        self._initialize()

    def _get_oauth(self):
        return self.creds["mcpOAuth"][self.mcp_key]

    def _refresh_access_token(self):
        oauth = self._get_oauth()
        data = (
            f"grant_type=refresh_token"
            f"&refresh_token={oauth['refreshToken']}"
            f"&client_id={oauth['clientId']}"
        ).encode()
        req = Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req) as resp:
            token_data = json.loads(resp.read())

        oauth["accessToken"] = token_data["access_token"]
        oauth["expiresAt"] = int(time.time() * 1000) + token_data.get("expires_in", 86400) * 1000
        if "refresh_token" in token_data:
            oauth["refreshToken"] = token_data["refresh_token"]
        self.credentials_path.write_text(json.dumps(self.creds))
        print("Refreshed access token.")

    def _get_access_token(self):
        oauth = self._get_oauth()
        if time.time() * 1000 >= oauth["expiresAt"] - 60000:
            print("Access token expired, refreshing...")
            self._refresh_access_token()
        return self._get_oauth()["accessToken"]

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _parse_sse(self, body, request_id):
        """Parse SSE response and extract the JSON-RPC result."""
        for line in body.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if data.get("id") == request_id:
                    return data
        return None

    def _request(self, method, params=None):
        token = self._get_access_token()
        request_id = self._next_id()
        payload = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = Request(MCP_URL, data=json.dumps(payload).encode(), headers=headers)
        with urlopen(req) as resp:
            # Capture session ID from response headers
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            body = resp.read().decode()

        return self._parse_sse(body, request_id)

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "fetch-images", "version": "1.0"},
        })
        if not result or "error" in result:
            print(f"Failed to initialize MCP session: {result}")
            sys.exit(1)
        # Send initialized notification
        self._request("notifications/initialized")

    def call_tool(self, name, arguments=None):
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if not result:
            return None
        if "error" in result:
            print(f"MCP error calling {name}: {result['error']}")
            return None
        # Extract text content from tool result
        content = result.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "text":
                text = item["text"]
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
        return None


def download_image(url, dest):
    req = Request(url)
    with urlopen(req) as resp:
        dest.write_bytes(resp.read())


def folder_name_from_barcode(barcode):
    """Strip the 'Cereal_Delusion_' prefix to get the folder name."""
    prefix = "Cereal_Delusion_"
    if barcode.startswith(prefix):
        return barcode[len(prefix):]
    return barcode


def main():
    images_dir = Path(__file__).parent / "images"

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
        folder = folder_name_from_barcode(barcode)
        plate_dir = images_dir / folder
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
            filename = f"{barcode}_{well}.jpg"
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

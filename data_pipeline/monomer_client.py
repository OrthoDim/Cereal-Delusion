"""Shared Monomer Cloud MCP client.

Handles OAuth authentication (macOS Keychain or file-based credentials),
token refresh, and JSON-RPC over SSE transport.
"""

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

MCP_URL = "https://backend-staging.monomerbio.com/mcp"
TOKEN_URL = "https://backend-staging.monomerbio.com/token"


def _read_keychain_credentials():
    """Read Claude Code credentials from macOS Keychain. Returns dict or None."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials",
             "-a", os.environ.get("USER", ""), "-w"],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout.strip())
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _write_keychain_credentials(creds):
    """Write Claude Code credentials back to macOS Keychain."""
    creds_json = json.dumps(creds)
    user = os.environ.get("USER", "")
    subprocess.run(
        ["security", "delete-generic-password", "-s", "Claude Code-credentials", "-a", user],
        capture_output=True,
    )
    subprocess.run(
        ["security", "add-generic-password", "-s", "Claude Code-credentials",
         "-a", user, "-w", creds_json],
        capture_output=True, check=True,
    )


def find_credentials():
    """Find Claude Code credentials across platforms.

    Returns (creds_dict, file_path_or_None). On macOS, credentials are stored
    in the Keychain so file_path is None. On Windows/Linux, returns the path
    to .credentials.json for write-back on token refresh.
    """
    if platform.system() == "Darwin":
        creds = _read_keychain_credentials()
        if creds:
            print("Loaded credentials from macOS Keychain.")
            return creds, None

    candidates = []
    candidates.append(Path.home() / ".claude" / ".credentials.json")

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "claude" / ".credentials.json")

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "claude" / ".credentials.json")

    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / ".claude" / ".credentials.json")

    for path in candidates:
        if path.exists():
            print(f"Loaded credentials from {path}")
            return json.loads(path.read_text()), path

    searched = ["macOS Keychain (Claude Code-credentials)"] if platform.system() == "Darwin" else []
    searched.extend(str(p) for p in candidates)
    print("Could not find Claude Code credentials. Searched:")
    for s in searched:
        print(f"  {s}")
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
        self.creds, self.credentials_path = find_credentials()
        self.mcp_key = find_mcp_key(self.creds)
        self.session_id = None
        self._request_id = 0
        self._initialize()

    def _get_oauth(self):
        return self.creds["mcpOAuth"][self.mcp_key]

    def _save_credentials(self):
        """Write credentials back to the appropriate store."""
        if self.credentials_path:
            self.credentials_path.write_text(json.dumps(self.creds))
        elif platform.system() == "Darwin":
            _write_keychain_credentials(self.creds)

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
        self._save_credentials()
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
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            body = resp.read().decode()

        return self._parse_sse(body, request_id)

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "cereal-delusion", "version": "1.0"},
        })
        if not result or "error" in result:
            print(f"Failed to initialize MCP session: {result}")
            sys.exit(1)
        self._request("notifications/initialized")

    def call_tool(self, name, arguments=None):
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if not result:
            return None
        if "error" in result:
            print(f"MCP error calling {name}: {result['error']}")
            return None
        content = result.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "text":
                text = item["text"]
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
        return None

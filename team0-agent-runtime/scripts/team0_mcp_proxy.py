#!/usr/bin/env python3
"""Bridge a host's stdio MCP traffic to Team0's authenticated HTTP endpoint.

Hosts start MCP servers as separate processes, before the prompt hooks run, so
a hook cannot populate ``TEAM0_API_KEY`` in the environment of a remote HTTP
MCP server.  This process is the MCP server from the host's perspective and
reloads that host's key when pairing changes and refreshes the host's tool list.
Every host uses it, so Team0's abilities do
not depend on the user having configured an MCP server by hand.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))

from credential_store import load_credential  # noqa: E402
from host_profile import candidate_roots, detect_host_id, host_credential, local_pairing_unavailable_reason  # noqa: E402


DEFAULT_BASE_URL = "https://api.team0.ai/v1"
RUNTIME_VERSION = "0.1.4"
# The Team0 endpoint exposes the modern 2026 adapter for direct runtime calls,
# but its Streamable HTTP MCP handshake currently accepts the legacy versions
# that Codex sends.  Keep the bridge on the negotiated MCP session version.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


def _api_key(host_id: str | None = None) -> str | None:
    host_id = host_id or detect_host_id()
    ambient = os.environ.get("TEAM0_API_KEY") or os.environ.get("TEAM0_ACCESS_KEY")
    if ambient and host_id == "openclaw":
        return ambient
    # One host never borrows another host's grant: the key is that host's
    # identity in Team0, and its reads and contributions are attributed to it.
    saved = host_credential(
        host_id, loader=lambda root=None: load_credential(root=root)
    )
    if not saved:
        return ambient or None
    return str(saved.get("key") or "").strip() or ambient or None


def _endpoint() -> str:
    base_url = os.environ.get("TEAM0_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return f"{base_url}/mcp"


def _sse_payloads(raw: bytes) -> list[bytes]:
    payloads: list[bytes] = []
    data_lines: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line and data_lines:
            payloads.append("\n".join(data_lines).encode("utf-8"))
            data_lines = []
    if data_lines:
        payloads.append("\n".join(data_lines).encode("utf-8"))
    return [payload for payload in payloads if payload and payload != b"[DONE]"]


class McpHttpBridge:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session_id: str | None = None
        self._protocol_version: str | None = None

    def _version_for(self, message: Mapping[str, Any]) -> str:
        """Use the version selected by initialize for every MCP request."""

        if message.get("method") == "initialize":
            params = message.get("params")
            requested = params.get("protocolVersion") if isinstance(params, Mapping) else None
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
                self._protocol_version = requested
        return self._protocol_version or PROTOCOL_VERSION

    def forward(self, message: Mapping[str, Any]) -> list[bytes]:
        body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._version_for(message),
            "User-Agent": f"team0-agent-runtime/{RUNTIME_VERSION}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = Request(_endpoint(), data=body, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=90) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                raw = response.read()
                content_type = response.headers.get("Content-Type", "").lower()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"Team0 MCP HTTP {error.code}: {detail}") from None
        except (OSError, URLError, TimeoutError) as error:
            raise RuntimeError(f"Team0 MCP unavailable: {error}") from None
        if not raw:
            return []
        if "text/event-stream" in content_type:
            return _sse_payloads(raw)
        return [raw]


def _error_response(message: Mapping[str, Any], detail: str) -> bytes:
    response: dict[str, Any] = {
        "jsonrpc": "2.0",
        "error": {"code": -32000, "message": detail},
    }
    if "id" in message:
        response["id"] = message["id"]
    return json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _credential_revision(host_id: str) -> tuple:
    """Watch secret-free pairing metadata, without polling the OS keychain."""
    revisions = []
    for root in candidate_roots(host_id):
        for name in ("connection.json", "credential.bin"):
            path = root / name
            try:
                stat = path.stat()
                revisions.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                revisions.append((str(path), None, None))
    return tuple(revisions)


class PairingAwareMcpBridge:
    """Stay discoverable before consent; never retain a pre-pairing identity."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self._revision: tuple | None = None
        self._key: str | None = None
        self._bridge: McpHttpBridge | None = None
        self._initialize: Mapping[str, Any] | None = None
        self._remote_initialized = False
        self.refresh()

    def refresh(self) -> bool:
        revision = _credential_revision(self.host_id)
        if revision == self._revision:
            return False
        self._revision = revision
        key = _api_key(self.host_id)
        if key == self._key:
            return False
        self._key = key
        self._bridge = McpHttpBridge(key) if key else None
        self._remote_initialized = False
        return self._initialize is not None

    @staticmethod
    def _result(message: Mapping[str, Any], result: Mapping[str, Any]) -> list[bytes]:
        return [json.dumps({
            "jsonrpc": "2.0", "id": message["id"], "result": result,
        }, separators=(",", ":")).encode("utf-8")]

    def forward(self, message: Mapping[str, Any]) -> list[bytes]:
        method = message.get("method")
        if method == "initialize":
            self._initialize = message
            payloads = []
            if self._bridge:
                try:
                    payloads = self._bridge.forward(message)
                except RuntimeError as error:
                    if not str(error).startswith(("Team0 MCP HTTP 401:", "Team0 MCP HTTP 403:")):
                        raise
                    # A revoked startup key must not make the host disconnect
                    # before the SessionStart hook can replace it through consent.
                    self._bridge = None
            if not self._bridge:
                params = message.get("params") or {}
                version = params.get("protocolVersion")
                return self._result(message, {
                    "protocolVersion": version if version in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "team0-agent-runtime", "version": RUNTIME_VERSION},
                    "instructions": local_pairing_unavailable_reason(self.host_id) or "Team0 awaits browser approval. Do not substitute another connector or account. Tools refresh after pairing.",
                })
            for index, payload in enumerate(payloads):
                response = json.loads(payload)
                if isinstance(response.get("result"), dict):
                    response["result"].setdefault("capabilities", {}).setdefault("tools", {})["listChanged"] = True
                    payloads[index] = json.dumps(response, separators=(",", ":")).encode("utf-8")
                    self._remote_initialized = True
            return payloads

        if method == "notifications/initialized":
            return self._bridge.forward(message) if self._remote_initialized else []
        if not self._bridge:
            if "id" not in message:
                return []
            if method == "tools/list":
                return self._result(message, {"tools": []})
            if method == "ping":
                return self._result(message, {})
            return [_error_response(message, local_pairing_unavailable_reason(self.host_id) or "Approve this host's Team0 connection in the browser first; do not use another connector's grant.")]

        if not self._remote_initialized:
            if not self._initialize:
                return [_error_response(message, "Initialize the Team0 MCP session first.")]
            payloads = self._bridge.forward(self._initialize)
            if not any("result" in json.loads(payload) for payload in payloads):
                return [_error_response(message, "Team0 MCP initialization failed; retry after connecting.")]
            self._bridge.forward({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._remote_initialized = True
        return self._bridge.forward(message)


def main() -> int:
    bridge = PairingAwareMcpBridge(detect_host_id())
    incoming: queue.Queue[str | None] = queue.Queue()

    def read_input() -> None:
        try:
            for line in sys.stdin:
                incoming.put(line)
        finally:
            incoming.put(None)

    threading.Thread(target=read_input, daemon=True).start()
    while True:
        # Pairing runs in a separate hook process. Notify even while the host
        # is idle, otherwise its initial empty tool list stays cached forever.
        if bridge.refresh():
            sys.stdout.buffer.write(b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n')
            sys.stdout.buffer.flush()
        try:
            line = incoming.get(timeout=1)
        except queue.Empty:
            continue
        if line is None:
            break
        if not line.strip():
            continue
        message: Mapping[str, Any] = {}
        try:
            message = json.loads(line)
            if not isinstance(message, Mapping):
                raise ValueError("MCP message must be an object")
            payloads = bridge.forward(message)
        except (json.JSONDecodeError, ValueError, RuntimeError) as error:
            payloads = [_error_response(message, str(error))] if "id" in message else []
        for payload in payloads:
            sys.stdout.buffer.write(payload.rstrip(b"\n") + b"\n")
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

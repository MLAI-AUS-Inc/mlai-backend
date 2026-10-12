"""Validate MCP 2.0 routing metadata against the authenticated request body."""

import base64
import binascii


def headers_match(headers, method, params):
    """Reject missing, malformed or contradictory standard MCP 2.0 headers.

    Body fields remain the authority. Matching the mirrored transport fields
    avoids routing one operation through an intermediary and executing another.
    No tool in this pilot advertises custom ``x-mcp-header`` parameters.
    """
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        return False
    if (headers.get("MCP-Protocol-Version") != "2026-07-28"
            or meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28"
            or headers.get("Mcp-Method") != method):
        return False
    if method not in {"tools/call", "resources/read", "prompts/get"}:
        return True
    name = params.get("uri" if method == "resources/read" else "name")
    mirrored = headers.get("Mcp-Name")
    if not isinstance(name, str) or not isinstance(mirrored, str) or len(mirrored) > 65536:
        return False
    if any(ord(character) < 32 or ord(character) > 126 for character in mirrored):
        return False
    if mirrored.startswith("=?base64?") and mirrored.endswith("?="):
        try:
            mirrored = base64.b64decode(mirrored[9:-2], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
    return mirrored == name

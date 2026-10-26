"""Tiny stdio MCP server used by the integration tests."""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "delete_item",
        "description": "Delete an item",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}},
    },
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        args = msg["params"]["arguments"]
        result = {"content": [{"type": "text", "text": f"echo: {args.get('text', args)}"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()

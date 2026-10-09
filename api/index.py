"""
Vercel Serverless Entrypoint for One Option Google Sheets MCP Server.
Provides:
- Health check & discovery at GET /
- Server-Sent Events (SSE) at GET /sse and POST /messages
- Streamable HTTP (MCP) at POST/GET /mcp
"""

import sys
import os

# Ensure parent directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from main import mcp

# Disable DNS rebinding check so remote clients and Vercel domains are accepted
if hasattr(mcp.settings, "transport_security") and mcp.settings.transport_security:
    mcp.settings.transport_security.enable_dns_rebinding_protection = False


async def root_status(request):
    return JSONResponse({
        "status": "online",
        "name": "one-option-sheets-mcp",
        "description": "MCP Server for One Option Manpower Consultancy (Google Sheets)",
        "endpoints": {
            "streamable_http": "/mcp",
            "sse": "/sse",
            "messages": "/messages"
        },
        "instructions": (
            "For Gemini Enterprise / Connected Apps: use https://<your-domain>/mcp. "
            "For SSE-based MCP clients: use https://<your-domain>/sse."
        )
    })


# Collect routes from FastMCP's SSE app and Streamable HTTP app
sse_routes = mcp.sse_app().routes
stream_routes = mcp.streamable_http_app().routes

routes = [
    Route("/", endpoint=root_status, methods=["GET"]),
    *sse_routes,
    *stream_routes
]

app = Starlette(debug=False, routes=routes)

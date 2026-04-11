#!/usr/bin/env python3
"""
MAVLink MCP Server - HTTP/SSE Transport + Internal API

Runs the MCP SSE transport alongside internal REST/WebSocket
endpoints for dashboard-api consumption:
  - GET  /api/telemetry   — TelemetryService snapshot
  - GET  /api/activity    — FlightActivity + telemetry
  - GET  /api/mission     — Current mission state
  - GET  /api/health      — System health
  - WS   /ws/telemetry    — Push telemetry at 2Hz
  - /sse, /messages/      — MCP SSE transport (unchanged)
"""

import sys
import os
import asyncio
import json
import time
import logging
from pathlib import Path
from contextlib import asynccontextmanager

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Configuration
PORT = int(os.environ.get("MCP_PORT", "8080"))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")

# Load environment variables
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# Now import after env vars are set
from src.server.droneserver import (
    logger, mcp, get_or_create_global_connector, LogColors,
)

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect


# ============================================================
# Accessor for global connector (avoids stale module-level ref)
# ============================================================

def _connector():
    """Get the current global connector (re-imports to avoid stale reference)."""
    from src.server.droneserver import _global_connector
    return _global_connector


# ============================================================
# REST Endpoints
# ============================================================

async def api_telemetry(request):
    """GET /api/telemetry — TelemetryService snapshot."""
    conn = _connector()
    if conn is None or conn.telemetry is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    snapshot = conn.telemetry.get_snapshot()
    return JSONResponse({"status": "ok", **snapshot})


async def api_activity(request):
    """GET /api/activity — current FlightActivity + telemetry."""
    conn = _connector()
    if conn is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)

    telemetry = conn.telemetry.get_snapshot() if conn.telemetry else {}

    activity = conn.current_activity
    activity_data = None
    if activity:
        activity_data = {
            "id": activity.id,
            "type": activity.type,
            "status": activity.status,
            "started_at": activity.started_at,
            "completed_at": activity.completed_at,
            "description": activity.description,
            "command_tool": activity.command_tool,
            "waypoint_count": activity.waypoint_count,
            "total_distance_m": activity.total_distance_m,
            "speed_m_s": activity.speed_m_s,
            "altitude_m": activity.altitude_m,
            "mission_id": activity.mission_id,
        }

    return JSONResponse({
        "status": "ok",
        "telemetry": telemetry,
        "activity": activity_data,
    })


async def api_mission(request):
    """GET /api/mission — current mission state."""
    conn = _connector()
    if conn is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)

    mission = conn.current_mission
    if mission is None:
        return JSONResponse({"status": "ok", "mission": None})
    return JSONResponse({"status": "ok", **mission.to_dict()})


async def api_health(request):
    """GET /api/health — overall system health for dashboard."""
    conn = _connector()
    connected = conn is not None and conn.connection_ready.is_set()
    perception_url = os.environ.get("PERCEPTION_URL", "http://localhost:8090")
    return JSONResponse({
        "status": "ok",
        "drone_connected": connected,
        "perception_url": perception_url,
        "mcp_port": PORT,
    })


# ============================================================
# WebSocket Endpoints
# ============================================================

async def ws_telemetry(websocket: WebSocket):
    """WS /ws/telemetry — push telemetry + activity + mission at 2Hz."""
    await websocket.accept()
    logger.info(f"{LogColors.HTTP}WS /ws/telemetry connected{LogColors.RESET}")

    try:
        while True:
            conn = _connector()
            if conn and conn.telemetry:
                snapshot = conn.telemetry.get_snapshot()

                activity = conn.current_activity
                activity_data = None
                if activity:
                    activity_data = {
                        "id": activity.id,
                        "type": activity.type,
                        "status": activity.status,
                        "description": activity.description,
                    }

                mission = conn.current_mission
                mission_data = mission.to_dict() if mission else None

                payload = {
                    "ts": time.time(),
                    "telemetry": snapshot,
                    "activity": activity_data,
                    "mission": mission_data,
                }
            else:
                payload = {"ts": time.time(), "telemetry": None, "status": "not_ready"}

            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(0.5)  # 2Hz

    except WebSocketDisconnect:
        logger.info(f"{LogColors.HTTP}WS /ws/telemetry disconnected{LogColors.RESET}")
    except Exception as e:
        logger.warning(f"WS /ws/telemetry error: {e}")


# ============================================================
# Application Factory
# ============================================================

@asynccontextmanager
async def app_lifespan(app):
    """Initialize the global drone connector on startup."""
    logger.info("Initializing drone connection (parent app lifespan)...")
    async with mcp.session_manager.run():
        await get_or_create_global_connector()
        yield
    logger.info("Server shutting down")


def create_app() -> Starlette:
    """Create Starlette app with dual MCP transports plus internal API routes.

    Route priority: explicit /api/* and /ws/* routes first.
    Expose:
      - SSE transport at /sse (+ /messages/*)
      - Streamable HTTP transport at /mcp
    """
    mcp_sse_app = mcp.sse_app()
    mcp_streamable_http_app = mcp.streamable_http_app()

    routes = [
        # Internal API for dashboard-api
        Route("/api/telemetry", api_telemetry),
        Route("/api/activity", api_activity),
        Route("/api/mission", api_mission),
        Route("/api/health", api_health),
        WebSocketRoute("/ws/telemetry", ws_telemetry),
        # MCP streamable HTTP for Codex and other modern clients
        Route("/mcp", endpoint=mcp_streamable_http_app),
        # MCP SSE for Claude/legacy clients — handles /sse and /messages/
        Mount("/", app=mcp_sse_app),
    ]

    return Starlette(routes=routes, lifespan=app_lifespan)


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 60)
    logger.info("MAVLink MCP Server - Dual MCP Transports + Internal API")
    logger.info("=" * 60)
    logger.info(f"  MCP HTTP:    http://{HOST}:{PORT}/mcp")
    logger.info(f"  MCP SSE:     http://{HOST}:{PORT}/sse")
    logger.info(f"  Telemetry:   http://{HOST}:{PORT}/api/telemetry")
    logger.info(f"  Activity:    http://{HOST}:{PORT}/api/activity")
    logger.info(f"  Mission:     http://{HOST}:{PORT}/api/mission")
    logger.info(f"  Health:      http://{HOST}:{PORT}/api/health")
    logger.info(f"  WS Telem:    ws://{HOST}:{PORT}/ws/telemetry")
    logger.info("=" * 60)

    verbose_mode = os.getenv("MAVLINK_VERBOSE", "0") == "1"

    if not verbose_mode:
        class SuppressPollingFilter(logging.Filter):
            """Allow /api/ and /ws/ access logs, suppress MCP SSE polling."""
            def filter(self, record):
                msg = record.getMessage()
                if "/api/" in msg or "/ws/" in msg:
                    return True
                return False

        uvicorn_access = logging.getLogger("uvicorn.access")
        uvicorn_access.addFilter(SuppressPollingFilter())

        mcp_server = logging.getLogger("mcp.server")
        mcp_server.setLevel(logging.WARNING)

        logger.info("HTTP access logs suppressed (set MAVLINK_VERBOSE=1 to re-enable)")

    app = create_app()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")

# Droneserver — Codex Context

MCP server for LLM-to-drone control via MAVLink. Fork of PeterJBurke/droneserver.

## Architecture
- `src/server/droneserver.py` — core MCP server (~5900 lines). ~67 tools: flight control, mission state, search patterns, safety, vision proxies
- `src/server/perception/` — separate FastAPI service for vision pipeline (camera, YOLO, Claude Vision, image store)
- `src/server/droneserver_http.py` — HTTP entrypoint. Starlette app exposes streamable HTTP MCP at `/mcp`, SSE MCP at `/sse` + `/messages/`, and the internal REST/WS API (`/api/telemetry`, `/api/activity`, `/api/mission`, `/api/health`, `WS /ws/telemetry`) for dashboard-api consumption
- `src/server/perception/app.py` — includes `WS /ws/video/{camera_name}` for JPEG video streaming to dashboard

## Key Patterns
- Vision MCP tools proxy to perception-service via httpx (PERCEPTION_URL env var)
- Core owns findings — perception returns detections, core decides what to add to mission state
- TelemetryService caches 10 MAVSDK streams for instant reads
- cosysairsim conflicts with asyncio — keep AirSim RPC on one dedicated executor thread; spreading calls across arbitrary worker threads breaks stream capture
- MAVSDK async iterators can block forever — wrap in `asyncio.wait_for()` with 5s timeout

## Rules
- Don't add vision/camera imports to droneserver.py — that belongs in perception/
- Keep MCP tool signatures stable — Claude operator relies on them
- Test with both AirSim profile (real cameras) and Gazebo profile (MockSource)

# Add lifespan support for startup/shutdown with strong typing
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from mcp.server.fastmcp import Context, FastMCP
from typing import Tuple
from mavsdk import System
from mavsdk.mission_raw import MissionItem
from mavsdk.geofence import Point as GeoPoint, Polygon as GeoPolygon, FenceType, GeofenceData
from enum import Enum
import asyncio
import os
import logging
import math
import uuid
import time
import json
import base64
import io
from dotenv import load_dotenv
from pathlib import Path

# Optional vision dependencies — graceful degradation if not installed
try:
    import cosysairsim as airsim
    AIRSIM_AVAILABLE = True
except ImportError:
    AIRSIM_AVAILABLE = False

try:
    import numpy as np
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# Load environment variables from .env file
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# Configure logger with clean single-line format for systemd
logger = logging.getLogger("droneserver")
logger.setLevel(logging.INFO)

# Remove any existing handlers to avoid duplicates
logger.handlers.clear()

# Single-line format for clean journalctl output
console_handler = logging.StreamHandler()
# Compact format: timestamp | level | message (no logger name, no multi-line)
console_formatter = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# Prevent propagation to avoid duplicate logs from parent loggers
logger.propagate = False

# Note: HTTP/framework log suppression is done in droneserver_http.py
# (must be set right before server start to prevent uvicorn from overriding)

# Ensure output is unbuffered for systemd journalctl
import sys
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, 'reconfigure') else None

# ============================================================
# Flight Logging System
# ============================================================
from datetime import datetime

# ANSI color codes for terminal output
class LogColors:
    """ANSI color codes for colored terminal output (dark/normal variants)"""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    
    # Dark colors (3x codes) - easier to read than bright (9x codes)
    RED = '\033[31m'      # Dark red
    GREEN = '\033[32m'    # Dark green
    YELLOW = '\033[33m'   # Dark yellow/orange
    BLUE = '\033[34m'     # Dark blue
    MAGENTA = '\033[35m'  # Dark magenta
    CYAN = '\033[36m'     # Dark cyan
    WHITE = '\033[37m'    # Light gray
    GRAY = '\033[90m'     # Dark gray for separators
    
    # Combined styles for specific log types
    MAVLINK = '\033[36m'  # Dark cyan for MAVLink commands
    TOOL = '\033[32m'     # Dark green for MCP tool calls
    ERROR = '\033[31m'    # Dark red for errors
    HTTP = '\033[35m'     # Dark magenta for HTTP requests (GET/POST)
    STATUS = '\033[94m'   # Bright blue for drone status/responses
    SUCCESS = '\033[92m'  # Bright green for success messages (✓)
    SEPARATOR = '\033[90m'  # Dark gray for visual separators

class FlightLogger:
    """Logs flight operations to a timestamped file"""
    def __init__(self):
        self.log_dir = Path(__file__).parent.parent.parent / "flight_logs"
        self.log_dir.mkdir(exist_ok=True)
        
        # Create log file with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"flight_{timestamp}.log"
        
        # Write header
        with open(self.log_file, 'w') as f:
            f.write("=" * 80 + "\n")
            f.write(f"MAVLink MCP Flight Log\n")
            f.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
        
        logger.info(f"✈️ Flight log created: {self.log_file}")
    
    def log_entry(self, entry_type: str, message: str):
        """Write a timestamped entry to the log file"""
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # Include milliseconds
        try:
            with open(self.log_file, 'a') as f:
                f.write(f"[{timestamp}] {entry_type}: {message}\n")
        except Exception as e:
            logger.error(f"{LogColors.ERROR}Failed to write to flight log: {e}{LogColors.RESET}")

# Global flight logger instance
_flight_logger: FlightLogger | None = None

def get_flight_logger() -> FlightLogger:
    """Get or create the global flight logger"""
    global _flight_logger
    if _flight_logger is None:
        _flight_logger = FlightLogger()
    return _flight_logger

def log_tool_call(tool_name: str, **kwargs):
    """Log MCP tool call with parameters (GREEN) with visual separator"""
    # Add visual separator before each tool call
    logger.info(f"{LogColors.SEPARATOR}{'─' * 60}{LogColors.RESET}")
    
    if kwargs:
        params_str = ", ".join([f"{k}={v}" for k, v in kwargs.items() if v is not None])
        msg = f"{tool_name}({params_str})"
        logger.info(f"{LogColors.TOOL}🔧 MCP TOOL: {msg}{LogColors.RESET}")
        # Log input JSON
        import json
        logger.info(f"{LogColors.TOOL}📥 INPUT: {json.dumps(kwargs, default=str)}{LogColors.RESET}")
        get_flight_logger().log_entry("MCP_TOOL", msg)
    else:
        msg = f"{tool_name}()"
        logger.info(f"{LogColors.TOOL}🔧 MCP TOOL: {msg}{LogColors.RESET}")
        logger.info(f"{LogColors.TOOL}📥 INPUT: {{}}{LogColors.RESET}")
        get_flight_logger().log_entry("MCP_TOOL", msg)

def log_tool_output(output: dict):
    """Log MCP tool output as JSON (GREEN)"""
    import json
    logger.info(f"{LogColors.TOOL}📤 OUTPUT: {json.dumps(output, default=str, indent=2)}{LogColors.RESET}")

def log_mavlink_cmd(command: str, **kwargs):
    """Log MAVLink command being sent to drone (CYAN)"""
    if kwargs:
        params_str = ", ".join([f"{k}={v}" for k, v in kwargs.items() if v is not None])
        msg = f"{command}({params_str})"
        logger.info(f"{LogColors.MAVLINK}📡 MAVLink → {msg}{LogColors.RESET}")
        get_flight_logger().log_entry("MAVLink_CMD", msg)
    else:
        msg = f"{command}()"
        logger.info(f"{LogColors.MAVLINK}📡 MAVLink → {msg}{LogColors.RESET}")
        get_flight_logger().log_entry("MAVLink_CMD", msg)

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the distance between two GPS coordinates using the Haversine formula.
    
    Args:
        lat1, lon1: First point (latitude, longitude in degrees)
        lat2, lon2: Second point (latitude, longitude in degrees)
        
    Returns:
        Distance in meters
    """
    R = 6371000  # Earth's radius in meters
    
    # Convert to radians
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

class MissionStatus(Enum):
    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"

class SectorStatus(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SKIPPED = "skipped"

@dataclass
class Sector:
    id: str
    bounds: dict
    status: SectorStatus = SectorStatus.PENDING
    waypoints: list = field(default_factory=list)
    waypoint_index_range: tuple = field(default=(0, 0))  # (start, end) indices into mission waypoint list
    started_at: float | None = None
    completed_at: float | None = None

@dataclass
class Finding:
    id: str
    type: str
    lat: float
    lon: float
    confidence: float
    metadata: dict = field(default_factory=dict)
    image_ref: str | None = None
    timestamp: float = field(default_factory=time.time)

@dataclass
class Decision:
    trigger: str
    action: str
    rationale: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class MissionState:
    id: str
    type: str
    status: MissionStatus
    objective: str
    area: dict
    params: dict = field(default_factory=dict)
    sectors: list[Sector] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Full JSON-serializable state for get_mission_state()"""
        sectors_completed = sum(1 for s in self.sectors if s.status == SectorStatus.COMPLETED)
        sectors_total = len(self.sectors)
        coverage_pct = round((sectors_completed / sectors_total * 100), 1) if sectors_total > 0 else 0.0
        return {
            "mission": {
                "id": self.id,
                "type": self.type,
                "status": self.status.value,
                "objective": self.objective,
                "area": self.area,
                "params": self.params,
            },
            "progress": {
                "sectors_total": sectors_total,
                "sectors_completed": sectors_completed,
                "sectors_remaining": sectors_total - sectors_completed,
                "coverage_pct": coverage_pct,
                "elapsed_s": round(time.time() - self.created_at, 1),
            },
            "sectors": [
                {
                    "id": s.id,
                    "status": s.status.value,
                    "bounds": s.bounds,
                    "waypoint_index_range": list(s.waypoint_index_range),
                }
                for s in self.sectors
            ],
            "findings": [
                {
                    "id": f.id,
                    "type": f.type,
                    "lat": f.lat,
                    "lon": f.lon,
                    "confidence": f.confidence,
                    "metadata": f.metadata,
                    "image_ref": f.image_ref,
                    "timestamp": f.timestamp,
                }
                for f in self.findings
            ],
            "decisions": [
                {
                    "trigger": d.trigger,
                    "action": d.action,
                    "rationale": d.rationale,
                    "timestamp": d.timestamp,
                }
                for d in self.decisions
            ],
        }

    def summary(self) -> str:
        """Natural language summary for get_mission_summary()"""
        sectors_completed = sum(1 for s in self.sectors if s.status == SectorStatus.COMPLETED)
        sectors_total = len(self.sectors)
        active_sectors = [s for s in self.sectors if s.status == SectorStatus.ACTIVE]
        elapsed = round(time.time() - self.created_at)

        lines = [
            f"Mission {self.id} ({self.type}): {self.status.value}",
            f"Objective: {self.objective}",
            f"Progress: {sectors_completed}/{sectors_total} sectors completed "
            f"({round(sectors_completed / sectors_total * 100, 1) if sectors_total else 0}%)",
        ]
        if active_sectors:
            lines.append(f"Currently searching: sector {active_sectors[0].id}")
        if self.findings:
            lines.append(f"Findings: {len(self.findings)} points of interest logged")
            for f in self.findings[-3:]:  # Last 3 findings
                lines.append(f"  - {f.type} at ({f.lat:.5f}, {f.lon:.5f}) confidence={f.confidence}")
        if self.decisions:
            last = self.decisions[-1]
            lines.append(f"Last decision: {last.action} (trigger: {last.trigger})")
        lines.append(f"Elapsed: {elapsed}s")
        return "\n".join(lines)


# Image store for vision pipeline (module-level, persists across requests)
# Stores metadata + optional PNG bytes. LRU eviction at _IMAGE_STORE_MAX_BYTES.
_image_store: dict[str, dict] = {}
_image_store_bytes: int = 0
_IMAGE_STORE_MAX_BYTES: int = 500 * 1024 * 1024  # 500MB cap

def _image_store_put(image_ref: str, meta: dict):
    """Add an image to the store with LRU eviction if over budget."""
    global _image_store_bytes
    png_size = len(meta.get("png_bytes", b""))
    # Evict oldest entries if over budget
    while _image_store_bytes + png_size > _IMAGE_STORE_MAX_BYTES and _image_store:
        oldest_key = next(iter(_image_store))
        oldest = _image_store.pop(oldest_key)
        _image_store_bytes -= len(oldest.get("png_bytes", b""))
    _image_store[image_ref] = meta
    _image_store_bytes += png_size


# ============================================================
# YOLO Model Loader (lazy, thread-safe)
# ============================================================
_yolo_model = None
_yolo_lock = asyncio.Lock()

def _get_yolo_model():
    """Load YOLO model on first call. Model auto-downloads from Ultralytics Hub (~6MB for nano)."""
    global _yolo_model
    if _yolo_model is None:
        model_name = os.environ.get("YOLO_MODEL", "yolo11n.pt")
        logger.info(f"{LogColors.STATUS}Loading YOLO model: {model_name}{LogColors.RESET}")
        _yolo_model = YOLO(model_name)
        logger.info(f"{LogColors.SUCCESS}YOLO model loaded{LogColors.RESET}")
    return _yolo_model


def _run_yolo(png_bytes: bytes, confidence: float = 0.3) -> list[dict]:
    """Run YOLO inference on PNG bytes. Returns list of detections."""
    img_array = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None:
        return []
    model = _get_yolo_model()
    results = model(img, conf=confidence, verbose=False)
    detections = []
    for r in results:
        for box in r.boxes:
            detections.append({
                "class": r.names[int(box.cls[0])],
                "confidence": round(float(box.conf[0]), 3),
                "bbox": [round(float(x), 1) for x in box.xyxy[0].tolist()],
            })
    return detections


# ============================================================
# Claude Vision Helper (async)
# ============================================================
async def _claude_vision_analyze(png_bytes: bytes, prompt: str,
                                  detections: list[dict],
                                  position: dict) -> dict:
    """Send image + context to Claude Vision API for reasoning.

    Returns structured JSON with description, findings, and recommendations.
    """
    model = os.environ.get("CLAUDE_VISION_MODEL", "claude-haiku-4-5")
    client = anthropic.AsyncAnthropic()  # Uses ANTHROPIC_API_KEY env var

    # Build context
    context_parts = []
    if position:
        context_parts.append(f"Drone position: lat={position.get('latitude_deg')}, lon={position.get('longitude_deg')}, alt={position.get('relative_altitude_m')}m")
    if detections:
        det_summary = ", ".join(f"{d['class']} ({d['confidence']:.0%})" for d in detections[:10])
        context_parts.append(f"YOLO detections: {det_summary}")

    system_prompt = (
        "You are analyzing aerial drone imagery for a search/survey mission. "
        "Respond with JSON only. Schema: "
        '{"description": "...", "findings": [{"type": "...", "description": "...", '
        '"confidence": 0.0-1.0, "severity": "low|medium|high|critical"}], '
        '"recommendation": "..."}'
    )

    user_content = []
    # Add image
    img_b64 = base64.b64encode(png_bytes).decode("utf-8")
    user_content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
    })
    # Add text prompt
    text = "\n".join(context_parts)
    if prompt:
        text += f"\n\nUser request: {prompt}"
    else:
        text += "\n\nDescribe what you see and flag any anomalies or objects of interest."
    user_content.append({"type": "text", "text": text})

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    # Parse JSON from response
    response_text = response.content[0].text
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown code block
        if "```" in response_text:
            json_str = response_text.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
            return json.loads(json_str.strip())
        return {"description": response_text, "findings": [], "recommendation": ""}


# ============================================================
# Geometry Helpers (pure functions for search pattern generation)
# ============================================================

def offset_gps(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Offset a GPS coordinate by meters north and east.
    Uses flat-earth approximation (111320 m/deg). Accurate for <10km offsets.
    """
    new_lat = lat + north_m / 111320.0
    new_lon = lon + east_m / (111320.0 * math.cos(math.radians(lat)))
    return (new_lat, new_lon)


def generate_grid_waypoints(
    bounds: dict, altitude: float, spacing: float
) -> tuple[list[dict], list[Sector]]:
    """Generate lawnmower (boustrophedon) grid search waypoints.

    Args:
        bounds: dict with keys north, south, east, west (lat/lon degrees)
        altitude: flight altitude in meters (relative)
        spacing: distance between passes in meters

    Returns:
        (waypoints, sectors) — waypoints are dicts with lat/lon/alt,
        sectors correspond to individual passes with waypoint index ranges.
    """
    north, south = bounds["north"], bounds["south"]
    east, west = bounds["east"], bounds["west"]

    # Area dimensions in meters
    height_m = haversine_distance(south, west, north, west)
    width_m = haversine_distance(south, west, south, east)

    num_passes = max(1, math.ceil(width_m / spacing))
    actual_spacing = width_m / num_passes

    waypoints = []
    sectors = []
    wp_index = 0

    for i in range(num_passes + 1):
        sector_start = wp_index
        east_offset = actual_spacing * i

        if i % 2 == 0:
            # South to North
            start = offset_gps(south, west, 0, east_offset)
            end = offset_gps(south, west, height_m, east_offset)
        else:
            # North to South
            start = offset_gps(south, west, height_m, east_offset)
            end = offset_gps(south, west, 0, east_offset)

        wp_start = {"latitude_deg": start[0], "longitude_deg": start[1], "relative_altitude_m": altitude}
        wp_end = {"latitude_deg": end[0], "longitude_deg": end[1], "relative_altitude_m": altitude}
        waypoints.extend([wp_start, wp_end])
        wp_index += 2

        sector_bounds = {
            "south": min(start[0], end[0]),
            "north": max(start[0], end[0]),
            "west": min(start[1], end[1]),
            "east": max(start[1], end[1]),
        }
        sectors.append(Sector(
            id=f"pass-{i}",
            bounds=sector_bounds,
            waypoints=[wp_start, wp_end],
            waypoint_index_range=(sector_start, wp_index - 1),
        ))

    return waypoints, sectors


def generate_expanding_square_waypoints(
    center_lat: float, center_lon: float, altitude: float,
    initial_size: float, expansion: float, legs: int
) -> tuple[list[dict], list[Sector]]:
    """Generate SAR expanding square search pattern.

    Spiral outward from center in square legs of increasing length.
    Pattern: E, N, W, W, S, S, E, E, E, N, N, N, ... (each direction twice before increasing)

    Args:
        center_lat, center_lon: center of search in degrees
        altitude: flight altitude in meters (relative)
        initial_size: length of first leg in meters
        expansion: how much each pair of legs grows in meters
        legs: total number of legs to fly

    Returns:
        (waypoints, sectors) — each leg is one sector
    """
    # Directions: E, N, W, S (cycle)
    directions = [(0, 1), (1, 0), (0, -1), (-1, 0)]  # (north_mult, east_mult)

    waypoints = []
    sectors = []
    current_lat, current_lon = center_lat, center_lon

    # Start point
    waypoints.append({
        "latitude_deg": current_lat,
        "longitude_deg": current_lon,
        "relative_altitude_m": altitude,
    })
    wp_index = 1

    leg_length = initial_size
    for i in range(legs):
        dir_idx = i % 4
        north_mult, east_mult = directions[dir_idx]

        north_m = north_mult * leg_length
        east_m = east_mult * leg_length

        new_lat, new_lon = offset_gps(current_lat, current_lon, north_m, east_m)

        sector_start = wp_index
        waypoints.append({
            "latitude_deg": new_lat,
            "longitude_deg": new_lon,
            "relative_altitude_m": altitude,
        })
        wp_index += 1

        sectors.append(Sector(
            id=f"leg-{i}",
            bounds={
                "south": min(current_lat, new_lat),
                "north": max(current_lat, new_lat),
                "west": min(current_lon, new_lon),
                "east": max(current_lon, new_lon),
            },
            waypoints=[waypoints[-1]],
            waypoint_index_range=(sector_start, sector_start),
        ))

        current_lat, current_lon = new_lat, new_lon
        # Increase leg length every 2 legs
        if i % 2 == 1:
            leg_length += expansion

    return waypoints, sectors


def generate_sector_search_waypoints(
    center_lat: float, center_lon: float, radius: float,
    altitude: float, num_sectors: int
) -> tuple[list[dict], list[Sector]]:
    """Generate pie-slice sector search pattern.

    Fly center → perimeter → arc sweep → center for each sector.

    Args:
        center_lat, center_lon: center of search area in degrees
        radius: search radius in meters
        altitude: flight altitude in meters (relative)
        num_sectors: number of pie slices

    Returns:
        (waypoints, sectors) — each pie slice is one sector
    """
    waypoints = []
    sectors = []
    wp_index = 0
    angle_step = 360.0 / num_sectors

    for i in range(num_sectors):
        sector_start = wp_index
        sector_wps = []

        # Center point
        center_wp = {
            "latitude_deg": center_lat,
            "longitude_deg": center_lon,
            "relative_altitude_m": altitude,
        }
        sector_wps.append(center_wp)

        # Points along the arc for this sector (start angle to end angle)
        start_angle = i * angle_step
        end_angle = (i + 1) * angle_step
        arc_points = 3  # start, mid, end of arc

        for j in range(arc_points):
            angle_deg = start_angle + (end_angle - start_angle) * j / (arc_points - 1)
            angle_rad = math.radians(angle_deg)
            north_m = radius * math.cos(angle_rad)
            east_m = radius * math.sin(angle_rad)
            pt_lat, pt_lon = offset_gps(center_lat, center_lon, north_m, east_m)
            sector_wps.append({
                "latitude_deg": pt_lat,
                "longitude_deg": pt_lon,
                "relative_altitude_m": altitude,
            })

        waypoints.extend(sector_wps)
        wp_index += len(sector_wps)

        # Sector bounds (approximate from waypoints)
        lats = [w["latitude_deg"] for w in sector_wps]
        lons = [w["longitude_deg"] for w in sector_wps]
        sectors.append(Sector(
            id=f"sector-{i}",
            bounds={
                "south": min(lats),
                "north": max(lats),
                "west": min(lons),
                "east": max(lons),
            },
            waypoints=sector_wps,
            waypoint_index_range=(sector_start, wp_index - 1),
        ))

    return waypoints, sectors


@dataclass
class FlightActivity:
    """Always-on flight tracker. Created by every flight action."""
    id: str                                    # "flight-{uuid8}"
    type: str                                  # "goto" | "waypoint_route" | "orbit" | "search" | "rtl" | "land" | "idle"
    status: str                                # "active" | "completed" | "aborted"
    started_at: float
    completed_at: float | None = None
    description: str = ""                      # Human-readable: "Grid search: 12 passes over solar farm"
    command_tool: str = ""                     # MCP tool name that started this
    # Navigation (goto)
    destination: dict | None = None            # {lat, lon, alt_msl, initial_distance_m}
    # Waypoint missions (route, orbit, search)
    waypoint_count: int = 0
    total_distance_m: float = 0.0
    estimated_time_s: float = 0.0
    speed_m_s: float = 0.0
    altitude_m: float = 0.0
    # Landing state machine (migrated from landing_in_progress bool)
    landing_initiated: bool = False
    # Link to mission intelligence
    mission_id: str | None = None


@dataclass
class TelemetryCacheEntry:
    """Single cached telemetry value with timestamp."""
    value: object = None
    updated_at: float = 0.0


class TelemetryService:
    """Persistent MAVSDK stream subscriptions with in-memory cache.

    Subscribes to 10 telemetry streams once at startup. Each stream runs in its
    own asyncio.Task, updating a cache dict on every value. Reads become instant
    dict lookups instead of blocking MAVSDK calls.
    """

    STREAMS = [
        "position", "battery", "flight_mode", "velocity_ned",
        "landed_state", "heading", "in_air", "armed",
        "health", "mission_progress",
    ]
    STALE_THRESHOLD_S = 10.0

    def __init__(self, drone: System):
        self._drone = drone
        self._cache: dict[str, TelemetryCacheEntry] = {
            name: TelemetryCacheEntry() for name in self.STREAMS
        }
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self):
        """Create background tasks for all telemetry streams."""
        logger.info(f"{LogColors.STATUS}TelemetryService: Starting {len(self.STREAMS)} streams...{LogColors.RESET}")
        stream_sources = {
            "position": self._drone.telemetry.position,
            "battery": self._drone.telemetry.battery,
            "flight_mode": self._drone.telemetry.flight_mode,
            "velocity_ned": self._drone.telemetry.velocity_ned,
            "landed_state": self._drone.telemetry.landed_state,
            "heading": self._drone.telemetry.heading,
            "in_air": self._drone.telemetry.in_air,
            "armed": self._drone.telemetry.armed,
            "health": self._drone.telemetry.health,
            "mission_progress": self._drone.mission.mission_progress,
        }
        for name, source_fn in stream_sources.items():
            self._tasks[name] = asyncio.create_task(
                self._stream_loop(name, source_fn),
                name=f"telemetry-{name}",
            )
        logger.info(f"{LogColors.SUCCESS}TelemetryService: All streams launched{LogColors.RESET}")

    async def stop(self):
        """Cancel all stream tasks cleanly."""
        logger.info(f"{LogColors.STATUS}TelemetryService: Stopping...{LogColors.RESET}")
        for name, task in self._tasks.items():
            task.cancel()
        for name, task in self._tasks.items():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info(f"{LogColors.STATUS}TelemetryService: Stopped{LogColors.RESET}")

    async def _stream_loop(self, name: str, source_fn):
        """Subscribe to one MAVSDK stream, updating cache on every value. Auto-reconnects on error."""
        while True:
            try:
                async for value in source_fn():
                    self._cache[name] = TelemetryCacheEntry(
                        value=value, updated_at=time.time()
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"{LogColors.YELLOW}TelemetryService: stream '{name}' error: {e} — reconnecting in 2s{LogColors.RESET}"
                )
                await asyncio.sleep(2.0)

    def get(self, name: str):
        """Return cached MAVSDK object (or None if never received)."""
        entry = self._cache.get(name)
        if entry is None or entry.value is None:
            return None
        age = time.time() - entry.updated_at
        if age > self.STALE_THRESHOLD_S:
            logger.warning(
                f"{LogColors.YELLOW}TelemetryService: '{name}' is stale ({age:.1f}s old){LogColors.RESET}"
            )
        return entry.value

    def get_age(self, name: str) -> float:
        """Seconds since last update for a stream (inf if never received)."""
        entry = self._cache.get(name)
        if entry is None or entry.updated_at == 0.0:
            return float("inf")
        return time.time() - entry.updated_at

    def get_snapshot(self) -> dict:
        """All telemetry as a JSON-serializable dict for tools and future WebSocket push."""
        pos = self.get("position")
        vel = self.get("velocity_ned")
        bat = self.get("battery")
        fm = self.get("flight_mode")
        ls = self.get("landed_state")
        hdg = self.get("heading")
        in_air = self.get("in_air")
        armed = self.get("armed")
        mp = self.get("mission_progress")

        position = None
        if pos:
            position = {
                "lat": round(pos.latitude_deg, 7),
                "lon": round(pos.longitude_deg, 7),
                "alt_relative_m": round(pos.relative_altitude_m, 1),
                "alt_msl_m": round(pos.absolute_altitude_m, 1),
            }

        speed_m_s = None
        if vel:
            speed_m_s = round(math.sqrt(vel.north_m_s**2 + vel.east_m_s**2), 1)

        flight_mode = str(fm).split(".")[-1] if fm else "UNKNOWN"
        landed_state = str(ls).split(".")[-1] if ls else "UNKNOWN"
        is_on_ground = landed_state == "ON_GROUND"

        battery_pct = round(bat.remaining_percent, 1) if bat else -1

        mission_current = mp.current if mp else 0
        mission_total = mp.total if mp else 0

        snapshot = {
            "position": position,
            "flight_mode": flight_mode,
            "battery_pct": battery_pct,
            "speed_m_s": speed_m_s,
            "landed_state": landed_state,
            "is_on_ground": is_on_ground,
            "heading_deg": round(hdg.heading_deg, 1) if hdg else None,
            "in_air": in_air if isinstance(in_air, bool) else None,
            "armed": armed if isinstance(armed, bool) else None,
            "mission_progress": {"current": mission_current, "total": mission_total},
            "_cache_ages_s": {
                name: round(self.get_age(name), 1) for name in self.STREAMS
            },
        }
        return snapshot


@dataclass
class MAVLinkConnector:
    drone: System
    connection_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Unified flight activity tracker (Phase 2E)
    current_activity: FlightActivity | None = field(default=None)
    # Mission state for Phase 2 mission intelligence
    current_mission: MissionState | None = field(default=None)
    # Background battery monitor task
    _battery_monitor_task: asyncio.Task | None = field(default=None)
    # Persistent telemetry cache (TelemetryService)
    telemetry: TelemetryService | None = field(default=None)
    # AirSim client for vision pipeline (None = synthetic stubs)
    airsim_client: object | None = field(default=None)

# Global connector instance - persists across all HTTP requests
_global_connector: MAVLinkConnector | None = None
_connection_task: asyncio.Task | None = None
_connection_lock = asyncio.Lock()
_lifespan_initialized = False  # Track if lifespan has run (to reduce log noise)

async def ensure_connection(connector: MAVLinkConnector, timeout: float = 30.0) -> bool:
    """
    Wait for the drone connection to be ready.
    
    Args:
        connector: The MAVLinkConnector instance
        timeout: Maximum time to wait in seconds
        
    Returns:
        bool: True if connected, False if timeout
    """
    try:
        await asyncio.wait_for(connector.connection_ready.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.error(f"{LogColors.ERROR}❌ Drone connection timeout after {timeout}s{LogColors.RESET}")
        return False

async def connect_drone_background(connector: MAVLinkConnector, address: str, port: str, protocol: str):
    """Connect to drone in the background without blocking server startup.

    After GPS lock, starts the TelemetryService (if present on connector).
    """
    drone = connector.drone
    connection_string = f"{protocol}://{address}:{port}"
    logger.info("Background: Connecting to drone...")
    logger.info("  Protocol: %s", protocol.upper())
    logger.info("  Target: %s:%s", address, port)

    await drone.connect(system_address=connection_string)

    logger.info("Background: Waiting for drone to respond...")
    async for state in drone.core.connection_state():
        if state.is_connected:
            logger.info("=" * 60)
            logger.info("✓ SUCCESS: Connected to drone at %s:%s!", address, port)
            logger.info("=" * 60)
            break

    logger.info("Background: Waiting for GPS lock...")
    async for health in drone.telemetry.health():
        if health.is_global_position_ok or health.is_home_position_ok:
            logger.info("=" * 60)
            logger.info("✓ GPS LOCK ACQUIRED")
            logger.info("  Global position: %s", "OK" if health.is_global_position_ok else "Not ready")
            logger.info("  Home position: %s", "OK" if health.is_home_position_ok else "Not ready")
            logger.info("=" * 60)
            # Start TelemetryService now that MAVSDK streams are available
            if connector.telemetry:
                await connector.telemetry.start()
            logger.info("Drone is READY for commands")
            logger.info("=" * 60)
            # Signal that connection is ready!
            connector.connection_ready.set()
            break


async def get_or_create_global_connector() -> MAVLinkConnector:
    """Get or create the global drone connector (thread-safe)"""
    global _global_connector, _connection_task
    
    async with _connection_lock:
        if _global_connector is not None:
            return _global_connector
        
        # Initialize for the first time
        logger.info("=" * 60)
        logger.info("MAVLink MCP Server - Initializing Global Drone Connection")
        logger.info("=" * 60)
        
        # Read connection settings from environment (.env file)
        address = os.environ.get("MAVLINK_ADDRESS", "")
        port = os.environ.get("MAVLINK_PORT", "14540")
        protocol = os.environ.get("MAVLINK_PROTOCOL", "udp").lower()
        
        # Display connection configuration
        logger.info("Configuration loaded from .env file:")
        logger.info("  MAVLINK_ADDRESS: %s", address if address else "(not set)")
        logger.info("  MAVLINK_PORT: %s", port)
        logger.info("  MAVLINK_PROTOCOL: %s", protocol)
        logger.info("=" * 60)
        
        # Empty or 0.0.0.0 address = MAVSDK listen mode (udp://:PORT)
        # This is needed when PX4 SITL sends heartbeats TO droneserver
        if not address or address == "0.0.0.0":
            address = ""
            logger.info("  Listen mode: will accept connections on port %s", port)
        
        # Validate protocol
        if protocol not in ["tcp", "udp", "serial"]:
            logger.warning("Invalid protocol '%s', defaulting to udp", protocol)
            protocol = "udp"
        
        drone = System()
        connection_ready = asyncio.Event()
        
        # Initialize AirSim client if configured
        airsim_client = None
        airsim_host = os.environ.get("AIRSIM_HOST", "")
        airsim_port = int(os.environ.get("AIRSIM_PORT", "41451"))
        if airsim_host and AIRSIM_AVAILABLE:
            try:
                # Run in executor — cosysairsim uses msgpack-rpc/tornado which
                # conflicts with the running asyncio event loop if called directly
                loop = asyncio.get_event_loop()
                def _connect_airsim():
                    client = airsim.MultirotorClient(ip=airsim_host, port=airsim_port)
                    client.confirmConnection()
                    client.enableApiControl(True)
                    return client
                airsim_client = await loop.run_in_executor(None, _connect_airsim)
                logger.info(f"{LogColors.SUCCESS}✓ AirSim connected at {airsim_host}:{airsim_port}{LogColors.RESET}")
            except Exception as e:
                logger.warning(f"{LogColors.YELLOW}⚠ AirSim connection failed ({e}) — falling back to synthetic stubs{LogColors.RESET}")
                airsim_client = None
        elif airsim_host and not AIRSIM_AVAILABLE:
            logger.warning(f"{LogColors.YELLOW}⚠ AIRSIM_HOST set but cosysairsim not installed — synthetic stubs{LogColors.RESET}")

        # Create the global connector with TelemetryService
        _global_connector = MAVLinkConnector(
            drone=drone,
            connection_ready=connection_ready,
            telemetry=TelemetryService(drone),
            airsim_client=airsim_client,
        )

        # Start drone connection in background
        logger.info("Starting persistent drone connection in background...")
        logger.info("This connection will be shared across all requests")
        logger.info("-" * 60)

        _connection_task = asyncio.create_task(
            connect_drone_background(_global_connector, address, port, protocol)
        )
        
        return _global_connector

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[MAVLinkConnector]:
    """Manage application lifecycle - returns global persistent connector
    
    Note: In HTTP/SSE mode, FastMCP calls this for EVERY request, not just once.
    We use _lifespan_initialized flag to suppress noisy logs after first call.
    """
    global _lifespan_initialized
    
    # Only log on first initialization to avoid spam
    if not _lifespan_initialized:
        logger.info("=" * 60)
        logger.info("🚀 LIFESPAN: Starting application lifespan...")
        logger.info("=" * 60)
    
    try:
        # Get or create the global connector (only happens once)
        if not _lifespan_initialized:
            logger.info("LIFESPAN: Calling get_or_create_global_connector()...")
        
        connector = await get_or_create_global_connector()
        
        if not _lifespan_initialized:
            logger.info("LIFESPAN: Connector created successfully!")
            _lifespan_initialized = True
        
        # Just yield the global connector - no teardown per request!
        yield connector
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ LIFESPAN ERROR: {e}{LogColors.RESET}", exc_info=True)
        raise
    
    # Note: cleanup only happens on server shutdown (not per request)
    # In HTTP mode, this might not be called at all until process termination
    # Only log if this is actually a shutdown (not just end of request)

# Pass lifespan to server
mcp = FastMCP("MAVLink MCP", lifespan=app_lifespan)


# ============================================================
# Startup Hook for SSE Mode
# ============================================================
async def initialize_drone_connection():
    """
    Initialize the global drone connection.
    Call this from droneserver_http.py after the server starts.
    """
    logger.info("=" * 60)
    logger.info("🚀 STARTUP: Initializing drone connection...")
    logger.info("=" * 60)
    try:
        await get_or_create_global_connector()
        logger.info("✓ Drone connection initialization complete!")
    except Exception as e:
        logger.error("❌ Failed to initialize drone connection: %s", str(e), exc_info=True)


# ARM
@mcp.tool()
async def arm_drone(ctx: Context) -> dict:
    """Arm the drone. Waits for drone connection if not yet ready."""
    log_tool_call("arm_drone")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    log_mavlink_cmd("drone.action.arm")
    await drone.action.arm()
    return {"status": "success", "message": "Drone armed"}


# Get Position
@mcp.tool()
async def get_position(ctx: Context) -> dict:
    """
    Get the position of the drone in latitude/longitude degrees and altitude in meters.
    The drone must be connected and have a global position estimate.
    This tool will wait up to 30 seconds for the drone to be ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: A dict with the position or error status.
    """
    log_tool_call("get_position")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching drone position")

    try:
        async for position in drone.telemetry.position():
            result = {"status": "success", "position": {
                "latitude_deg": position.latitude_deg,
                "longitude_deg": position.longitude_deg,
                "absolute_altitude_m": position.absolute_altitude_m,
                "relative_altitude_m": position.relative_altitude_m
            }}
            log_tool_output(result)
            return result
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to retrieve position: {e}{LogColors.RESET}")
        return {"status": "failed", "error": str(e)}

@mcp.tool()
async def move_to_relative(ctx: Context, north_m: float, east_m: float, down_m: float, yaw_deg: float = 0.0) -> dict:
    """
    Move the drone relative to the current position using ArduPilot's GUIDED mode.
    
    ArduPilot automatically enters GUIDED mode when receiving goto_location commands
    (as long as the drone is armed). No manual mode switching required.
    
    The drone must be armed and in the air. Waits for connection if not ready.

    Args:
        ctx (Context): the context.
        north_m (float): distance in meters to move north (negative for south).
        east_m (float): distance in meters to move east (negative for west).
        down_m (float): distance in meters to move down (negative for up). Note: negative values = climb.
        yaw_deg (float): target yaw angle in degrees. Default is 0.0 (no yaw change).

    Returns:
        dict: Status message with success or error.
    """
    log_tool_call("move_to_relative", north_m=north_m, east_m=east_m, down_m=down_m, yaw_deg=yaw_deg)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone

    try:
        # Get current position
        position = await drone.telemetry.position().__anext__()
        current_lat = position.latitude_deg
        current_lon = position.longitude_deg
        # IMPORTANT: goto_location() requires ABSOLUTE altitude (MSL), not relative!
        current_alt = position.absolute_altitude_m
        
        # Calculate target altitude (down is positive in NED, so negate)
        target_alt = current_alt - down_m
        
        # Convert NED offsets (meters) to lat/lon offsets (degrees)
        # Earth radius in meters (approximate)
        EARTH_RADIUS = 6371000.0
        
        # Latitude: 1 degree = ~111,320 meters (constant)
        # north_m positive = increase latitude
        lat_offset_deg = north_m / 111320.0
        
        # Longitude: varies with latitude
        # east_m positive = increase longitude
        lon_offset_deg = east_m / (111320.0 * math.cos(math.radians(current_lat)))
        
        # Calculate target position
        target_lat = current_lat + lat_offset_deg
        target_lon = current_lon + lon_offset_deg
        
        logger.info(f"Moving in GUIDED mode:")
        logger.info(f"  Current: {current_lat:.6f}°, {current_lon:.6f}°")
        logger.info(f"  Altitude: {position.relative_altitude_m:.1f}m AGL (relative) / {current_alt:.1f}m MSL")
        logger.info(f"  Offset: north={north_m:.1f}m, east={east_m:.1f}m, down={down_m:.1f}m")
        target_rel_alt = position.relative_altitude_m - down_m
        logger.info(f"  Target: {target_lat:.6f}°, {target_lon:.6f}°, {target_rel_alt:.1f}m AGL (relative) / {target_alt:.1f}m MSL")
        
        # Use goto_location with calculated target coordinates
        log_mavlink_cmd("drone.action.goto_location", lat=f"{target_lat:.6f}", lon=f"{target_lon:.6f}", alt=f"{target_alt:.1f}", yaw=f"{yaw_deg:.1f}" if not math.isnan(yaw_deg) else "nan")
        await drone.action.goto_location(
            target_lat,
            target_lon,
            target_alt,
            yaw_deg
        )
        
        logger.info("✓ Movement command sent successfully")
        return {
            "status": "success", 
            "message": f"Moving: north={north_m}m, east={east_m}m, altitude_change={-down_m}m",
            "target_position": {
                "latitude_deg": target_lat,
                "longitude_deg": target_lon,
                "altitude_m": target_alt
            }
        }
        
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to execute relative movement: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Movement failed: {str(e)}"}

@mcp.tool()
async def takeoff(ctx: Context, takeoff_altitude: float = 3.0, wait_for_altitude: bool = True) -> dict:
    """Command the drone to initiate takeoff and ascend to a specified altitude. 
    The drone must be armed. Waits for connection if not ready.
    
    IMPORTANT: By default, this function waits until the drone reaches the target 
    altitude before returning. This prevents unsafe conditions where subsequent 
    navigation commands are sent while the drone is still climbing.

    Args:
        ctx (Context): The context of the request.
        takeoff_altitude (float): The altitude to ascend to after takeoff. Default is 3.0 meters.
        wait_for_altitude (bool): If True (default), waits until drone reaches target altitude.
                                  Set to False only if you need immediate return and will
                                  monitor altitude manually before sending navigation commands.

    Returns:
        dict: Status message with success or error, including final altitude reached.
    """
    log_tool_call("takeoff", takeoff_altitude=takeoff_altitude, wait_for_altitude=wait_for_altitude)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info(f"Taking off to {takeoff_altitude}m AGL (relative altitude)")
    log_mavlink_cmd("drone.action.set_takeoff_altitude", altitude=takeoff_altitude)
    await drone.action.set_takeoff_altitude(takeoff_altitude)
    log_mavlink_cmd("drone.action.takeoff")
    await drone.action.takeoff()
    
    if not wait_for_altitude:
        return {
            "status": "success", 
            "message": f"Takeoff initiated to {takeoff_altitude}m AGL (relative)",
            "warning": "⚠️ Takeoff in progress - do NOT send navigation commands until altitude is reached!"
        }
    
    # Wait for drone to reach target altitude
    logger.info(f"Waiting for drone to reach {takeoff_altitude}m...")
    altitude_threshold = 0.5  # Consider arrived when within 0.5m of target
    max_wait_time = 60  # Maximum wait time in seconds
    check_interval = 1.0  # Check every second
    elapsed_time = 0
    
    while elapsed_time < max_wait_time:
        try:
            async for position in drone.telemetry.position():
                current_alt = position.relative_altitude_m
                logger.info(f"  Altitude: {current_alt:.1f}m / {takeoff_altitude}m")
                
                if current_alt >= (takeoff_altitude - altitude_threshold):
                    logger.info(f"{LogColors.SUCCESS}✅ Takeoff complete - reached {current_alt:.1f}m{LogColors.RESET}")
                    result = {
                        "status": "success",
                        "message": f"Takeoff complete - drone at {current_alt:.1f}m AGL",
                        "altitude_reached_m": round(current_alt, 1),
                        "target_altitude_m": takeoff_altitude,
                        "safe_to_navigate": True
                    }
                    log_tool_output(result)
                    return result
                break  # Got one position reading, wait and try again
        except Exception as e:
            logger.warning(f"Error reading altitude: {e}")
        
        await asyncio.sleep(check_interval)
        elapsed_time += check_interval
    
    # Timeout - get final altitude
    try:
        async for position in drone.telemetry.position():
            current_alt = position.relative_altitude_m
            break
    except:
        current_alt = 0
    
    logger.warning(f"Takeoff timeout after {max_wait_time}s - current altitude: {current_alt:.1f}m")
    return {
        "status": "warning",
        "message": f"Takeoff timeout - drone at {current_alt:.1f}m (target was {takeoff_altitude}m)",
        "altitude_reached_m": round(current_alt, 1),
        "target_altitude_m": takeoff_altitude,
        "safe_to_navigate": current_alt >= (takeoff_altitude - altitude_threshold)
    }

@mcp.tool()
async def land(ctx: Context, force: bool = False) -> dict:
    """Command the drone to initiate landing at its current location.
    
    LANDING GATE SAFETY: If there's a pending navigation destination (from go_to_location),
    this will check if the drone has arrived before allowing landing. This prevents
    accidentally landing far from the intended destination.
    
    Use force=True to override the landing gate (emergency use only).

    Args:
        ctx (Context): The context of the request.
        force (bool): If True, bypass landing gate safety check (default: False).

    Returns:
        dict: Status message with success, blocked, or error.
    """
    log_tool_call("land", force=force)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone

    # RTL GUARD: Block manual landing when drone is already returning autonomously
    if not force:
        current_fm = None
        if connector.telemetry:
            fm_obj = connector.telemetry.get("flight_mode")
            current_fm = str(fm_obj).split(".")[-1] if fm_obj else None
        if current_fm in ("RETURN_TO_LAUNCH", "LAND"):
            result = {
                "status": "blocked",
                "message": f"Drone is already in {current_fm} mode — landing autonomously. Do not intervene.",
                "flight_mode": current_fm,
                "recommendation": "Call get_drone_activity() to monitor landing progress. Use land(force=True) only for emergencies.",
            }
            log_tool_output(result)
            return result

    # LANDING GATE: Check if there's a pending destination
    if connector.current_activity and connector.current_activity.destination and not force:
        dest = connector.current_activity.destination
        dest_lat = dest["latitude"]
        dest_lon = dest["longitude"]
        
        # Get current position
        try:
            async for position in drone.telemetry.position():
                current_lat = position.latitude_deg
                current_lon = position.longitude_deg
                break
            
            distance = haversine_distance(current_lat, current_lon, dest_lat, dest_lon)
            
            # Landing gate threshold - block if more than 20m from destination
            landing_gate_threshold = 20.0
            
            if distance > landing_gate_threshold:
                logger.warning(f"{LogColors.ERROR}🚫 LANDING BLOCKED - {distance:.0f}m from destination!{LogColors.RESET}")
                
                result = {
                    "status": "blocked",
                    "message": f"Cannot land - drone is {distance:.0f}m from destination!",
                    "distance_to_destination_m": round(distance, 1),
                    "current_position": {
                        "latitude": current_lat,
                        "longitude": current_lon
                    },
                    "destination": {
                        "latitude": dest_lat,
                        "longitude": dest_lon
                    },
                    "recommendation": "Call get_drone_activity() to check progress, or use land(force=True) for emergency landing",
                    "safe_to_land": False
                }
                log_tool_output(result)
                return result
            else:
                # Close enough - clear destination and proceed with landing
                logger.info(f"Landing gate passed - {distance:.1f}m from destination (within {landing_gate_threshold}m threshold)")
                if connector.current_activity:
                    connector.current_activity.destination = None

        except Exception as e:
            logger.warning(f"Could not check position for landing gate: {e}")
            # Proceed with landing if we can't check position

    # Update activity for landing
    if connector.current_activity:
        connector.current_activity.destination = None
        connector.current_activity.landing_initiated = True
    else:
        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="land",
            status="active",
            started_at=time.time(),
            command_tool="land",
            description="Landing",
            landing_initiated=True,
        )
    
    log_mavlink_cmd("drone.action.land")
    await drone.action.land()
    
    result = {
        "status": "success", 
        "message": "Landing initiated",
        "next_step": "Call get_drone_activity() until mission_complete is true"
    }
    log_tool_output(result)
    return result

@mcp.tool()
async def print_status_text(ctx: Context) -> dict:
    """Print and return status text from the drone. Waits for connection if not ready."""
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    try:
        async for status_text in drone.telemetry.status_text():
            logger.info(f"Status: {status_text.type}: {status_text.text}")
            return {"status": "success", "type": status_text.type, "text": status_text.text}
    except asyncio.CancelledError:
        return {"status": "failed", "error": "Failed to retrieve status text"}

@mcp.tool()
async def get_imu(ctx: Context, n: int = 1) -> dict:
    """Fetch the first n IMU data points from the drone. Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        n (int): The number of IMU data points to fetch. Default is 1.

    Returns:
        dict: A dict with status and list of IMU data points.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    telemetry = drone.telemetry

    # Set the rate at which IMU data is updated (in Hz)
    await telemetry.set_rate_imu(200.0)

    imu_data = []
    count = 0

    async for imu in telemetry.imu():
        imu_data.append({
            "timestamp_us": imu.timestamp_us,
            "acceleration": {
                "x": imu.acceleration_frd.forward_m_s2,
                "y": imu.acceleration_frd.right_m_s2,
                "z": imu.acceleration_frd.down_m_s2
            },
            "angular_velocity": {
                "x": imu.angular_velocity_frd.forward_rad_s,
                "y": imu.angular_velocity_frd.right_rad_s,
                "z": imu.angular_velocity_frd.down_rad_s
            },
            "magnetic_field": {
                "x": imu.magnetic_field_frd.forward_gauss,
                "y": imu.magnetic_field_frd.right_gauss,
                "z": imu.magnetic_field_frd.down_gauss
            },
            "temperature_degc": imu.temperature_degc
        })
        count += 1
        if count >= n:
            break

    return {"status": "success", "imu_data": imu_data, "count": len(imu_data)}

@mcp.tool()
async def print_mission_progress(ctx: Context) -> dict:
    """
    Print and return the current mission progress of the drone. Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: A dictionary containing the current and total mission progress or error status.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    async for mission_progress in drone.mission.mission_progress():
        logger.info(f"Mission progress: {mission_progress.current}/{mission_progress.total}")
        return {"status": "success", "current": mission_progress.current, "total": mission_progress.total}



@mcp.tool()
async def initiate_mission(ctx: Context, mission_points: list, return_to_launch: bool = True) -> dict:
    """
    Initiate a mission with a list of mission points. The drone must be armed. Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        mission_points (list): A list of dictionaries representing mission points. Each dictionary must include:
            - latitude_deg (float): Latitude in degrees (range: -90 to +90).
            - longitude_deg (float): Longitude in degrees (range: -180 to +180).
            - relative_altitude_m (float): Altitude relative to the takeoff altitude in meters.
            - speed_m_s (float): Speed in meters per second.
            - is_fly_through (bool): Whether to fly through the point or stop.
            - gimbal_pitch_deg (float): Gimbal pitch angle in degrees (optional).
            - gimbal_yaw_deg (float): Gimbal yaw angle in degrees (optional).
            - camera_action (MissionItem.CameraAction): Camera action at the point (optional).
            - loiter_time_s (float): Loiter time in seconds (optional).
            - camera_photo_interval_s (float): Camera photo interval in seconds (optional).
            - acceptance_radius_m (float): Acceptance radius in meters (optional).
            - yaw_deg (float): Yaw angle in degrees (optional).
            - camera_photo_distance_m (float): Camera photo distance in meters (optional).
            - vehicle_action (MissionItem.VehicleAction): Vehicle action at the point (optional).
        return_to_launch (bool): Whether to return to launch after completing the mission. Default is True.

    Returns:
        dict: Status message with success or error.
    """
    log_tool_call("initiate_mission", waypoint_count=len(mission_points), return_to_launch=return_to_launch)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone

    # Validate and construct mission items using mission_raw (ArduPilot-compatible)
    mission_items = []
    for i, point in enumerate(mission_points):
        try:
            # Validate latitude and longitude ranges
            if not (-90 <= point["latitude_deg"] <= 90):
                return {"status": "failed", "error": f"Invalid latitude_deg: {point['latitude_deg']}. Must be between -90 and 90."}
            if not (-180 <= point["longitude_deg"] <= 180):
                return {"status": "failed", "error": f"Invalid longitude_deg: {point['longitude_deg']}. Must be between -180 and 180."}

            # Use mission_raw format (raw MAVLink protocol)
            mission_items.append(MissionItem(
                seq=i,
                frame=3,  # MAV_FRAME_GLOBAL_RELATIVE_ALT
                command=16,  # MAV_CMD_NAV_WAYPOINT
                current=1 if i == 0 else 0,
                autocontinue=1,
                param1=point.get("loiter_time_s", 0),  # Hold time
                param2=point.get("acceptance_radius_m", 2.0),  # Acceptance radius
                param3=0,  # Pass radius
                param4=point.get("yaw_deg", float('nan')),  # Yaw angle
                x=int(point["latitude_deg"] * 1e7),  # Latitude * 1e7
                y=int(point["longitude_deg"] * 1e7),  # Longitude * 1e7
                z=float(point["relative_altitude_m"]),  # Altitude
                mission_type=0  # MAV_MISSION_TYPE_MISSION
            ))
        except KeyError as e:
            return {"status": "failed", "error": f"Missing required field in mission point: {e}"}

    # Set return-to-launch behavior
    log_mavlink_cmd("drone.mission.set_return_to_launch_after_mission", return_to_launch=return_to_launch)
    await drone.mission.set_return_to_launch_after_mission(return_to_launch)

    log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(mission_items))
    logger.info("Uploading mission using mission_raw (ArduPilot-compatible)")
    await drone.mission_raw.upload_mission(mission_items)

    log_mavlink_cmd("drone.mission.start_mission")
    logger.info("⚠️  Mission starting - drone will switch to AUTO flight mode")
    await drone.mission.start_mission()

    return {
        "status": "success", 
        "message": f"Mission started with {len(mission_items)} waypoints",
        "note": "Flight mode automatically changed to AUTO for mission execution"
    }

@mcp.tool()
async def get_flight_mode(ctx: Context) -> dict:
    """
    Get the current flight mode of the drone. Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: The current flight mode of the drone or error status.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    try:
        flight_mode = await drone.telemetry.flight_mode().__anext__()
        logger.info(f"FlightMode: {flight_mode}")
        return {"status": "success", "flight_mode": str(flight_mode)}
    except StopAsyncIteration:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to retrieve flight mode{LogColors.RESET}")
        return {"status": "failed", "error": "Failed to retrieve flight mode"}

@mcp.tool()
async def set_flight_mode(ctx: Context, mode: str) -> dict:
    """
    Set the flight mode of the drone.
    
    Available modes:
    - HOLD: Hold current position (requires GPS)
    - RTL: Return to launch/home position
    - LAND: Land at current position
    - GUIDED: Manual waypoint control (used by go_to_location)
    
    Note: Some modes like AUTO require an active mission.
    For GUIDED mode navigation, use go_to_location instead.

    Args:
        ctx (Context): The context of the request.
        mode (str): The flight mode to set (HOLD, RTL, LAND, GUIDED).

    Returns:
        dict: Status message with the new flight mode or error.
    """
    log_tool_call("set_flight_mode", mode=mode)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    mode_upper = mode.upper().strip()
    
    # Map mode names to MAVSDK actions
    supported_modes = {
        "HOLD": "hold",
        "LOITER": "hold",  # LOITER maps to hold action
        "RTL": "return_to_launch",
        "RETURN_TO_LAUNCH": "return_to_launch",
        "LAND": "land",
        "GUIDED": "guided",
    }
    
    if mode_upper not in supported_modes:
        return {
            "status": "failed", 
            "error": f"Unsupported mode: {mode}. Supported modes: HOLD, RTL, LAND, GUIDED",
            "hint": "For AUTO mode, use initiate_mission or resume_mission instead."
        }
    
    try:
        action_name = supported_modes[mode_upper]
        
        if action_name == "hold":
            log_mavlink_cmd("drone.action.hold")
            await drone.action.hold()
            result_mode = "HOLD/LOITER"
            
        elif action_name == "return_to_launch":
            log_mavlink_cmd("drone.action.return_to_launch")
            await drone.action.return_to_launch()
            result_mode = "RTL"
            
        elif action_name == "land":
            log_mavlink_cmd("drone.action.land")
            await drone.action.land()
            result_mode = "LAND"
            
        elif action_name == "guided":
            # For GUIDED, we need to send a position command to enter GUIDED mode
            # Get current position and command drone to hold there
            position = await drone.telemetry.position().__anext__()
            log_mavlink_cmd("drone.action.goto_location (GUIDED mode)", 
                          lat=f"{position.latitude_deg:.6f}", 
                          lon=f"{position.longitude_deg:.6f}",
                          alt=f"{position.absolute_altitude_m:.1f}")
            await drone.action.goto_location(
                position.latitude_deg,
                position.longitude_deg,
                position.absolute_altitude_m,
                float('nan')  # Maintain current yaw
            )
            result_mode = "GUIDED"
        
        # Verify mode changed
        await asyncio.sleep(0.5)
        try:
            new_mode = await drone.telemetry.flight_mode().__anext__()
            actual_mode = str(new_mode)
        except:
            actual_mode = "UNKNOWN"
        
        logger.info(f"{LogColors.SUCCESS}✅ Flight mode set to {result_mode} (actual: {actual_mode}){LogColors.RESET}")
        
        return {
            "status": "success",
            "message": f"Flight mode changed to {result_mode}",
            "requested_mode": mode_upper,
            "actual_mode": actual_mode
        }
        
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to set flight mode: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Failed to set flight mode: {str(e)}"}

# ============================================================================
# PRIORITY 1: CRITICAL SAFETY TOOLS (v1.1.0)
# ============================================================================

@mcp.tool()
async def disarm_drone(ctx: Context) -> dict:
    """
    Disarm the drone motors. This stops the motors from spinning.
    SAFETY: Only use when drone is on the ground!
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with success or error.
    """
    log_tool_call("disarm_drone")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Disarming drone")
    
    try:
        log_mavlink_cmd("drone.action.disarm")
        await drone.action.disarm()
        return {"status": "success", "message": "Drone disarmed - motors stopped"}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to disarm: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Disarm failed: {str(e)}"}

@mcp.tool()
async def return_to_launch(ctx: Context) -> dict:
    """
    Command the drone to return to its launch/home position (RTL mode).
    This is the primary emergency/safety feature.
    The drone will fly back to home and land automatically.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with success or error.
    """
    log_tool_call("return_to_launch")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Initiating Return to Launch (RTL)")
    
    try:
        log_mavlink_cmd("drone.action.return_to_launch")
        await drone.action.return_to_launch()

        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="rtl",
            status="active",
            started_at=time.time(),
            command_tool="return_to_launch",
            description="Returning to launch",
        )

        return {"status": "success", "message": "Return to Launch initiated - drone returning home"}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - RTL failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Return to Launch failed: {str(e)}"}

@mcp.tool()
async def kill_motors(ctx: Context) -> dict:
    """
    EMERGENCY ONLY: Immediately cut power to all motors.
    ⚠️  WARNING: This will cause the drone to fall from the sky!
    ⚠️  Only use in critical emergencies (fire, collision imminent, etc.)
    ⚠️  Drone may be damaged from the fall!
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with success or error.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.warning(f"{LogColors.YELLOW}⚠️  EMERGENCY MOTOR KILL ACTIVATED ⚠️{LogColors.RESET}")
    
    try:
        log_mavlink_cmd("drone.action.kill")
        await drone.action.kill()
        return {
            "status": "success", 
            "message": "EMERGENCY: Motors killed - drone will fall!",
            "warning": "This is an emergency action. Drone may be damaged."
        }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Motor kill failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Motor kill failed: {str(e)}"}

@mcp.tool()
async def hold_position(ctx: Context) -> dict:
    """
    Command the drone to hold its current position while staying in GUIDED mode.
    Useful for pausing during flight to assess situation or wait.
    Waits for connection if not ready.
    
    Note: This uses goto_location with current position instead of hold() to avoid
          switching to LOITER mode which can cause unwanted altitude changes.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with success or error including current position.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    log_tool_call("hold_position")
    logger.info("Commanding drone to hold position (staying in GUIDED mode)")
    
    try:
        # Get current position
        position = await drone.telemetry.position().__anext__()
        current_lat = position.latitude_deg
        current_lon = position.longitude_deg
        current_alt = position.absolute_altitude_m
        
        # Send goto_location with current position - keeps drone in GUIDED mode
        # This prevents the altitude drop that occurs when switching to LOITER mode
        log_mavlink_cmd("drone.action.goto_location", lat=f"{current_lat:.6f}", lon=f"{current_lon:.6f}", alt=f"{current_alt:.1f}")
        await drone.action.goto_location(
            current_lat,
            current_lon,
            current_alt,
            float('nan')  # Maintain current heading
        )
        
        logger.info(f"{LogColors.SUCCESS}✓ Holding position at ({current_lat:.6f}, {current_lon:.6f}) @ {position.relative_altitude_m:.1f}m AGL (relative) / {current_alt:.1f}m MSL{LogColors.RESET}")
        
        return {
            "status": "success",
            "message": "Drone holding position in GUIDED mode",
            "position": {
                "latitude_deg": current_lat,
                "longitude_deg": current_lon,
                "altitude_m": position.relative_altitude_m,
                "altitude_rel": position.relative_altitude_m
            },
            "note": "Using GUIDED mode instead of LOITER to prevent altitude drift"
        }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Hold position failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Hold position failed: {str(e)}"}

@mcp.tool()
async def get_battery(ctx: Context) -> dict:
    """
    Get the current battery status including voltage and remaining percentage.
    Critical for monitoring flight time and knowing when to land.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Battery voltage (V), remaining percentage (%), and status.
    """
    log_tool_call("get_battery")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching battery status")
    
    try:
        async for battery in drone.telemetry.battery():
            voltage = battery.voltage_v
            percent_raw = battery.remaining_percent
            
            battery_data = {
                "voltage_v": round(voltage, 2),
                "remaining_percent": round(percent_raw * 100, 1),  # Convert to percentage
            }
            
            # Handle case where percentage is unavailable/uncalibrated (0% with good voltage)
            if percent_raw == 0.0 and voltage > 10.0:
                battery_data["note"] = "⚠️  Battery percentage unavailable - using voltage estimate"
                battery_data["calibration_status"] = "Uncalibrated or not supported by autopilot"
                
                # Rough LiPo estimate: 4.2V = 100%, 3.7V = 50%, 3.5V = 0% per cell
                # Assume 4S LiPo (most common for drones): 16.8V full, 14.8V nominal, 14.0V empty
                if voltage >= 16.0:
                    estimated_percent = 90
                elif voltage >= 15.2:
                    estimated_percent = 75
                elif voltage >= 14.8:
                    estimated_percent = 50
                elif voltage >= 14.0:
                    estimated_percent = 25
                else:
                    estimated_percent = 10
                
                battery_data["estimated_percent"] = estimated_percent
                battery_data["hint"] = "Set battery capacity parameter (BATT_CAPACITY) for accurate readings"
            
            # Add warning if battery is low (use estimated if percentage unavailable)
            effective_percent = percent_raw if percent_raw > 0 else (battery_data.get("estimated_percent", 100) / 100)
            
            if effective_percent < 0.20:
                battery_data["warning"] = "⚠️  LOW BATTERY - Land soon!"
            elif effective_percent < 0.30:
                battery_data["warning"] = "Battery getting low - consider landing"
            
            logger.info(f"{LogColors.STATUS}Battery: {battery_data['voltage_v']}V, {battery_data['remaining_percent']}% "
                       f"{'(estimated: ' + str(battery_data.get('estimated_percent', '')) + '%)' if 'estimated_percent' in battery_data else ''}{LogColors.RESET}")
            return {"status": "success", "battery": battery_data}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get battery status: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Battery read failed: {str(e)}"}

# ============================================================================
# PRIORITY 2: FLIGHT MODE MANAGEMENT & SYSTEM HEALTH (v1.1.0)
# ============================================================================

@mcp.tool()
async def get_health(ctx: Context) -> dict:
    """
    Get comprehensive system health status for pre-flight checks.
    Returns status of GPS, accelerometer, gyro, magnetometer, and more.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Comprehensive health status of all drone subsystems.
    """
    log_tool_call("get_health")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching system health")
    
    try:
        async for health in drone.telemetry.health():
            health_data = {
                "is_gyrometer_calibrated": health.is_gyrometer_calibration_ok,
                "is_accelerometer_calibrated": health.is_accelerometer_calibration_ok,
                "is_magnetometer_calibrated": health.is_magnetometer_calibration_ok,
                "is_local_position_ok": health.is_local_position_ok,
                "is_global_position_ok": health.is_global_position_ok,
                "is_home_position_ok": health.is_home_position_ok,
                "is_armable": health.is_armable,
            }
            
            # Add overall health assessment
            all_ok = all(health_data.values())
            health_data["overall_status"] = "HEALTHY" if all_ok else "ISSUES DETECTED"
            
            # Add warnings for critical issues
            warnings = []
            if not health.is_global_position_ok:
                warnings.append("⚠️  No GPS lock - cannot fly safely!")
            if not health.is_armable:
                warnings.append("⚠️  Drone is not armable - check for errors")
            if not health.is_gyrometer_calibration_ok:
                warnings.append("Gyroscope needs calibration")
            if not health.is_accelerometer_calibration_ok:
                warnings.append("Accelerometer needs calibration")
            if not health.is_magnetometer_calibration_ok:
                warnings.append("Magnetometer/compass needs calibration")
            
            if warnings:
                health_data["warnings"] = warnings
            
            logger.info(f"{LogColors.STATUS}System health: {health_data['overall_status']}{LogColors.RESET}")
            return {"status": "success", "health": health_data}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get health status: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Health check failed: {str(e)}"}

@mcp.tool()
async def pause_mission(ctx: Context) -> dict:
    """
    ⛔ DEPRECATED - DO NOT USE ⛔
    
    This tool has been deprecated due to CRITICAL SAFETY ISSUES:
    - Entering LOITER mode causes ALTITUDE DESCENT
    - LOITER does NOT hold current altitude
    - This has caused CRASHES in testing
    
    ✅ USE hold_mission_position() INSTEAD ✅
    
    The hold_mission_position() tool:
    - Stays in GUIDED mode (safe)
    - Maintains current altitude (no descent)
    - Holds position reliably
    
    This tool will be removed in a future version.
    
    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Error message directing to safe alternative.
    """
    logger.error(f"{LogColors.ERROR}⛔ pause_mission() called - THIS TOOL IS DEPRECATED AND UNSAFE!{LogColors.RESET}")
    logger.error(f"{LogColors.ERROR}⚠️  CRITICAL: pause_mission enters LOITER mode which requires RC throttle input{LogColors.RESET}")
    logger.error(f"{LogColors.ERROR}⚠️  Without RC throttle at 50%, altitude is unpredictable - this has caused crashes!{LogColors.RESET}")
    logger.error(f"{LogColors.ERROR}⚠️  Use hold_mission_position() instead - it stays in GUIDED mode{LogColors.RESET}")
    
    return {
        "status": "failed",
        "error": "⛔ pause_mission() is DEPRECATED due to safety issues",
        "reason": "LOITER mode requires RC throttle input (50% to hold altitude) - not available via MAVLink",
        "technical_details": "Per ArduPilot docs: 'Altitude can be controlled with the Throttle control stick' - we don't have throttle control via MAVSDK",
        "crash_report": "Flight testing: unknown throttle position → altitude descent from 25m → GROUND IMPACT",
        "safe_alternative": "Use hold_mission_position() instead",
        "why_safe": "hold_mission_position() uses GUIDED mode which doesn't require RC input and maintains altitude autonomously",
        "how_to_use": "Call hold_mission_position() to pause, then set_current_waypoint() + resume_mission() to continue",
        "migration_guide": "See LOITER_MODE_CRASH_REPORT.md for full details"
    }

@mcp.tool()
async def hold_mission_position(ctx: Context) -> dict:
    """
    Alternative to pause_mission that holds position in GUIDED mode instead of LOITER.
    This interrupts the current mission and switches to GUIDED mode to hold the current position.
    Unlike pause_mission, this does NOT enter LOITER mode.
    
    NOTE: This stops the mission entirely. To continue the mission after using this,
    you must use set_current_waypoint() to jump back to a waypoint and resume_mission(),
    or upload/initiate a new mission.
    
    Use this when you want to:
    - Pause flight without entering LOITER mode
    - Maintain altitude stability (GUIDED mode)
    - Temporarily interrupt a mission for manual control
    
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with current position and waypoint info.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    log_tool_call("hold_mission_position")
    
    try:
        # Get current mission progress before holding
        current_wp = 0
        total_wp = 0
        async for mission_progress in drone.mission.mission_progress():
            current_wp = mission_progress.current
            total_wp = mission_progress.total
            break
        
        # Get current position
        async for position in drone.telemetry.position():
            current_lat = position.latitude_deg
            current_lon = position.longitude_deg
            current_alt = position.absolute_altitude_m
            break
        
        # Use hold_position to stay in GUIDED mode
        # This will call goto_location with current position
        log_mavlink_cmd(f"drone.action.goto_location(lat={current_lat}, lon={current_lon}, alt={current_alt})")
        logger.info(f"⚠️  Holding mission position in GUIDED mode (not LOITER) - was at waypoint {current_wp}/{total_wp}")
        await drone.action.goto_location(current_lat, current_lon, current_alt, float('nan'))
        
        return {
            "status": "success",
            "message": f"Holding position in GUIDED mode - mission interrupted at waypoint {current_wp}/{total_wp}",
            "was_at_waypoint": current_wp,
            "total_waypoints": total_wp,
            "position": {
                "latitude": current_lat,
                "longitude": current_lon,
                "altitude": current_alt
            },
            "flight_mode": "GUIDED",
            "note": "Mission stopped. To continue: use set_current_waypoint() then resume_mission(), or start a new mission."
        }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to hold mission position: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Hold mission position failed: {str(e)}"}

@mcp.tool()
async def resume_mission(ctx: Context) -> dict:
    """
    Resume a previously paused mission.
    The drone will continue from where it was paused.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with success or error including current waypoint.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    log_tool_call("resume_mission")
    
    try:
        # Get current mission progress before resuming
        current_wp = 0
        total_wp = 0
        async for mission_progress in drone.mission.mission_progress():
            current_wp = mission_progress.current
            total_wp = mission_progress.total
            break
        
        log_mavlink_cmd("drone.mission.start_mission")
        logger.info(f"⚠️  Resuming mission from waypoint {current_wp}/{total_wp} - drone will switch to AUTO flight mode")
        await drone.mission.start_mission()
        
        # Give the autopilot a moment to process the command
        await asyncio.sleep(0.5)
        
        # Verify flight mode changed to AUTO
        try:
            flight_mode = await drone.telemetry.flight_mode().__anext__()
            logger.info(f"Flight mode after resume: {flight_mode}")
            mode_ok = "AUTO" in str(flight_mode) or "MISSION" in str(flight_mode)
        except:
            mode_ok = False
            flight_mode = "UNKNOWN"
        
        return {
            "status": "success", 
            "message": f"Mission resumed from waypoint {current_wp}/{total_wp}",
            "current_waypoint": current_wp,
            "total_waypoints": total_wp,
            "flight_mode": str(flight_mode),
            "mode_transition_ok": mode_ok,
            "note": "Flight mode should have changed to AUTO/MISSION for mission execution"
        }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to resume mission: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Mission resume failed: {str(e)}"}

@mcp.tool()
async def clear_mission(ctx: Context) -> dict:
    """
    Clear the current mission from the drone.
    Removes all uploaded waypoints.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Status message with success or error.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Clearing mission")
    
    try:
        log_mavlink_cmd("drone.mission.clear_mission")
        await drone.mission.clear_mission()

        # Also clear mission_raw (fixes re-arm after mission_raw missions)
        try:
            await drone.mission_raw.clear_mission()
        except Exception:
            pass

        # Reset Python-side state
        connector.current_activity = None
        connector.current_mission = None

        return {"status": "success", "message": "Mission cleared - all waypoints and tracking state reset"}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to clear mission: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Mission clear failed: {str(e)}"}

# ============================================================================
# PRIORITY 3: NAVIGATION ENHANCEMENTS (v1.1.0)
# ============================================================================

@mcp.tool()
async def go_to_location(ctx: Context, latitude_deg: float, longitude_deg: float, 
                        absolute_altitude_m: float, yaw_deg: float = float('nan')) -> dict:
    """
    Fly to an absolute GPS location. Returns immediately - drone flies autonomously.
    
    AFTER CALLING THIS, YOU MUST:
    1. Call get_drone_activity() repeatedly
    2. PRINT the DISPLAY_TO_USER value to the user after each monitor_flight() call
    3. When status is "arrived", call land()
    4. Continue calling monitor_flight() until mission_complete is true
    
    This gives the user real-time updates on flight progress (distance, speed, ETA).

    Args:
        ctx (Context): The context of the request.
        latitude_deg (float): Target latitude in degrees (-90 to +90).
        longitude_deg (float): Target longitude in degrees (-180 to +180).
        absolute_altitude_m (float): Target altitude in meters MSL.
        yaw_deg (float): Target heading in degrees (optional).

    Returns:
        dict: Navigation started. Next: call monitor_flight() and show updates to user.
    """
    log_tool_call("go_to_location", latitude_deg=latitude_deg, longitude_deg=longitude_deg, 
                  absolute_altitude_m=absolute_altitude_m)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    # Validate coordinates
    if not (-90 <= latitude_deg <= 90):
        return {"status": "failed", "error": f"Invalid latitude: {latitude_deg}. Must be between -90 and 90."}
    if not (-180 <= longitude_deg <= 180):
        return {"status": "failed", "error": f"Invalid longitude: {longitude_deg}. Must be between -180 and 180."}
    
    drone = connector.drone
    
    try:
        # Get current position to calculate relative altitude and initial distance
        position = await drone.telemetry.position().__anext__()
        home_alt = position.absolute_altitude_m - position.relative_altitude_m
        relative_alt = absolute_altitude_m - home_alt
        initial_distance = haversine_distance(position.latitude_deg, position.longitude_deg, 
                                               latitude_deg, longitude_deg)
        
        # Get current speed to estimate flight time
        try:
            async for velocity in drone.telemetry.velocity_ned():
                ground_speed = math.sqrt(velocity.north_m_s**2 + velocity.east_m_s**2)
                break
        except:
            ground_speed = 10.0  # Default assumption
        
        # Estimate flight time (assuming ~10-15 m/s cruise speed for copter)
        estimated_speed = max(ground_speed, 10.0)  # At least 10 m/s for ETA
        eta_seconds = initial_distance / estimated_speed
        
        logger.info(f"Flying to GPS location: {latitude_deg}, {longitude_deg} at {relative_alt:.1f}m AGL / {absolute_altitude_m:.1f}m MSL")
        logger.info(f"Distance to target: {initial_distance:.1f}m, ETA: {eta_seconds:.0f}s")
        
        log_mavlink_cmd("drone.action.goto_location", lat=f"{latitude_deg:.6f}", lon=f"{longitude_deg:.6f}", 
                       alt=f"{absolute_altitude_m:.1f}", yaw=f"{yaw_deg:.1f}" if not math.isnan(yaw_deg) else "nan")
        await drone.action.goto_location(latitude_deg, longitude_deg, absolute_altitude_m, yaw_deg)
        
        # Create FlightActivity for unified tracking
        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="goto",
            status="active",
            started_at=time.time(),
            command_tool="go_to_location",
            description=f"Flying to ({latitude_deg:.5f}, {longitude_deg:.5f}) at {relative_alt:.0f}m AGL",
            destination={
                "latitude": latitude_deg,
                "longitude": longitude_deg,
                "altitude_msl": absolute_altitude_m,
                "initial_distance": initial_distance,
                "start_time": asyncio.get_event_loop().time(),
            },
            altitude_m=relative_alt,
            total_distance_m=initial_distance,
            estimated_time_s=eta_seconds,
        )
        
        result = {
            "status": "success", 
            "message": "Navigation started. Call get_drone_activity() to track progress.",
            "initial_distance_m": round(initial_distance, 1),
            "estimated_flight_time_seconds": round(eta_seconds, 0),
            "target": {
                "latitude": latitude_deg,
                "longitude": longitude_deg,
                "altitude_msl": absolute_altitude_m,
                "altitude_agl": round(relative_alt, 1),
                "yaw": yaw_deg if not math.isnan(yaw_deg) else "maintain current"
            },
            "next_step": "Call get_drone_activity() repeatedly until mission_complete is true"
        }
        log_tool_output(result)
        return result
        
    except Exception as e:
        logger.error(f"Go to location failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Navigation failed: {str(e)}"}

@mcp.tool()
async def check_arrival(
    ctx: Context,
    latitude_deg: float,
    longitude_deg: float,
    threshold_m: float = 10.0
) -> dict:
    """
    Check if the drone has arrived at a target GPS location (instant, non-blocking).
    
    IMPORTANT: Call this AFTER go_to_location or reposition commands.
    This returns immediately with current distance - it does NOT wait.
    
    If status is "in_progress", call this again after a few seconds.
    If status is "arrived", the drone is within threshold of target - safe to land.

    Args:
        ctx (Context): The context of the request.
        latitude_deg (float): Target latitude in degrees (-90 to +90).
        longitude_deg (float): Target longitude in degrees (-180 to +180).
        threshold_m (float): Distance threshold in meters to consider "arrived" (default: 10.0m).

    Returns:
        dict: Status with "arrived" (within threshold) or "in_progress" (still flying).
    """
    log_tool_call("check_arrival", latitude_deg=latitude_deg, longitude_deg=longitude_deg, 
                  threshold_m=threshold_m)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    # Validate coordinates
    if not (-90 <= latitude_deg <= 90):
        return {"status": "failed", "error": f"Invalid latitude: {latitude_deg}. Must be between -90 and 90."}
    if not (-180 <= longitude_deg <= 180):
        return {"status": "failed", "error": f"Invalid longitude: {longitude_deg}. Must be between -180 and 180."}
    
    drone = connector.drone
    
    try:
        # Get current position (instant - no waiting)
        async for position in drone.telemetry.position():
            current_lat = position.latitude_deg
            current_lon = position.longitude_deg
            current_alt = position.relative_altitude_m
            break
        
        # Calculate distance to target
        distance = haversine_distance(current_lat, current_lon, latitude_deg, longitude_deg)
        
        logger.info(f"📍 Distance to target: {distance:.1f}m (threshold: {threshold_m}m)")
        
        # Check if arrived
        if distance <= threshold_m:
            logger.info(f"{LogColors.SUCCESS}✅ ARRIVED at target! Distance: {distance:.1f}m{LogColors.RESET}")
            get_flight_logger().log_entry("ARRIVED", f"Distance: {distance:.1f}m")
            
            result = {
                "status": "arrived",
                "message": f"Drone has arrived at target location! Distance: {distance:.1f}m",
                "distance_m": round(distance, 1),
                "current_position": {
                    "latitude": current_lat,
                    "longitude": current_lon,
                    "altitude_m": current_alt
                },
                "target": {"latitude": latitude_deg, "longitude": longitude_deg}
            }
            log_tool_output(result)
            return result
        else:
            result = {
                "status": "in_progress",
                "message": f"Still {distance:.1f}m from target. Call check_arrival again in a few seconds.",
                "distance_m": round(distance, 1),
                "current_position": {
                    "latitude": current_lat,
                    "longitude": current_lon,
                    "altitude_m": current_alt
                },
                "target": {"latitude": latitude_deg, "longitude": longitude_deg}
            }
            log_tool_output(result)
            return result
            
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ Check arrival failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Check failed: {str(e)}"}


@mcp.tool()
async def monitor_flight(ctx: Context, arrival_threshold_m: float = 20.0, auto_land: bool = True) -> dict:
    """
    Monitor flight progress. YOU MUST CALL THIS IN A LOOP UNTIL mission_complete IS TRUE.
    
    ⚠️ CRITICAL: If mission_complete is false, you MUST call monitor_flight() again!
    Stopping early leaves the drone flying unattended - DANGEROUS!
    
    REQUIRED LOOP:
    while True:
        result = monitor_flight()
        print(result["DISPLAY_TO_USER"])  # Show user the progress
        if result["mission_complete"]:
            break  # Only stop when mission_complete is true
    
    Landing is automatic when the drone arrives (auto_land=True by default).

    Args:
        ctx (Context): The context of the request.
        arrival_threshold_m (float): Distance to consider "arrived" (default: 20m).
        auto_land (bool): Automatically land when arrived (default: True).

    Returns:
        dict: DISPLAY_TO_USER (print this!), status, mission_complete (ONLY stop when true).
    """
    # Fixed 30-second update interval (not configurable to prevent LLM from overriding)
    wait_seconds = 30.0
    
    log_tool_call("monitor_flight", arrival_threshold_m=arrival_threshold_m, auto_land=auto_land)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    
    try:
        # First, check if drone is on the ground (mission complete)
        async for landed_state in drone.telemetry.landed_state():
            landed_state_str = str(landed_state).split(".")[-1]
            break
        
        async for in_air in drone.telemetry.in_air():
            is_in_air = in_air
            break
        
        # Get current position
        async for position in drone.telemetry.position():
            current_lat = position.latitude_deg
            current_lon = position.longitude_deg
            current_alt = position.relative_altitude_m
            break
        
        # Check if landed (mission complete!)
        if landed_state_str == "ON_GROUND" or (not is_in_air and current_alt < 1.0):
            logger.info(f"{LogColors.SUCCESS}✅ MISSION COMPLETE - Drone has landed!{LogColors.RESET}")
            get_flight_logger().log_entry("LANDED", "Mission complete")
            
            # Mark activity completed
            if connector.current_activity:
                connector.current_activity.status = "completed"
                connector.current_activity.completed_at = time.time()
                connector.current_activity.landing_initiated = False
            
            result = {
                "DISPLAY_TO_USER": "✅ MISSION COMPLETE - Drone has landed safely!",
                "status": "landed",
                "altitude_m": round(current_alt, 1),
                "action_required": None,
                "mission_complete": True
            }
            log_tool_output(result)
            return result
        
        # Check if landing in progress
        if landed_state_str == "LANDING":
            logger.info(f"🛬 Landing in progress... altitude: {current_alt:.1f}m")
            
            result = {
                "DISPLAY_TO_USER": f"🛬 LANDING | Alt: {current_alt:.1f}m | Descending...",
                "status": "landing",
                "altitude_m": round(current_alt, 1),
                "action_required": "SHOW the DISPLAY_TO_USER to user, then CALL monitor_flight() AGAIN",
                "mission_complete": False
            }
            log_tool_output(result)
            return result
        
        # Check if there's a pending destination (still navigating)
        if not (connector.current_activity and connector.current_activity.destination):
            # Check if we initiated landing (auto_land or manual land call)
            if connector.current_activity and connector.current_activity.landing_initiated:
                logger.info(f"🛬 Landing in progress (flag set)... altitude: {current_alt:.1f}m")
                result = {
                    "DISPLAY_TO_USER": f"🛬 LANDING | Alt: {current_alt:.1f}m | Descending...",
                    "status": "landing",
                    "altitude_m": round(current_alt, 1),
                    "action_required": "call monitor_flight again",
                    "mission_complete": False
                }
                log_tool_output(result)
                return result
            
            # No destination and not landing - drone is just hovering
            result = {
                "DISPLAY_TO_USER": f"🚁 HOVERING | Alt: {current_alt:.1f}m | No destination set",
                "status": "hovering",
                "altitude_m": round(current_alt, 1),
                "action_required": "Call go_to_location() to set destination, or land() to land here",
                "mission_complete": False
            }
            log_tool_output(result)
            return result
        
        # Get destination from current activity
        dest = connector.current_activity.destination
        dest_lat = dest["latitude"]
        dest_lon = dest["longitude"]
        initial_distance = dest["initial_distance"]
        start_time = dest.get("start_time", asyncio.get_event_loop().time())
        
        logger.info(f"Monitoring flight for {wait_seconds}s...")
        
        check_interval = 1.0  # Check every second for arrival detection
        elapsed_in_monitor = 0
        
        while elapsed_in_monitor < wait_seconds:
            # Get current position
            async for position in drone.telemetry.position():
                current_lat = position.latitude_deg
                current_lon = position.longitude_deg
                current_alt = position.relative_altitude_m
                break
            
            # Calculate distance to destination
            distance = haversine_distance(current_lat, current_lon, dest_lat, dest_lon)
            
            # Calculate progress percentage
            if initial_distance > 0:
                progress = ((initial_distance - distance) / initial_distance) * 100
                progress = max(0, min(100, progress))
            else:
                progress = 100 if distance <= arrival_threshold_m else 0
            
            # Get speed for ETA calculation
            try:
                async for velocity in drone.telemetry.velocity_ned():
                    ground_speed = math.sqrt(velocity.north_m_s**2 + velocity.east_m_s**2)
                    break
            except:
                ground_speed = 0
            
            # Calculate ETA
            if ground_speed > 0.5:
                eta_seconds = distance / ground_speed
            else:
                eta_seconds = None
            
            logger.info(f"  📍 Distance: {distance:.1f}m ({progress:.0f}%), Speed: {ground_speed:.1f}m/s, Alt: {current_alt:.1f}m")
            
            # Check if arrived at destination
            if distance <= arrival_threshold_m:
                logger.info(f"{LogColors.SUCCESS}✅ ARRIVED at destination! Distance: {distance:.1f}m{LogColors.RESET}")
                get_flight_logger().log_entry("ARRIVED", f"Distance: {distance:.1f}m")
                
                # Clear destination on activity
                if connector.current_activity:
                    connector.current_activity.destination = None

                total_flight_time = asyncio.get_event_loop().time() - start_time

                if auto_land:
                    # Automatically initiate landing and WAIT for it to complete
                    logger.info(f"{LogColors.MAVLINK}🛬 Auto-landing initiated - waiting for touchdown{LogColors.RESET}")
                    get_flight_logger().log_entry("AUTO_LAND", "Landing initiated automatically")
                    if connector.current_activity:
                        connector.current_activity.landing_initiated = True
                    await drone.action.land()
                    
                    # Wait for landing to complete (up to 120 seconds)
                    landing_timeout = 120
                    landing_start = asyncio.get_event_loop().time()
                    
                    while (asyncio.get_event_loop().time() - landing_start) < landing_timeout:
                        # Check landed state
                        async for state in drone.telemetry.landed_state():
                            landed_state = state
                            break
                        
                        async for position in drone.telemetry.position():
                            current_alt = position.relative_altitude_m
                            break
                        
                        async for in_air in drone.telemetry.in_air():
                            is_in_air = in_air
                            break
                        
                        landed_state_str = str(landed_state).split(".")[-1]
                        
                        # Only consider landed when PX4 reports ON_GROUND AND not in air AND altitude < 2m
                        if landed_state_str == "ON_GROUND" and not is_in_air and current_alt < 2.0:
                            # Wait 3 more seconds to confirm stable on ground
                            logger.info(f"🛬 Touchdown detected, confirming stable...")
                            await asyncio.sleep(3)
                            
                            # Re-check to confirm
                            async for state in drone.telemetry.landed_state():
                                landed_state = state
                                break
                            async for in_air in drone.telemetry.in_air():
                                is_in_air = in_air
                                break
                            
                            landed_state_str = str(landed_state).split(".")[-1]
                            if landed_state_str == "ON_GROUND" and not is_in_air:
                                # Confirmed landed! Mark activity completed
                                if connector.current_activity:
                                    connector.current_activity.status = "completed"
                                    connector.current_activity.completed_at = time.time()
                                    connector.current_activity.landing_initiated = False
                                total_flight_time = asyncio.get_event_loop().time() - start_time
                                
                                logger.info(f"{LogColors.SUCCESS}✅ LANDED! Flight complete.{LogColors.RESET}")
                                get_flight_logger().log_entry("LANDED", "Mission complete")
                                
                                result = {
                                    "DISPLAY_TO_USER": f"✅ MISSION COMPLETE | Landed safely | Flight time: {total_flight_time:.0f}s",
                                    "status": "landed",
                                    "flight_time_seconds": round(total_flight_time, 0),
                                    "mission_complete": True
                                }
                                log_tool_output(result)
                                return result
                        
                        logger.info(f"🛬 Landing... altitude: {current_alt:.1f}m, state: {landed_state_str}, in_air: {is_in_air}")
                        await asyncio.sleep(2)  # Check every 2 seconds
                    
                    # Timeout - return landing status
                    result = {
                        "DISPLAY_TO_USER": f"⚠️ LANDING TIMEOUT | Alt: {current_alt:.1f}m | Check drone status",
                        "status": "landing_timeout",
                        "altitude_m": round(current_alt, 1),
                        "mission_complete": False
                    }
                    log_tool_output(result)
                    return result
                else:
                    # Manual landing required (auto_land=False)
                    result = {
                        "DISPLAY_TO_USER": f"✅ ARRIVED | Distance: {distance:.1f}m | Alt: {current_alt:.1f}m | Call land() to land",
                        "status": "arrived",
                        "distance_m": round(distance, 1),
                        "altitude_m": round(current_alt, 1),
                        "mission_complete": False
                    }
                    log_tool_output(result)
                    return result
            
            # Wait before next check
            await asyncio.sleep(check_interval)
            elapsed_in_monitor += check_interval
        
        # Monitoring period ended, still in progress
        total_elapsed = asyncio.get_event_loop().time() - start_time
        
        # Format ETA nicely
        if eta_seconds:
            if eta_seconds > 60:
                eta_str = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
            else:
                eta_str = f"{int(eta_seconds)}s"
        else:
            eta_str = "calculating..."
        
        result = {
            "DISPLAY_TO_USER": f"🚁 FLYING | Dist: {distance:.0f}m | Alt: {current_alt:.1f}m | Speed: {ground_speed:.1f}m/s | ETA: {eta_str} | {progress:.0f}%",
            "status": "in_progress",
            "distance_m": round(distance, 1),
            "progress_percent": round(progress, 0),
            "action_required": "call monitor_flight again",
            "mission_complete": False
        }
        log_tool_output(result)
        return result
        
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ Monitor flight failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Monitoring failed: {str(e)}"}


@mcp.tool()
async def get_home_position(ctx: Context) -> dict:
    """
    Get the home position where Return to Launch (RTL) will return to.
    This is typically set at the launch location when the drone first arms.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Home position coordinates and altitude.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching home position")
    
    try:
        async for home in drone.telemetry.home():
            home_data = {
                "latitude_deg": home.latitude_deg,
                "longitude_deg": home.longitude_deg,
                "absolute_altitude_m": home.absolute_altitude_m,
            }
            logger.info(f"Home position: {home_data['latitude_deg']}, {home_data['longitude_deg']} at {home_data['absolute_altitude_m']}m")
            return {"status": "success", "home": home_data}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get home position: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Home position read failed: {str(e)}"}

@mcp.tool()
async def set_max_speed(ctx: Context, speed_m_s: float) -> dict:
    """
    Set the maximum speed limit for the drone.
    Useful for safety or when flying in confined areas.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        speed_m_s (float): Maximum speed in meters per second. Typical range: 1-20 m/s.

    Returns:
        dict: Status message with success or error.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    # Validate speed
    if speed_m_s <= 0:
        return {"status": "failed", "error": f"Invalid speed: {speed_m_s}. Must be positive."}
    if speed_m_s > 30:
        return {"status": "failed", "error": f"Speed too high: {speed_m_s} m/s. Maximum is 30 m/s for safety."}
    
    drone = connector.drone
    logger.info(f"Setting maximum speed to {speed_m_s} m/s")
    
    try:
        log_mavlink_cmd("drone.action.set_maximum_speed", speed_m_s=speed_m_s)
        await drone.action.set_maximum_speed(speed_m_s)
        return {
            "status": "success", 
            "message": f"Maximum speed set to {speed_m_s} m/s",
            "speed_kmh": round(speed_m_s * 3.6, 1)  # Also provide in km/h
        }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to set max speed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Set max speed failed: {str(e)}"}

# ============================================================================
# PRIORITY 4: TELEMETRY & MONITORING (v1.1.0)
# ============================================================================

@mcp.tool()
async def get_speed(ctx: Context) -> dict:
    """
    Get the current ground speed (velocity over ground).
    Returns velocity in North, East, Down directions.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Current velocity in NED frame and total ground speed.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching ground speed")
    
    try:
        async for velocity in drone.telemetry.velocity_ned():
            # Calculate total ground speed (horizontal speed only)
            ground_speed_m_s = math.sqrt(velocity.north_m_s**2 + velocity.east_m_s**2)
            
            speed_data = {
                "north_m_s": velocity.north_m_s,
                "east_m_s": velocity.east_m_s,
                "down_m_s": velocity.down_m_s,
                "ground_speed_m_s": round(ground_speed_m_s, 2),
                "ground_speed_kmh": round(ground_speed_m_s * 3.6, 2),
            }
            
            logger.info(f"Ground speed: {speed_data['ground_speed_m_s']} m/s ({speed_data['ground_speed_kmh']} km/h)")
            return {"status": "success", "velocity": speed_data}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get speed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Speed read failed: {str(e)}"}

@mcp.tool()
async def get_attitude(ctx: Context) -> dict:
    """
    Get the current attitude (orientation) of the drone.
    Returns roll, pitch, and yaw angles in degrees.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Roll, pitch, yaw angles in degrees.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching attitude")
    
    try:
        async for attitude in drone.telemetry.attitude_euler():
            attitude_data = {
                "roll_deg": round(attitude.roll_deg, 2),
                "pitch_deg": round(attitude.pitch_deg, 2),
                "yaw_deg": round(attitude.yaw_deg, 2),
            }
            
            logger.info(f"Attitude: roll={attitude_data['roll_deg']}°, pitch={attitude_data['pitch_deg']}°, yaw={attitude_data['yaw_deg']}°")
            return {"status": "success", "attitude": attitude_data}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get attitude: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Attitude read failed: {str(e)}"}

@mcp.tool()
async def get_gps_info(ctx: Context) -> dict:
    """
    Get detailed GPS information including number of satellites and fix type.
    Important for assessing navigation quality.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: GPS satellite count, fix type, and quality metrics.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Fetching GPS info")
    
    try:
        async for gps_info in drone.telemetry.gps_info():
            gps_data = {
                "num_satellites": gps_info.num_satellites,
                "fix_type": str(gps_info.fix_type),
            }
            
            # Add quality assessment
            if gps_info.num_satellites >= 10:
                gps_data["quality"] = "Excellent"
            elif gps_info.num_satellites >= 6:
                gps_data["quality"] = "Good"
            elif gps_info.num_satellites >= 4:
                gps_data["quality"] = "Marginal"
            else:
                gps_data["quality"] = "Poor"
                gps_data["warning"] = "⚠️  Insufficient satellites for reliable navigation!"
            
            logger.info(f"GPS: {gps_data['num_satellites']} satellites, {gps_data['fix_type']}, {gps_data['quality']}")
            return {"status": "success", "gps": gps_data}
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get GPS info: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"GPS info read failed: {str(e)}"}

@mcp.tool()
async def get_in_air(ctx: Context) -> dict:
    """
    Check if the drone is currently in the air (flying) or on the ground.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Boolean indicating if drone is airborne.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Checking if drone is in air")
    
    try:
        async for in_air in drone.telemetry.in_air():
            status_text = "IN AIR (flying)" if in_air else "ON GROUND"
            logger.info(f"{LogColors.STATUS}Drone status: {status_text}{LogColors.RESET}")
            return {
                "status": "success", 
                "in_air": in_air,
                "status_text": status_text
            }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to check in_air status: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"In-air check failed: {str(e)}"}

@mcp.tool()
async def get_armed(ctx: Context) -> dict:
    """
    Check if the drone is currently armed (motors can spin).
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Boolean indicating if drone is armed.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Checking if drone is armed")
    
    try:
        async for armed in drone.telemetry.armed():
            status_text = "ARMED (motors ready)" if armed else "DISARMED (motors off)"
            logger.info(f"{LogColors.STATUS}Drone status: {status_text}{LogColors.RESET}")
            return {
                "status": "success", 
                "armed": armed,
                "status_text": status_text
            }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to check armed status: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Armed check failed: {str(e)}"}

# ============================================================================
# v1.2.0: PARAMETER MANAGEMENT
# ============================================================================

@mcp.tool()
async def get_parameter(ctx: Context, name: str, param_type: str = "auto") -> dict:
    """
    Get the value of a drone parameter by name.
    Parameters control drone behavior (e.g., flight speeds, sensor settings).
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        name (str): Parameter name (e.g., "RTL_ALT", "WPNAV_SPEED", "BATT_CAPACITY").
        param_type (str): Type of parameter - "int", "float", or "auto" (default: auto).
                          If "auto", will try float first, then int.

    Returns:
        dict: Parameter value and type, or error if parameter not found.
    
    Examples:
        - get_parameter("RTL_ALT", "float") - Get return-to-launch altitude
        - get_parameter("BATT_CAPACITY", "int") - Get battery capacity in mAh
        - get_parameter("WPNAV_SPEED") - Auto-detect parameter type
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info(f"Getting parameter: {name} (type: {param_type})")
    
    try:
        if param_type == "int":
            value = await drone.param.get_param_int(name)
            return {
                "status": "success",
                "name": name,
                "value": value,
                "type": "int"
            }
        elif param_type == "float":
            value = await drone.param.get_param_float(name)
            return {
                "status": "success",
                "name": name,
                "value": value,
                "type": "float"
            }
        else:  # auto-detect
            # Try float first (most common)
            try:
                value = await drone.param.get_param_float(name)
                return {
                    "status": "success",
                    "name": name,
                    "value": value,
                    "type": "float"
                }
            except:
                # If float fails, try int
                value = await drone.param.get_param_int(name)
                return {
                    "status": "success",
                    "name": name,
                    "value": value,
                    "type": "int"
                }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get parameter {name}: {e}{LogColors.RESET}")
        return {
            "status": "failed", 
            "error": f"Parameter '{name}' not found or inaccessible: {str(e)}",
            "suggestion": "Check parameter name spelling. Use list_parameters to see available parameters."
        }

@mcp.tool()
async def set_parameter(ctx: Context, name: str, value: float, param_type: str = "auto") -> dict:
    """
    Set the value of a drone parameter by name.
    ⚠️ WARNING: Changing parameters can affect flight behavior. Only modify if you know what you're doing!
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        name (str): Parameter name (e.g., "RTL_ALT", "WPNAV_SPEED").
        value (float): New parameter value.
        param_type (str): Type of parameter - "int", "float", or "auto" (default: auto).
                          If "auto", will detect based on value (int if no decimal).

    Returns:
        dict: Confirmation of parameter change with old and new values.
    
    Examples:
        - set_parameter("RTL_ALT", 1500.0, "float") - Set RTL altitude to 15m
        - set_parameter("BATT_CAPACITY", 5200, "int") - Set battery capacity to 5200 mAh
    
    ⚠️ CAUTION: 
        - Invalid parameters can make the drone unflyable
        - Always verify values are within safe ranges
        - Consider backing up parameters before changes
        - Some parameters require reboot to take effect
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.warning(f"⚠️ Setting parameter: {name} = {value} (type: {param_type})")
    
    try:
        # Get old value first
        try:
            if param_type == "int" or (param_type == "auto" and value == int(value)):
                old_value = await drone.param.get_param_int(name)
                param_type_final = "int"
            else:
                old_value = await drone.param.get_param_float(name)
                param_type_final = "float"
        except:
            old_value = None
            # Assume float if we can't get old value
            param_type_final = "float" if param_type == "auto" else param_type
        
        # Set new value
        if param_type_final == "int":
            log_mavlink_cmd("drone.param.set_param_int", name=name, value=int(value))
            await drone.param.set_param_int(name, int(value))
        else:
            log_mavlink_cmd("drone.param.set_param_float", name=name, value=float(value))
            await drone.param.set_param_float(name, float(value))
        
        logger.info(f"{LogColors.SUCCESS}✓ Parameter {name} changed from {old_value} to {value}{LogColors.RESET}")
        
        return {
            "status": "success",
            "name": name,
            "old_value": old_value,
            "new_value": int(value) if param_type_final == "int" else float(value),
            "type": param_type_final,
            "message": f"Parameter '{name}' set to {value}",
            "warning": "Some parameters may require a reboot to take effect."
        }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to set parameter {name}: {e}{LogColors.RESET}")
        return {
            "status": "failed", 
            "error": f"Failed to set parameter '{name}': {str(e)}",
            "suggestion": "Verify parameter name and value are valid for this drone."
        }

@mcp.tool()
async def list_parameters(ctx: Context, filter_prefix: str = "") -> dict:
    """
    List all available drone parameters.
    This can return a large number of parameters (100-1000+).
    Optionally filter by prefix to narrow results.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        filter_prefix (str): Optional prefix to filter parameters (e.g., "BATT" for battery params).
                            Leave empty to get all parameters.

    Returns:
        dict: List of all parameters with their names, values, and types.
    
    Examples:
        - list_parameters() - Get ALL parameters (may be very long!)
        - list_parameters("RTL") - Get all Return-to-Launch parameters
        - list_parameters("BATT") - Get all battery-related parameters
        - list_parameters("WPNAV") - Get all waypoint navigation parameters
    
    Common Parameter Prefixes:
        - RTL_ : Return to Launch settings
        - BATT_ : Battery settings
        - WPNAV_ : Waypoint navigation
        - EK2_ / EK3_ : EKF (Extended Kalman Filter) settings
        - COMPASS_ : Compass/magnetometer settings
        - GPS_ : GPS settings
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info(f"Listing parameters{f' (filter: {filter_prefix}*)' if filter_prefix else ''}")
    
    try:
        log_mavlink_cmd("drone.param.get_all_params", filter_prefix=filter_prefix if filter_prefix else "none")
        all_params = await drone.param.get_all_params()
        
        # Filter if prefix provided
        if filter_prefix:
            filter_upper = filter_prefix.upper()
            filtered = []
            for param in all_params.int_params:
                if param.name.upper().startswith(filter_upper):
                    filtered.append({"name": param.name, "value": param.value, "type": "int"})
            for param in all_params.float_params:
                if param.name.upper().startswith(filter_upper):
                    filtered.append({"name": param.name, "value": param.value, "type": "float"})
            
            filtered.sort(key=lambda x: x["name"])
            logger.info(f"Found {len(filtered)} parameters matching '{filter_prefix}*'")
            
            return {
                "status": "success",
                "filter": filter_prefix,
                "count": len(filtered),
                "parameters": filtered
            }
        else:
            # Return all parameters
            params_list = []
            for param in all_params.int_params:
                params_list.append({"name": param.name, "value": param.value, "type": "int"})
            for param in all_params.float_params:
                params_list.append({"name": param.name, "value": param.value, "type": "float"})
            
            params_list.sort(key=lambda x: x["name"])
            logger.info(f"Found {len(params_list)} total parameters")
            
            return {
                "status": "success",
                "count": len(params_list),
                "parameters": params_list,
                "warning": f"This is a large list ({len(params_list)} parameters). Consider using filter_prefix to narrow results."
            }
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to list parameters: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Failed to retrieve parameters: {str(e)}"}

# ============================================================================
# v1.2.0: ADVANCED NAVIGATION
# ============================================================================
@mcp.tool()
async def set_yaw(ctx: Context, yaw_deg: float, yaw_rate_deg_s: float = 30.0) -> dict:
    """
    Set the drone's heading (yaw) without changing position.
    Rotates the drone to face a specific direction.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        yaw_deg (float): Target heading in degrees (0-360, where 0/360 is North).
        yaw_rate_deg_s (float): Rotation speed in degrees per second (default: 30).

    Returns:
        dict: Status message with target heading.
    
    Examples:
        - set_yaw(0) - Face North
        - set_yaw(90) - Face East
        - set_yaw(180) - Face South
        - set_yaw(270) - Face West
        - set_yaw(45, 15) - Face Northeast at 15 deg/s rotation speed
    
    Note:
        - 0° = North, 90° = East, 180° = South, 270° = West
        - Drone will rotate in place to face the specified direction
        - Implementation: Uses goto_location with current position + new yaw
          (MAVSDK doesn't have a dedicated "yaw only" command)
    """
    log_tool_call("set_yaw", yaw_deg=yaw_deg, yaw_rate_deg_s=yaw_rate_deg_s)
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    # Normalize yaw to 0-360
    yaw_normalized = yaw_deg % 360
    
    # Validate yaw rate
    if yaw_rate_deg_s <= 0:
        return {"status": "failed", "error": f"Invalid yaw rate: {yaw_rate_deg_s}. Must be positive."}
    
    drone = connector.drone
    logger.info(f"Setting yaw to {yaw_normalized}° at {yaw_rate_deg_s}°/s")
    
    try:
        # WORKAROUND: MAVSDK doesn't have a "set yaw only" command
        # We use goto_location with current position + new yaw
        # This tells the drone to "fly to where you already are, but face this direction"
        async for position in drone.telemetry.position():
            current_lat = position.latitude_deg
            current_lon = position.longitude_deg
            current_alt = position.absolute_altitude_m
            current_rel_alt = position.relative_altitude_m
            
            logger.info(f"Reading current position: ({current_lat:.6f}, {current_lon:.6f}) @ {current_rel_alt:.1f}m AGL")
            logger.info(f"Commanding: same position, new yaw = {yaw_normalized}°")
            
            # Use goto_location with current position but new yaw
            # This is the standard MAVSDK workaround for yaw-only control
            log_mavlink_cmd("drone.action.goto_location", lat=f"{current_lat:.6f}", 
                           lon=f"{current_lon:.6f}", alt=f"{current_alt:.1f}", 
                           yaw=f"{yaw_normalized:.1f}")
            await drone.action.goto_location(
                current_lat,
                current_lon,
                current_alt,
                yaw_normalized
            )
            
            # Convert heading to cardinal direction
            directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
            direction_index = int((yaw_normalized + 22.5) / 45) % 8
            cardinal = directions[direction_index]
            
            logger.info(f"{LogColors.SUCCESS}✓ Yaw set to {yaw_normalized}° ({cardinal}){LogColors.RESET}")
            
            return {
                "status": "success",
                "message": f"Rotating to heading {yaw_normalized}°",
                "yaw_degrees": yaw_normalized,
                "cardinal_direction": cardinal,
                "yaw_rate_deg_s": yaw_rate_deg_s
            }
    except Exception as e:
        logger.error(f"Set yaw failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Yaw control failed: {str(e)}"}

@mcp.tool()
async def reposition(
    ctx: Context,
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float
) -> dict:
    """
    Move to a new location and loiter (hover) there.
    Combination of goto_location and hold_position.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        latitude_deg (float): Target latitude in degrees.
        longitude_deg (float): Target longitude in degrees.
        altitude_m (float): Target altitude above sea level in meters.

    Returns:
        dict: Status message with target position.
    
    Examples:
        - reposition(33.645, -117.842, 50) - Move to coordinates and hover at 50m
        - reposition(33.646, -117.843, 100) - Reposition to new survey point
    
    Use Cases:
        - Adjusting survey position
        - Moving to better vantage point
        - Relocating between tasks
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    # Validate coordinates
    if not (-90 <= latitude_deg <= 90):
        return {"status": "failed", "error": f"Invalid latitude: {latitude_deg}. Must be between -90 and 90."}
    if not (-180 <= longitude_deg <= 180):
        return {"status": "failed", "error": f"Invalid longitude: {longitude_deg}. Must be between -180 and 180."}
    
    drone = connector.drone
    
    try:
        # Get current position to calculate relative altitude for display
        position = await drone.telemetry.position().__anext__()
        home_alt = position.absolute_altitude_m - position.relative_altitude_m
        relative_alt = altitude_m - home_alt
        
        logger.info(f"Repositioning to ({latitude_deg}, {longitude_deg}) at {relative_alt:.1f}m AGL (relative) / {altitude_m:.1f}m MSL")
        
        # Move to new location (will loiter automatically in GUIDED mode)
        log_mavlink_cmd("drone.action.goto_location", lat=f"{latitude_deg:.6f}", 
                       lon=f"{longitude_deg:.6f}", alt=f"{altitude_m:.1f}", yaw="nan")
        await drone.action.goto_location(
            latitude_deg,
            longitude_deg,
            altitude_m,
            float('nan')  # Maintain current heading
        )
        
        return {
            "status": "success",
            "message": "Repositioning to new location",
            "target": {
                "latitude": latitude_deg,
                "longitude": longitude_deg,
                "altitude_msl": altitude_m
            },
            "note": "Drone will fly to location and loiter (hover) there"
        }
    except Exception as e:
        logger.error(f"Reposition failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Reposition failed: {str(e)}"}

# ============================================================================
# v1.2.0: MISSION ENHANCEMENTS
# ============================================================================

@mcp.tool()
async def upload_mission(ctx: Context, waypoints: list) -> dict:
    """
    Upload a mission to the drone WITHOUT starting it.
    Allows preparing missions in advance.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        waypoints (list): List of waypoint dictionaries with keys:
                         - latitude_deg (float): Waypoint latitude
                         - longitude_deg (float): Waypoint longitude
                         - relative_altitude_m (float): Altitude above home
                         - speed_m_s (float, optional): Speed to waypoint

    Returns:
        dict: Status message with mission summary.
    
    Examples:
        waypoints = [
            {"latitude_deg": 33.645, "longitude_deg": -117.842, "relative_altitude_m": 10},
            {"latitude_deg": 33.646, "longitude_deg": -117.843, "relative_altitude_m": 15},
            {"latitude_deg": 33.647, "longitude_deg": -117.844, "relative_altitude_m": 20}
        ]
        upload_mission(waypoints)
    
    Note:
        - Mission is uploaded but NOT started automatically
        - Use initiate_mission or start_mission to begin execution
        - Clears any existing mission first
        
    Important:
        Pass waypoints as a properly formatted list of dictionaries.
        Each waypoint MUST have: latitude_deg, longitude_deg, relative_altitude_m
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    # Validate waypoints input
    if not waypoints:
        return {
            "status": "failed", 
            "error": "No waypoints provided",
            "example": [
                {"latitude_deg": 33.645, "longitude_deg": -117.842, "relative_altitude_m": 10},
                {"latitude_deg": 33.646, "longitude_deg": -117.843, "relative_altitude_m": 15}
            ]
        }
    
    if not isinstance(waypoints, list):
        return {
            "status": "failed",
            "error": f"Waypoints must be a list, got {type(waypoints).__name__}",
            "hint": "Pass waypoints as: [{'latitude_deg': 33.645, 'longitude_deg': -117.842, 'relative_altitude_m': 10}]"
        }
    
    drone = connector.drone
    logger.info(f"Uploading mission with {len(waypoints)} waypoints")
    
    try:
        # Validate and create mission items
        mission_items = []
        for i, wp in enumerate(waypoints):
            # Type check
            if not isinstance(wp, dict):
                return {
                    "status": "failed",
                    "error": f"Waypoint {i} must be a dictionary, got {type(wp).__name__}",
                    "hint": "Each waypoint needs: latitude_deg, longitude_deg, relative_altitude_m"
                }
            
            # Required field check
            required_fields = ["latitude_deg", "longitude_deg", "relative_altitude_m"]
            missing = [f for f in required_fields if f not in wp]
            if missing:
                return {
                    "status": "failed",
                    "error": f"Waypoint {i} missing required fields: {', '.join(missing)}",
                    "received": list(wp.keys()),
                    "required": required_fields
                }
            
            # Validate coordinates
            if not (-90 <= wp["latitude_deg"] <= 90):
                return {"status": "failed", "error": f"Waypoint {i}: invalid latitude {wp['latitude_deg']} (must be -90 to 90)"}
            if not (-180 <= wp["longitude_deg"] <= 180):
                return {"status": "failed", "error": f"Waypoint {i}: invalid longitude {wp['longitude_deg']} (must be -180 to 180)"}
            if wp["relative_altitude_m"] < 0:
                return {"status": "failed", "error": f"Waypoint {i}: altitude cannot be negative"}
            
            # Use mission_raw format (ArduPilot-compatible)
            # MAVLink uses lat/lon * 1e7 as integers
            mission_item = MissionItem(
                seq=i,  # Sequence number
                frame=3,  # MAV_FRAME_GLOBAL_RELATIVE_ALT
                command=16,  # MAV_CMD_NAV_WAYPOINT
                current=1 if i == 0 else 0,  # First waypoint is current
                autocontinue=1,  # Auto-continue to next waypoint
                param1=0,  # Hold time (seconds)
                param2=2.0,  # Acceptance radius (meters)
                param3=0,  # Pass radius (meters)
                param4=float('nan'),  # Yaw angle (NaN = don't change)
                x=int(wp["latitude_deg"] * 1e7),  # Latitude * 1e7
                y=int(wp["longitude_deg"] * 1e7),  # Longitude * 1e7
                z=float(wp["relative_altitude_m"]),  # Altitude (meters)
                mission_type=0  # MAV_MISSION_TYPE_MISSION
            )
            mission_items.append(mission_item)
        
        # Upload mission using mission_raw (ArduPilot-compatible)
        log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(waypoints))
        await drone.mission_raw.upload_mission(mission_items)
        
        logger.info(f"{LogColors.SUCCESS}✓ Mission uploaded successfully: {len(waypoints)} waypoints{LogColors.RESET}")
        
        return {
            "status": "success",
            "message": f"Mission uploaded with {len(waypoints)} waypoints",
            "waypoint_count": len(waypoints),
            "waypoints_summary": [
                f"WP{i}: ({wp['latitude_deg']:.5f}, {wp['longitude_deg']:.5f}) @ {wp['relative_altitude_m']}m"
                for i, wp in enumerate(waypoints)
            ],
            "note": "Mission uploaded but NOT started. Use initiate_mission to start."
        }
    except Exception as e:
        logger.error(f"Mission upload failed: {e}{LogColors.RESET}")
        return {
            "status": "failed", 
            "error": f"Mission upload failed: {str(e)}",
            "troubleshooting": "Ensure waypoints are formatted correctly as list of dictionaries"
        }

@mcp.tool()
async def download_mission(ctx: Context) -> dict:
    """
    Download the current mission from the drone.
    Retrieves all waypoints stored on the drone.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Mission data with all waypoints.
    
    Use Cases:
        - Backup current mission
        - Verify uploaded mission
        - Check drone's planned route
        - Mission debugging
    
    Note:
        Mission download may occasionally fail with "UNSUPPORTED" immediately after upload
        due to ArduPilot mission state synchronization. If this happens, wait a moment and
        try again, or use mission_progress() to verify the mission exists.
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Downloading mission from drone")
    
    # Check mission progress first to verify mission exists
    try:
        log_mavlink_cmd("drone.mission.mission_progress")
        async for progress in drone.mission.mission_progress():
            logger.info(f"{LogColors.STATUS}Mission has {progress.total} waypoints, currently at {progress.current}{LogColors.RESET}")
            if progress.total == 0:
                return {
                    "status": "failed",
                    "error": "No mission on drone (mission count is 0)",
                    "hint": "Upload a mission first using upload_mission"
                }
            break
    except Exception as e:
        logger.warning(f"Could not check mission progress: {e}")
    
    # Try to download mission with proper retry logic
    max_retries = 5  # Increased retries
    retry_delay = 0.3  # Shorter, more frequent retries
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.info(f"Retry attempt {attempt + 1}/{max_retries} after {retry_delay}s delay...")
                await asyncio.sleep(retry_delay)
            
            log_mavlink_cmd("drone.mission_raw.download_mission")
            mission_items = await drone.mission_raw.download_mission()
            
            # Convert raw mission items to dict format
            # Filter for waypoint commands only (command 16 = MAV_CMD_NAV_WAYPOINT)
            waypoints = []
            for item in mission_items:
                if item.command == 16:  # MAV_CMD_NAV_WAYPOINT
                    waypoints.append({
                        "seq": item.seq,
                        "latitude_deg": item.x / 1e7,  # Convert from int * 1e7 to float
                        "longitude_deg": item.y / 1e7,  # Convert from int * 1e7 to float
                        "relative_altitude_m": item.z,
                        "frame": item.frame,
                        "command": item.command
                    })
            
            logger.info(f"{LogColors.SUCCESS}✓ Downloaded mission with {len(waypoints)} waypoints (from {len(mission_items)} total items){LogColors.RESET}")
            
            return {
                "status": "success",
                "waypoint_count": len(waypoints),
                "waypoints": waypoints,
                "note": f"Downloaded on attempt {attempt + 1}" if attempt > 0 else None
            }
            
        except Exception as e:
            error_str = str(e)
            
            # If UNSUPPORTED and not last attempt, retry
            if "UNSUPPORTED" in error_str.upper() and attempt < max_retries - 1:
                logger.warning(f"Mission download attempt {attempt + 1} failed (UNSUPPORTED), retrying...")
                continue
            
            # Last attempt or different error - report it
            logger.error(f"Mission download failed after {attempt + 1} attempts: {e}{LogColors.RESET}")
            
            # Provide helpful error message
            if "UNSUPPORTED" in error_str.upper():
                logger.error(f"{LogColors.ERROR}Mission download failed - ArduPilot may need mission state refresh{LogColors.RESET}")
                return {
                    "status": "failed", 
                    "error": "Mission download UNSUPPORTED by current autopilot state",
                    "hint": "Try waiting a moment after upload, or use mission_progress() to verify mission exists",
                    "mission_exists": "Mission was successfully uploaded and verified via mission_progress",
                    "attempts": attempt + 1,
                    "technical_error": error_str,
                    "workaround": "Use is_mission_finished() to monitor mission execution even without download"
                }
            else:
                return {
                    "status": "failed", 
                    "error": f"Mission download failed: {error_str}",
                    "hint": "Ensure a mission has been uploaded to the drone",
                    "attempts": attempt + 1
                }

@mcp.tool()
async def set_current_waypoint(ctx: Context, waypoint_index: int) -> dict:
    """
    Jump to a specific waypoint in the current mission.
    Allows skipping ahead or going back in a mission.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.
        waypoint_index (int): Waypoint number to jump to (0-based index).

    Returns:
        dict: Status message with new current waypoint.
    
    Examples:
        - set_current_waypoint(0) - Jump to first waypoint (restart mission)
        - set_current_waypoint(5) - Skip to waypoint 5
        - set_current_waypoint(3) - Go back to waypoint 3
    
    Use Cases:
        - Skip completed waypoints
        - Restart mission from beginning
        - Re-survey specific area
        - Mission recovery after interruption
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    if waypoint_index < 0:
        return {"status": "failed", "error": f"Invalid waypoint index: {waypoint_index}. Must be 0 or greater."}
    
    drone = connector.drone
    logger.info(f"Setting current mission waypoint to index {waypoint_index}")
    
    try:
        log_mavlink_cmd("drone.mission.set_current_mission_item", waypoint_index=waypoint_index)
        await drone.mission.set_current_mission_item(waypoint_index)
        
        logger.info(f"{LogColors.SUCCESS}✓ Current waypoint set to index {waypoint_index}{LogColors.RESET}")
        
        return {
            "status": "success",
            "message": f"Current waypoint set to index {waypoint_index}",
            "waypoint_index": waypoint_index,
            "note": "Mission will continue from this waypoint"
        }
    except Exception as e:
        logger.error(f"Set current waypoint failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Set waypoint failed: {str(e)}"}

@mcp.tool()
async def is_mission_finished(ctx: Context) -> dict:
    """
    Check if the current mission has completed.
    Returns true if all waypoints have been reached.
    Waits for connection if not ready.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Boolean indicating if mission is finished.
    
    Use Cases:
        - Monitor mission completion
        - Trigger post-mission actions
        - Mission automation
        - Status monitoring
    """
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Checking if mission is finished")
    
    try:
        # Check mission finished status
        log_mavlink_cmd("drone.mission.is_mission_finished")
        finished = await drone.mission.is_mission_finished()
        
        # Get current waypoint progress
        current_wp = 0
        total_wp = 0
        async for mission_progress in drone.mission.mission_progress():
            current_wp = mission_progress.current
            total_wp = mission_progress.total
            break
        
        # Get current flight mode
        try:
            flight_mode = await drone.telemetry.flight_mode().__anext__()
        except:
            flight_mode = "UNKNOWN"
        
        status_text = "FINISHED" if finished else "IN PROGRESS"
        logger.info(f"Mission status: {status_text} - Waypoint {current_wp}/{total_wp} - Mode: {flight_mode}")
        
        return {
            "status": "success",
            "mission_finished": finished,
            "status_text": status_text,
            "current_waypoint": current_wp,
            "total_waypoints": total_wp,
            "flight_mode": str(flight_mode),
            "progress_percentage": round((current_wp / total_wp * 100) if total_wp > 0 else 0, 1)
        }
    except Exception as e:
        logger.error(f"Check mission finished failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Mission status check failed: {str(e)}"}


# ============================================================================
# ENHANCED TELEMETRY TOOLS
# ============================================================================

@mcp.tool()
async def get_health_all_ok(ctx: Context) -> dict:
    """
    Quick health check - returns True if ALL systems are OK for flight.
    This is a simplified check that returns a single boolean rather than
    detailed health status per subsystem.
    
    Use this for quick pre-flight go/no-go decisions.
    For detailed health breakdown, use get_health() instead.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Boolean indicating if all systems pass health checks.
    """
    log_tool_call("get_health_all_ok")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Checking if all systems are healthy")
    
    try:
        async for health_all_ok in drone.telemetry.health_all_ok():
            status_text = "ALL SYSTEMS GO ✓" if health_all_ok else "SYSTEMS NOT READY ✗"
            logger.info(f"{LogColors.STATUS}Health check: {status_text}{LogColors.RESET}")
            
            result = {
                "status": "success",
                "health_all_ok": health_all_ok,
                "status_text": status_text,
                "recommendation": "Ready for flight" if health_all_ok else "Run get_health() for details on what's not ready"
            }
            log_tool_output(result)
            return result
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to check health_all_ok: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Health check failed: {str(e)}"}


@mcp.tool()
async def get_landed_state(ctx: Context) -> dict:
    """
    Get detailed landed state of the drone.
    Returns one of: ON_GROUND, TAKING_OFF, IN_AIR, LANDING, or UNKNOWN.
    
    More detailed than get_in_air() which only tells you if drone is airborne.
    This tells you the transition states (taking off, landing) as well.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Landed state enum value and descriptive text.
    """
    log_tool_call("get_landed_state")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Checking landed state")
    
    try:
        async for landed_state in drone.telemetry.landed_state():
            state_str = str(landed_state)
            
            # Map enum to human-readable description
            state_descriptions = {
                "UNKNOWN": "State cannot be determined",
                "ON_GROUND": "Drone is on the ground, not moving",
                "IN_AIR": "Drone is flying/airborne",
                "TAKING_OFF": "Drone is in the process of taking off",
                "LANDING": "Drone is in the process of landing"
            }
            
            # Extract enum name from string representation
            state_name = state_str.split(".")[-1] if "." in state_str else state_str
            description = state_descriptions.get(state_name, state_str)
            
            logger.info(f"{LogColors.STATUS}Landed state: {state_name} - {description}{LogColors.RESET}")
            
            result = {
                "status": "success",
                "landed_state": state_name,
                "description": description,
                "is_on_ground": state_name == "ON_GROUND",
                "is_in_air": state_name == "IN_AIR",
                "is_transitioning": state_name in ["TAKING_OFF", "LANDING"]
            }
            log_tool_output(result)
            return result
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get landed state: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Landed state read failed: {str(e)}"}


@mcp.tool()
async def get_rc_status(ctx: Context) -> dict:
    """
    Get RC (Remote Control) controller connection status and signal strength.
    Shows whether an RC transmitter is connected and the signal quality.
    
    Useful for monitoring RC link health during manual/assisted flight.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: RC availability status and signal strength percentage.
    """
    log_tool_call("get_rc_status")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Checking RC controller status")
    
    try:
        async for rc_status in drone.telemetry.rc_status():
            is_available = rc_status.is_available
            signal_strength = rc_status.signal_strength_percent
            
            # Determine signal quality
            if not is_available:
                quality = "NO RC CONNECTED"
            elif signal_strength >= 80:
                quality = "Excellent"
            elif signal_strength >= 60:
                quality = "Good"
            elif signal_strength >= 40:
                quality = "Fair"
            elif signal_strength >= 20:
                quality = "Poor"
            else:
                quality = "Critical - Link may be lost"
            
            status_text = f"RC {'Available' if is_available else 'Not Available'} - Signal: {signal_strength:.0f}% ({quality})"
            logger.info(f"{LogColors.STATUS}RC Status: {status_text}{LogColors.RESET}")
            
            result = {
                "status": "success",
                "rc_available": is_available,
                "signal_strength_percent": round(signal_strength, 1) if is_available else 0,
                "signal_quality": quality,
                "status_text": status_text
            }
            log_tool_output(result)
            return result
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get RC status: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"RC status read failed: {str(e)}"}


@mcp.tool()
async def get_heading(ctx: Context) -> dict:
    """
    Get the current compass heading of the drone in degrees.
    Returns heading from 0 to 360 degrees where:
    - 0° = North
    - 90° = East  
    - 180° = South
    - 270° = West

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Heading in degrees and cardinal direction.
    """
    log_tool_call("get_heading")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Getting compass heading")
    
    try:
        async for heading in drone.telemetry.heading():
            heading_deg = heading.heading_deg
            
            # Normalize heading to 0-360
            heading_normalized = heading_deg % 360
            if heading_normalized < 0:
                heading_normalized += 360
            
            # Determine cardinal direction
            if heading_normalized >= 337.5 or heading_normalized < 22.5:
                cardinal = "N"
                direction = "North"
            elif heading_normalized < 67.5:
                cardinal = "NE"
                direction = "Northeast"
            elif heading_normalized < 112.5:
                cardinal = "E"
                direction = "East"
            elif heading_normalized < 157.5:
                cardinal = "SE"
                direction = "Southeast"
            elif heading_normalized < 202.5:
                cardinal = "S"
                direction = "South"
            elif heading_normalized < 247.5:
                cardinal = "SW"
                direction = "Southwest"
            elif heading_normalized < 292.5:
                cardinal = "W"
                direction = "West"
            else:
                cardinal = "NW"
                direction = "Northwest"
            
            logger.info(f"{LogColors.STATUS}Heading: {heading_normalized:.1f}° ({direction}){LogColors.RESET}")
            
            result = {
                "status": "success",
                "heading_deg": round(heading_normalized, 1),
                "cardinal_direction": cardinal,
                "direction_name": direction
            }
            log_tool_output(result)
            return result
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get heading: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Heading read failed: {str(e)}"}


@mcp.tool()
async def get_odometry(ctx: Context) -> dict:
    """
    Get combined odometry data: position, velocity, and orientation.
    Returns all motion-related telemetry in a single call.
    
    This is more efficient than calling get_position, get_velocity, 
    and get_attitude separately when you need all three.
    
    Position is in NED (North-East-Down) frame relative to home.
    Velocity is in body frame (forward, right, down).
    Orientation is given as quaternion and can be converted to Euler angles.

    Args:
        ctx (Context): The context of the request.

    Returns:
        dict: Combined position, velocity, and orientation data.
    """
    log_tool_call("get_odometry")
    connector = ctx.request_context.lifespan_context
    
    # Wait for connection
    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout. Please wait and try again."}
    
    drone = connector.drone
    logger.info("Getting odometry data")
    
    try:
        async for odometry in drone.telemetry.odometry():
            # Extract position (NED frame)
            position = {
                "north_m": round(odometry.position_body.x_m, 3),
                "east_m": round(odometry.position_body.y_m, 3),
                "down_m": round(odometry.position_body.z_m, 3),
            }
            
            # Extract velocity (body frame)
            velocity = {
                "forward_m_s": round(odometry.velocity_body.x_m_s, 3),
                "right_m_s": round(odometry.velocity_body.y_m_s, 3),
                "down_m_s": round(odometry.velocity_body.z_m_s, 3),
            }
            
            # Extract orientation quaternion
            quaternion = {
                "w": round(odometry.q.w, 4),
                "x": round(odometry.q.x, 4),
                "y": round(odometry.q.y, 4),
                "z": round(odometry.q.z, 4),
            }
            
            # Convert quaternion to Euler angles for easier interpretation
            # Using standard aerospace convention (roll, pitch, yaw)
            w, x, y, z = odometry.q.w, odometry.q.x, odometry.q.y, odometry.q.z
            
            # Roll (rotation around x-axis)
            sinr_cosp = 2 * (w * x + y * z)
            cosr_cosp = 1 - 2 * (x * x + y * y)
            roll_rad = math.atan2(sinr_cosp, cosr_cosp)
            
            # Pitch (rotation around y-axis)
            sinp = 2 * (w * y - z * x)
            if abs(sinp) >= 1:
                pitch_rad = math.copysign(math.pi / 2, sinp)  # Use 90 degrees if out of range
            else:
                pitch_rad = math.asin(sinp)
            
            # Yaw (rotation around z-axis)
            siny_cosp = 2 * (w * z + x * y)
            cosy_cosp = 1 - 2 * (y * y + z * z)
            yaw_rad = math.atan2(siny_cosp, cosy_cosp)
            
            euler_angles = {
                "roll_deg": round(math.degrees(roll_rad), 2),
                "pitch_deg": round(math.degrees(pitch_rad), 2),
                "yaw_deg": round(math.degrees(yaw_rad), 2),
            }
            
            # Calculate derived values
            ground_speed = math.sqrt(velocity["forward_m_s"]**2 + velocity["right_m_s"]**2)
            total_speed = math.sqrt(ground_speed**2 + velocity["down_m_s"]**2)
            
            logger.info(f"{LogColors.STATUS}Odometry: Pos({position['north_m']:.1f}N, {position['east_m']:.1f}E, {-position['down_m']:.1f}Up) "
                       f"Vel({ground_speed:.1f}m/s ground) Yaw({euler_angles['yaw_deg']:.0f}°){LogColors.RESET}")
            
            result = {
                "status": "success",
                "frame_id": str(odometry.frame_id),
                "child_frame_id": str(odometry.child_frame_id),
                "position_ned_m": position,
                "velocity_body_m_s": velocity,
                "orientation_quaternion": quaternion,
                "euler_angles_deg": euler_angles,
                "ground_speed_m_s": round(ground_speed, 2),
                "total_speed_m_s": round(total_speed, 2),
                "altitude_m": round(-position["down_m"], 2)  # Convert down to up (altitude)
            }
            log_tool_output(result)
            return result
    except Exception as e:
        logger.error(f"{LogColors.ERROR}❌ TOOL ERROR - Failed to get odometry: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Odometry read failed: {str(e)}"}


# ============================================================
# Phase 2A: Mission State Tools
# ============================================================

@mcp.tool()
async def create_mission(ctx: Context, type: str, objective: str, area: dict, params: dict = {}) -> dict:
    """Create a new mission, replacing any existing mission.

    This initializes the mission state system that tracks progress, findings, and decisions
    throughout an autonomous search or survey operation.

    Args:
        ctx: MCP context
        type: Mission type (e.g. "grid_search", "expanding_square", "sector_search", "custom")
        objective: Human-readable mission objective (e.g. "Survey solar farm for panel damage")
        area: Area definition dict. For search patterns, use {north, south, east, west} bounds
              or {center_lat, center_lon, radius} for circular areas.
        params: Optional mission parameters (e.g. altitude, spacing, speed)

    Returns:
        dict with mission ID and initial state
    """
    log_tool_call("create_mission", type=type, objective=objective, area=area, params=params)
    connector = ctx.request_context.lifespan_context

    mission_id = f"mission-{uuid.uuid4().hex[:8]}"
    mission = MissionState(
        id=mission_id,
        type=type,
        status=MissionStatus.CREATED,
        objective=objective,
        area=area,
        params=params,
    )
    connector.current_mission = mission

    result = {
        "status": "success",
        "message": f"Mission {mission_id} created",
        "mission_id": mission_id,
        "type": type,
        "objective": objective,
    }
    log_tool_output(result)
    return result


@mcp.tool()
async def get_mission_state(ctx: Context) -> dict:
    """Get the full mission state as JSON.

    This is the primary context tool — returns everything the LLM needs to understand
    current mission progress: sectors completed, findings logged, decisions made.

    Args:
        ctx: MCP context

    Returns:
        dict with full mission state or error if no active mission
    """
    log_tool_call("get_mission_state")
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission. Use create_mission first."}
        log_tool_output(result)
        return result

    state = connector.current_mission.to_dict()
    result = {"status": "success", **state}
    log_tool_output(result)
    return result


@mcp.tool()
async def get_mission_summary(ctx: Context) -> dict:
    """Get a concise natural language summary of mission progress.

    Lighter than get_mission_state — returns a human-readable summary string
    suitable for quick status checks without flooding context.

    Args:
        ctx: MCP context

    Returns:
        dict with summary string
    """
    log_tool_call("get_mission_summary")
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission. Use create_mission first."}
        log_tool_output(result)
        return result

    summary = connector.current_mission.summary()
    result = {"status": "success", "summary": summary}
    log_tool_output(result)
    return result


@mcp.tool()
async def update_mission_progress(ctx: Context, sector_id: str, status: str) -> dict:
    """Update the status of a mission sector.

    Called as search patterns execute to mark sectors active, completed, or skipped.
    Also used by monitor_search_progress for automatic tracking.

    Args:
        ctx: MCP context
        sector_id: The sector ID (e.g. "pass-0", "leg-2", "sector-1")
        status: New status — one of "pending", "active", "completed", "skipped"

    Returns:
        dict with updated sector info
    """
    log_tool_call("update_mission_progress", sector_id=sector_id, status=status)
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission."}
        log_tool_output(result)
        return result

    try:
        new_status = SectorStatus(status)
    except ValueError:
        result = {"status": "failed", "error": f"Invalid status '{status}'. Use: pending, active, completed, skipped"}
        log_tool_output(result)
        return result

    for sector in connector.current_mission.sectors:
        if sector.id == sector_id:
            sector.status = new_status
            if new_status == SectorStatus.ACTIVE and sector.started_at is None:
                sector.started_at = time.time()
            elif new_status in (SectorStatus.COMPLETED, SectorStatus.SKIPPED):
                sector.completed_at = time.time()
            result = {"status": "success", "sector_id": sector_id, "new_status": status}
            log_tool_output(result)
            return result

    result = {"status": "failed", "error": f"Sector '{sector_id}' not found in mission"}
    log_tool_output(result)
    return result


@mcp.tool()
async def add_finding(
    ctx: Context, type: str, lat: float, lon: float,
    confidence: float, metadata: dict = {}, image_ref: str | None = None
) -> dict:
    """Log a point of interest found during a mission.

    Findings are stored in the mission state and included in get_mission_state responses.
    Use this whenever the drone detects something worth recording.

    Args:
        ctx: MCP context
        type: Finding type (e.g. "damaged_panel", "hotspot", "person", "anomaly")
        lat: Latitude in degrees
        lon: Longitude in degrees
        confidence: Confidence score 0.0-1.0
        metadata: Optional additional data (e.g. description, severity)
        image_ref: Optional reference to a captured image

    Returns:
        dict with finding ID
    """
    log_tool_call("add_finding", type=type, lat=lat, lon=lon, confidence=confidence, metadata=metadata, image_ref=image_ref)
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission."}
        log_tool_output(result)
        return result

    finding_id = f"f-{len(connector.current_mission.findings) + 1}"
    finding = Finding(
        id=finding_id,
        type=type,
        lat=lat,
        lon=lon,
        confidence=confidence,
        metadata=metadata,
        image_ref=image_ref,
    )
    connector.current_mission.findings.append(finding)

    result = {
        "status": "success",
        "finding_id": finding_id,
        "type": type,
        "position": {"lat": lat, "lon": lon},
        "confidence": confidence,
        "total_findings": len(connector.current_mission.findings),
    }
    log_tool_output(result)
    return result


@mcp.tool()
async def log_decision(ctx: Context, trigger: str, action: str, rationale: str) -> dict:
    """Record an adaptive re-tasking decision.

    Decisions are logged with trigger/action/rationale format for experience storage.
    Use this when the drone changes behavior based on findings or conditions.

    Args:
        ctx: MCP context
        trigger: What triggered the decision (e.g. "finding f-1", "low battery", "high-confidence detection")
        action: What action was taken (e.g. "orbit_for_closer_look", "skip_sector", "return_to_base")
        rationale: Why this action was chosen

    Returns:
        dict confirming decision logged
    """
    log_tool_call("log_decision", trigger=trigger, action=action, rationale=rationale)
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission."}
        log_tool_output(result)
        return result

    decision = Decision(trigger=trigger, action=action, rationale=rationale)
    connector.current_mission.decisions.append(decision)

    result = {
        "status": "success",
        "message": f"Decision logged: {action}",
        "total_decisions": len(connector.current_mission.decisions),
    }
    log_tool_output(result)
    return result


# ============================================================
# Phase 2B: Search Pattern Tools
# ============================================================

@mcp.tool()
async def execute_grid_search(
    ctx: Context, bounds: dict, altitude: float, spacing: float, objective: str = "Grid search"
) -> dict:
    """Execute a grid (lawnmower) search pattern over a rectangular area.

    Generates boustrophedon waypoints, creates a mission, uploads to PX4, and starts.
    The drone must be armed and airborne.

    Args:
        ctx: MCP context
        bounds: Area bounds as {north, south, east, west} in latitude/longitude degrees.
                Example: {"north": 47.3984, "south": 47.3972, "east": 8.5467, "west": 8.5453}
        altitude: Flight altitude in meters (relative to home)
        spacing: Distance between passes in meters (e.g. 30 for wide coverage, 10 for detailed)
        objective: Mission objective description

    Returns:
        dict with mission state, waypoint count, estimated flight info
    """
    log_tool_call("execute_grid_search", bounds=bounds, altitude=altitude, spacing=spacing, objective=objective)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    # Validate bounds
    required_keys = ["north", "south", "east", "west"]
    missing = [k for k in required_keys if k not in bounds]
    if missing:
        return {"status": "failed", "error": f"Bounds missing keys: {missing}. Need: {required_keys}"}
    if bounds["north"] <= bounds["south"]:
        return {"status": "failed", "error": "north must be > south"}
    if bounds["east"] <= bounds["west"]:
        return {"status": "failed", "error": "east must be > west"}

    # Generate waypoints
    waypoints, sectors = generate_grid_waypoints(bounds, altitude, spacing)

    if len(waypoints) > 200:
        return {
            "status": "failed",
            "error": f"Too many waypoints ({len(waypoints)}). Reduce area or increase spacing. Max 200.",
        }
    if len(waypoints) < 2:
        return {"status": "failed", "error": "Area too small for grid search."}

    # Create mission state
    mission_id = f"mission-{uuid.uuid4().hex[:8]}"
    mission = MissionState(
        id=mission_id,
        type="grid_search",
        status=MissionStatus.ACTIVE,
        objective=objective,
        area=bounds,
        params={"altitude": altitude, "spacing": spacing},
        sectors=sectors,
    )
    connector.current_mission = mission

    # Build MissionItems and upload to PX4
    drone = connector.drone
    try:
        mission_items = []
        for i, wp in enumerate(waypoints):
            mission_items.append(MissionItem(
                seq=i,
                frame=3,  # MAV_FRAME_GLOBAL_RELATIVE_ALT
                command=16,  # MAV_CMD_NAV_WAYPOINT
                current=1 if i == 0 else 0,
                autocontinue=1,
                param1=0,
                param2=2.0,  # acceptance radius
                param3=0,
                param4=float('nan'),
                x=int(wp["latitude_deg"] * 1e7),
                y=int(wp["longitude_deg"] * 1e7),
                z=float(wp["relative_altitude_m"]),
                mission_type=0,
            ))

        # Append explicit RTL waypoint (PX4 HOLD bug workaround)
        mission_items.append(MissionItem(
            seq=len(mission_items),
            frame=2,  # MAV_FRAME_MISSION
            command=20,  # MAV_CMD_NAV_RETURN_TO_LAUNCH
            current=0,
            autocontinue=1,
            param1=0, param2=0, param3=0, param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(mission_items))
        await drone.mission_raw.upload_mission(mission_items)

        # RTL flag as backup (may not work with mission_raw uploads)
        log_mavlink_cmd("drone.mission.set_return_to_launch_after_mission", return_to_launch=True)
        await drone.mission.set_return_to_launch_after_mission(True)

        # Small delay to let PX4 process the upload before starting
        await asyncio.sleep(0.5)

        log_mavlink_cmd("drone.mission.start_mission")
        await drone.mission.start_mission()

        # Verify PX4 entered mission mode
        await asyncio.sleep(1.0)
        flight_mode = None
        try:
            async for fm in drone.telemetry.flight_mode():
                flight_mode = str(fm)
                break
        except Exception:
            pass

        # Mark first sector as active
        if sectors:
            sectors[0].status = SectorStatus.ACTIVE
            sectors[0].started_at = time.time()

        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="search",
            status="active",
            started_at=time.time(),
            command_tool="execute_grid_search",
            description=f"Grid search: {len(sectors)} passes, {altitude}m alt, {spacing}m spacing",
            waypoint_count=len(waypoints),
            altitude_m=altitude,
            mission_id=mission_id,
        )

        result = {
            "status": "success",
            "message": f"Grid search started: {len(waypoints)} waypoints, {len(sectors)} passes",
            "flight_mode": flight_mode,
            "mission_id": mission_id,
            "waypoint_count": len(waypoints),
            "sector_count": len(sectors),
            "pattern": "boustrophedon (lawnmower)",
            "altitude_m": altitude,
            "spacing_m": spacing,
            "note": "Use get_drone_activity() to track progress. monitor_search_progress() also available for sector details.",
        }
        log_tool_output(result)
        return result

    except Exception as e:
        mission.status = MissionStatus.ABORTED
        logger.error(f"{LogColors.ERROR}Grid search failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Grid search failed: {str(e)}"}


@mcp.tool()
async def execute_expanding_square(
    ctx: Context, center_lat: float, center_lon: float,
    altitude: float, initial_size: float = 50.0, expansion: float = 50.0,
    legs: int = 12, objective: str = "Expanding square search"
) -> dict:
    """Execute a SAR expanding square search pattern.

    Spirals outward from a center point in square legs of increasing length.
    Standard SAR pattern for when the target's last known position is approximate.

    Args:
        ctx: MCP context
        center_lat: Center latitude in degrees
        center_lon: Center longitude in degrees
        altitude: Flight altitude in meters (relative)
        initial_size: Length of first leg in meters (default 50)
        expansion: How much each pair of legs grows in meters (default 50)
        legs: Total number of legs to fly (default 12)
        objective: Mission objective description

    Returns:
        dict with mission state and pattern info
    """
    log_tool_call("execute_expanding_square", center_lat=center_lat, center_lon=center_lon,
                  altitude=altitude, initial_size=initial_size, expansion=expansion, legs=legs, objective=objective)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    waypoints, sectors = generate_expanding_square_waypoints(
        center_lat, center_lon, altitude, initial_size, expansion, legs
    )

    if len(waypoints) > 200:
        return {"status": "failed", "error": f"Too many waypoints ({len(waypoints)}). Reduce legs. Max 200."}

    mission_id = f"mission-{uuid.uuid4().hex[:8]}"
    mission = MissionState(
        id=mission_id,
        type="expanding_square",
        status=MissionStatus.ACTIVE,
        objective=objective,
        area={"center_lat": center_lat, "center_lon": center_lon},
        params={"altitude": altitude, "initial_size": initial_size, "expansion": expansion, "legs": legs},
        sectors=sectors,
    )
    connector.current_mission = mission

    drone = connector.drone
    try:
        mission_items = []
        for i, wp in enumerate(waypoints):
            mission_items.append(MissionItem(
                seq=i,
                frame=3,
                command=16,
                current=1 if i == 0 else 0,
                autocontinue=1,
                param1=0,
                param2=2.0,
                param3=0,
                param4=float('nan'),
                x=int(wp["latitude_deg"] * 1e7),
                y=int(wp["longitude_deg"] * 1e7),
                z=float(wp["relative_altitude_m"]),
                mission_type=0,
            ))

        # Append explicit RTL waypoint (PX4 HOLD bug workaround)
        mission_items.append(MissionItem(
            seq=len(mission_items),
            frame=2,  # MAV_FRAME_MISSION
            command=20,  # MAV_CMD_NAV_RETURN_TO_LAUNCH
            current=0,
            autocontinue=1,
            param1=0, param2=0, param3=0, param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(mission_items))
        await drone.mission_raw.upload_mission(mission_items)

        # RTL flag as backup
        log_mavlink_cmd("drone.mission.set_return_to_launch_after_mission", return_to_launch=True)
        await drone.mission.set_return_to_launch_after_mission(True)

        await asyncio.sleep(0.5)

        log_mavlink_cmd("drone.mission.start_mission")
        await drone.mission.start_mission()

        await asyncio.sleep(1.0)
        flight_mode = None
        try:
            async for fm in drone.telemetry.flight_mode():
                flight_mode = str(fm)
                break
        except Exception:
            pass

        if sectors:
            sectors[0].status = SectorStatus.ACTIVE
            sectors[0].started_at = time.time()

        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="search",
            status="active",
            started_at=time.time(),
            command_tool="execute_expanding_square",
            description=f"Expanding square: {len(sectors)} legs, {altitude}m alt",
            waypoint_count=len(waypoints),
            altitude_m=altitude,
            mission_id=mission_id,
        )

        result = {
            "status": "success",
            "message": f"Expanding square started: {len(waypoints)} waypoints, {len(sectors)} legs",
            "flight_mode": flight_mode,
            "mission_id": mission_id,
            "waypoint_count": len(waypoints),
            "sector_count": len(sectors),
            "pattern": "expanding_square (SAR standard)",
            "altitude_m": altitude,
            "note": "Use get_drone_activity() to track progress. monitor_search_progress() also available for sector details.",
        }
        log_tool_output(result)
        return result

    except Exception as e:
        mission.status = MissionStatus.ABORTED
        logger.error(f"{LogColors.ERROR}Expanding square failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Expanding square failed: {str(e)}"}


@mcp.tool()
async def execute_sector_search(
    ctx: Context, center_lat: float, center_lon: float,
    radius: float, altitude: float, num_sectors: int = 6,
    objective: str = "Sector search"
) -> dict:
    """Execute a pie-slice sector search pattern.

    Divides a circular area into sectors and flies center → arc → sweep → center for each.
    Good for searching around a known point of interest.

    Args:
        ctx: MCP context
        center_lat: Center latitude in degrees
        center_lon: Center longitude in degrees
        radius: Search radius in meters
        altitude: Flight altitude in meters (relative)
        num_sectors: Number of pie slices (default 6)
        objective: Mission objective description

    Returns:
        dict with mission state and pattern info
    """
    log_tool_call("execute_sector_search", center_lat=center_lat, center_lon=center_lon,
                  radius=radius, altitude=altitude, num_sectors=num_sectors, objective=objective)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    waypoints, sectors = generate_sector_search_waypoints(
        center_lat, center_lon, radius, altitude, num_sectors
    )

    if len(waypoints) > 200:
        return {"status": "failed", "error": f"Too many waypoints ({len(waypoints)}). Reduce num_sectors. Max 200."}

    mission_id = f"mission-{uuid.uuid4().hex[:8]}"
    mission = MissionState(
        id=mission_id,
        type="sector_search",
        status=MissionStatus.ACTIVE,
        objective=objective,
        area={"center_lat": center_lat, "center_lon": center_lon, "radius": radius},
        params={"altitude": altitude, "num_sectors": num_sectors},
        sectors=sectors,
    )
    connector.current_mission = mission

    drone = connector.drone
    try:
        mission_items = []
        for i, wp in enumerate(waypoints):
            mission_items.append(MissionItem(
                seq=i,
                frame=3,
                command=16,
                current=1 if i == 0 else 0,
                autocontinue=1,
                param1=0,
                param2=2.0,
                param3=0,
                param4=float('nan'),
                x=int(wp["latitude_deg"] * 1e7),
                y=int(wp["longitude_deg"] * 1e7),
                z=float(wp["relative_altitude_m"]),
                mission_type=0,
            ))

        # Append explicit RTL waypoint (PX4 HOLD bug workaround)
        mission_items.append(MissionItem(
            seq=len(mission_items),
            frame=2,  # MAV_FRAME_MISSION
            command=20,  # MAV_CMD_NAV_RETURN_TO_LAUNCH
            current=0,
            autocontinue=1,
            param1=0, param2=0, param3=0, param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(mission_items))
        await drone.mission_raw.upload_mission(mission_items)

        # RTL flag as backup
        log_mavlink_cmd("drone.mission.set_return_to_launch_after_mission", return_to_launch=True)
        await drone.mission.set_return_to_launch_after_mission(True)

        await asyncio.sleep(0.5)

        log_mavlink_cmd("drone.mission.start_mission")
        await drone.mission.start_mission()

        await asyncio.sleep(1.0)
        flight_mode = None
        try:
            async for fm in drone.telemetry.flight_mode():
                flight_mode = str(fm)
                break
        except Exception:
            pass

        if sectors:
            sectors[0].status = SectorStatus.ACTIVE
            sectors[0].started_at = time.time()

        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="search",
            status="active",
            started_at=time.time(),
            command_tool="execute_sector_search",
            description=f"Sector search: {len(sectors)} sectors, {altitude}m alt, {radius}m radius",
            waypoint_count=len(waypoints),
            altitude_m=altitude,
            mission_id=mission_id,
        )

        result = {
            "status": "success",
            "message": f"Sector search started: {len(waypoints)} waypoints, {len(sectors)} sectors",
            "flight_mode": flight_mode,
            "mission_id": mission_id,
            "waypoint_count": len(waypoints),
            "sector_count": len(sectors),
            "pattern": "sector_search (pie-slice)",
            "altitude_m": altitude,
            "radius_m": radius,
            "note": "Use get_drone_activity() to track progress. monitor_search_progress() also available for sector details.",
        }
        log_tool_output(result)
        return result

    except Exception as e:
        mission.status = MissionStatus.ABORTED
        logger.error(f"{LogColors.ERROR}Sector search failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Sector search failed: {str(e)}"}


@mcp.tool()
async def monitor_search_progress(ctx: Context) -> dict:
    """Check PX4 mission progress and update sector statuses automatically.

    Maps the PX4 current waypoint index back to sectors via stored waypoint_index_range.
    Marks sectors as completed when their waypoints have been passed.
    Returns combined mission state + live telemetry.

    Call this periodically during search pattern execution.

    Args:
        ctx: MCP context

    Returns:
        dict with mission progress, current sector, telemetry, and overall state
    """
    log_tool_call("monitor_search_progress")
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission."}
        log_tool_output(result)
        return result

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    drone = connector.drone
    mission = connector.current_mission

    try:
        # Read telemetry from cache (instant) or fallback to direct reads
        if connector.telemetry:
            snapshot = connector.telemetry.get_snapshot()
            mp = snapshot["mission_progress"]
            current_wp = mp["current"]
            total_wp = mp["total"]
            pos_snap = snapshot["position"]
            position_data = {
                "latitude_deg": pos_snap["lat"],
                "longitude_deg": pos_snap["lon"],
                "relative_altitude_m": pos_snap["alt_relative_m"],
            } if pos_snap else {}
            battery_pct = snapshot["battery_pct"] if snapshot["battery_pct"] != -1 else None
            flight_mode = snapshot["flight_mode"]
        else:
            async def _read_one(aiter, timeout=5.0):
                async for item in aiter:
                    return item
                return None

            current_wp = 0
            total_wp = 0
            try:
                progress = await asyncio.wait_for(_read_one(drone.mission.mission_progress()), timeout=5.0)
                if progress:
                    current_wp = progress.current
                    total_wp = progress.total
            except asyncio.TimeoutError:
                logger.warning(f"{LogColors.YELLOW}mission_progress() timed out — PX4 may not be in mission mode{LogColors.RESET}")

            position_data = {}
            try:
                pos = await asyncio.wait_for(_read_one(drone.telemetry.position()), timeout=5.0)
                if pos:
                    position_data = {
                        "latitude_deg": pos.latitude_deg,
                        "longitude_deg": pos.longitude_deg,
                        "relative_altitude_m": pos.relative_altitude_m,
                    }
            except asyncio.TimeoutError:
                logger.warning(f"{LogColors.YELLOW}position() timed out{LogColors.RESET}")

            battery_pct = None
            try:
                bat = await asyncio.wait_for(_read_one(drone.telemetry.battery()), timeout=5.0)
                if bat:
                    battery_pct = round(bat.remaining_percent, 1)
            except (asyncio.TimeoutError, Exception):
                pass

            flight_mode = None
            try:
                fm = await asyncio.wait_for(_read_one(drone.telemetry.flight_mode()), timeout=5.0)
                if fm:
                    flight_mode = str(fm).split(".")[-1]
            except (asyncio.TimeoutError, Exception):
                pass

        # Update sector statuses using position-based proximity
        # (PX4 mission_progress may return 0/0 when using mission_raw upload)
        current_sector_id = None
        drone_lat = position_data.get("latitude_deg")
        drone_lon = position_data.get("longitude_deg")

        if drone_lat is not None and drone_lon is not None:
            WAYPOINT_REACHED_M = 15.0  # consider waypoint reached within this radius

            for sector in mission.sectors:
                if sector.status == SectorStatus.COMPLETED:
                    continue

                # Check if drone is near any waypoint in this sector
                near_sector = False
                last_wp = sector.waypoints[-1] if sector.waypoints else None
                for wp in sector.waypoints:
                    dist = haversine_distance(
                        drone_lat, drone_lon,
                        wp["latitude_deg"], wp["longitude_deg"]
                    )
                    if dist < WAYPOINT_REACHED_M:
                        near_sector = True
                        break

                # Check if drone reached the last waypoint of this sector
                last_wp_reached = False
                if last_wp:
                    last_dist = haversine_distance(
                        drone_lat, drone_lon,
                        last_wp["latitude_deg"], last_wp["longitude_deg"]
                    )
                    last_wp_reached = last_dist < WAYPOINT_REACHED_M

                # Bug 3 fix: if waypoint proximity missed, check sector bounds
                if not near_sector and sector.bounds:
                    b = sector.bounds
                    if (b.get("south", 90) <= drone_lat <= b.get("north", -90) and
                            b.get("west", 180) <= drone_lon <= b.get("east", -180)):
                        near_sector = True

                if near_sector:
                    if sector.status != SectorStatus.ACTIVE:
                        sector.status = SectorStatus.ACTIVE
                        if sector.started_at is None:
                            sector.started_at = time.time()
                    current_sector_id = sector.id
                    break  # Only one sector can be active at a time

            # Mark sectors as completed if the drone has moved past them
            # A sector is complete if it was active and the drone is now in a later sector
            # or near a later sector's waypoints
            if current_sector_id:
                found_current = False
                for sector in mission.sectors:
                    if sector.id == current_sector_id:
                        found_current = True
                        continue
                    if not found_current and sector.status == SectorStatus.ACTIVE:
                        # This sector was active but drone has moved to a later one
                        sector.status = SectorStatus.COMPLETED
                        if sector.completed_at is None:
                            sector.completed_at = time.time()

        # Also use PX4 waypoint index if available (non-zero)
        if current_wp > 0 and total_wp > 0:
            for sector in mission.sectors:
                start_idx, end_idx = sector.waypoint_index_range
                if current_wp > end_idx and sector.status not in (SectorStatus.COMPLETED, SectorStatus.SKIPPED):
                    sector.status = SectorStatus.COMPLETED
                    if sector.completed_at is None:
                        sector.completed_at = time.time()
                elif start_idx <= current_wp <= end_idx and sector.status != SectorStatus.COMPLETED:
                    if sector.status != SectorStatus.ACTIVE:
                        sector.status = SectorStatus.ACTIVE
                        if sector.started_at is None:
                            sector.started_at = time.time()
                    current_sector_id = sector.id

        # Bug 2 fix: when PX4 transitions to HOLD/RTL/LAND after mission,
        # mark all remaining active/pending sectors as complete.
        # Guard: only if at least one sector was previously completed (avoids false trigger on takeoff).
        if flight_mode and flight_mode.upper() in ("HOLD", "RETURN_TO_LAUNCH", "LAND"):
            any_completed = any(s.status == SectorStatus.COMPLETED for s in mission.sectors)
            any_active = any(s.status == SectorStatus.ACTIVE for s in mission.sectors)
            if any_completed or any_active:
                for sector in mission.sectors:
                    if sector.status in (SectorStatus.ACTIVE, SectorStatus.PENDING):
                        sector.status = SectorStatus.COMPLETED
                        if sector.completed_at is None:
                            sector.completed_at = time.time()

        # Check if mission is complete
        all_done = all(s.status == SectorStatus.COMPLETED for s in mission.sectors) if mission.sectors else False
        if all_done:
            mission.status = MissionStatus.COMPLETED
            # Clear mission_raw to fix re-arm bug (proactive — don't wait for get_drone_activity)
            try:
                await drone.mission_raw.clear_mission()
            except Exception:
                pass
            # Mark activity returning or completed based on flight mode
            if connector.current_activity and connector.current_activity.status == "active":
                if flight_mode and flight_mode.upper() in ("RETURN_TO_LAUNCH", "LAND"):
                    connector.current_activity.status = "returning"
                else:
                    connector.current_activity.status = "completed"
                    connector.current_activity.completed_at = time.time()

        state = mission.to_dict()
        result = {
            "status": "success",
            "px4_progress": {"current_waypoint": current_wp, "total_waypoints": total_wp},
            "current_sector": current_sector_id,
            "flight_mode": flight_mode,
            "drone_state": {
                "position": position_data,
                "battery_pct": battery_pct,
            },
            "mission_complete": all_done,
            **state,
        }
        log_tool_output(result)
        return result

    except Exception as e:
        logger.error(f"{LogColors.ERROR}Monitor search progress failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Monitor failed: {str(e)}"}


# ============================================================
# Phase 2C: Vision Pipeline Hooks
# ============================================================

@mcp.tool()
async def capture_image(ctx: Context, label: str = "", camera_name: str = "front_center",
                        image_type: str = "scene") -> dict:
    """Capture an image from the drone's camera.

    When AirSim is connected, captures a real rendered frame.
    Otherwise returns a synthetic image reference with GPS metadata.

    Args:
        ctx: MCP context
        label: Optional label for the image (e.g. "sector-2-start", "anomaly-closeup")
        camera_name: Camera to use — "front_center" (1920x1080) or "bottom_center" (1280x720)
        image_type: Type of image — "scene" (RGB), "depth" (depth map), "segmentation"

    Returns:
        dict with image_ref, position metadata, and source info
    """
    log_tool_call("capture_image", label=label, camera_name=camera_name, image_type=image_type)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    # Get position from telemetry cache
    position_data = {}
    if connector.telemetry:
        snap = connector.telemetry.get_snapshot()
        if snap.get("position"):
            position_data = {
                "latitude_deg": snap["position"]["lat"],
                "longitude_deg": snap["position"]["lon"],
                "relative_altitude_m": snap["position"]["alt_relative_m"],
                "absolute_altitude_m": snap["position"]["alt_msl_m"],
            }

    # Generate image reference
    mission_id = connector.current_mission.id if connector.current_mission else "no-mission"
    image_ref = f"img-{mission_id}-{uuid.uuid4().hex[:6]}"

    # Map image_type string to AirSim ImageType enum
    airsim_type_map = {"scene": 0, "depth": 2, "segmentation": 5}
    airsim_type_int = airsim_type_map.get(image_type, 0)

    png_bytes = b""
    source = "synthetic"
    width = 0
    height = 0

    if connector.airsim_client and AIRSIM_AVAILABLE:
        try:
            loop = asyncio.get_event_loop()
            responses = await loop.run_in_executor(None, lambda: connector.airsim_client.simGetImages([
                airsim.ImageRequest(camera_name, airsim_type_int, False, False)
            ]))
            if responses and len(responses) > 0 and responses[0].width > 0:
                resp = responses[0]
                # Convert raw bytes to PNG
                if CV2_AVAILABLE:
                    img_1d = np.frombuffer(resp.image_data_uint8, dtype=np.uint8)
                    img_bgr = img_1d.reshape(resp.height, resp.width, 3)
                    _, png_buf = cv2.imencode('.png', img_bgr)
                    png_bytes = png_buf.tobytes()
                else:
                    # Fallback: store raw bytes (less efficient but works)
                    png_bytes = bytes(resp.image_data_uint8)
                width = resp.width
                height = resp.height
                source = "airsim"
            else:
                logger.warning(f"{LogColors.YELLOW}AirSim returned empty image for {camera_name}{LogColors.RESET}")
        except Exception as e:
            logger.warning(f"{LogColors.YELLOW}AirSim capture failed ({e}) — synthetic fallback{LogColors.RESET}")

    image_meta = {
        "image_ref": image_ref,
        "label": label,
        "camera_name": camera_name,
        "image_type": image_type,
        "position": position_data,
        "timestamp": time.time(),
        "source": source,
        "mission_id": mission_id,
        "width": width,
        "height": height,
        "png_bytes": png_bytes,
    }

    _image_store_put(image_ref, image_meta)

    result = {
        "status": "success",
        "image_ref": image_ref,
        "label": label,
        "camera_name": camera_name,
        "image_type": image_type,
        "position": position_data,
        "source": source,
        "width": width,
        "height": height,
        "png_size_bytes": len(png_bytes),
        "mission_id": mission_id,
    }
    if source == "synthetic":
        result["note"] = "Headless SITL — synthetic image ref. Set AIRSIM_HOST for real camera."
    log_tool_output(result)
    return result


@mcp.tool()
async def set_camera_pose(ctx: Context, camera_name: str = "front_center",
                          pitch_deg: float = 0.0, roll_deg: float = 0.0,
                          yaw_deg: float = 0.0) -> dict:
    """Set the camera gimbal orientation.

    Controls the camera angle for directed inspection. Maps to
    GIMBAL_MANAGER_SET_PITCHYAW on real hardware.

    Args:
        ctx: MCP context
        camera_name: Camera to control ("front_center" or "bottom_center")
        pitch_deg: Pitch angle in degrees (negative = down, -90 = nadir)
        roll_deg: Roll angle in degrees
        yaw_deg: Yaw angle in degrees

    Returns:
        dict with status and new pose
    """
    log_tool_call("set_camera_pose", camera_name=camera_name, pitch_deg=pitch_deg,
                  roll_deg=roll_deg, yaw_deg=yaw_deg)
    connector = ctx.request_context.lifespan_context

    if connector.airsim_client and AIRSIM_AVAILABLE:
        try:
            pose = airsim.Pose(
                airsim.Vector3r(0, 0, 0),
                airsim.to_quaternion(
                    math.radians(pitch_deg),
                    math.radians(roll_deg),
                    math.radians(yaw_deg),
                ),
            )
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: connector.airsim_client.simSetCameraPose(camera_name, pose),
            )
            result = {
                "status": "success",
                "camera_name": camera_name,
                "pitch_deg": pitch_deg,
                "roll_deg": roll_deg,
                "yaw_deg": yaw_deg,
            }
        except Exception as e:
            result = {"status": "failed", "error": str(e)}
    else:
        result = {
            "status": "success",
            "camera_name": camera_name,
            "pitch_deg": pitch_deg,
            "roll_deg": roll_deg,
            "yaw_deg": yaw_deg,
            "note": "No AirSim — pose stored but not applied.",
        }

    log_tool_output(result)
    return result


@mcp.tool()
async def capture_multi_camera(ctx: Context, label: str = "",
                                cameras: str = "front_center,bottom_center") -> dict:
    """Capture images from multiple cameras simultaneously.

    Efficient single-RPC multi-camera capture for survey waypoints.

    Args:
        ctx: MCP context
        label: Label prefix for images
        cameras: Comma-separated camera names (default: "front_center,bottom_center")

    Returns:
        dict with list of image refs from each camera
    """
    camera_list = [c.strip() for c in cameras.split(",") if c.strip()]
    log_tool_call("capture_multi_camera", label=label, cameras=camera_list)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    # Get position once for all images
    position_data = {}
    if connector.telemetry:
        snap = connector.telemetry.get_snapshot()
        if snap.get("position"):
            position_data = {
                "latitude_deg": snap["position"]["lat"],
                "longitude_deg": snap["position"]["lon"],
                "relative_altitude_m": snap["position"]["alt_relative_m"],
                "absolute_altitude_m": snap["position"]["alt_msl_m"],
            }

    mission_id = connector.current_mission.id if connector.current_mission else "no-mission"
    captures = []

    if connector.airsim_client and AIRSIM_AVAILABLE and CV2_AVAILABLE:
        try:
            requests = [airsim.ImageRequest(cam, 0, False, False) for cam in camera_list]
            loop = asyncio.get_event_loop()
            responses = await loop.run_in_executor(
                None, lambda: connector.airsim_client.simGetImages(requests)
            )
            for i, resp in enumerate(responses):
                cam = camera_list[i] if i < len(camera_list) else f"camera_{i}"
                image_ref = f"img-{mission_id}-{uuid.uuid4().hex[:6]}"
                png_bytes = b""
                width, height = 0, 0
                if resp.width > 0:
                    img_1d = np.frombuffer(resp.image_data_uint8, dtype=np.uint8)
                    img_bgr = img_1d.reshape(resp.height, resp.width, 3)
                    _, png_buf = cv2.imencode('.png', img_bgr)
                    png_bytes = png_buf.tobytes()
                    width, height = resp.width, resp.height

                meta = {
                    "image_ref": image_ref, "label": f"{label}-{cam}" if label else cam,
                    "camera_name": cam, "image_type": "scene", "position": position_data,
                    "timestamp": time.time(), "source": "airsim", "mission_id": mission_id,
                    "width": width, "height": height, "png_bytes": png_bytes,
                }
                _image_store_put(image_ref, meta)
                captures.append({"image_ref": image_ref, "camera": cam,
                                 "width": width, "height": height,
                                 "png_size_bytes": len(png_bytes)})
        except Exception as e:
            logger.warning(f"{LogColors.YELLOW}Multi-camera capture failed ({e}){LogColors.RESET}")

    # Fallback: synthetic refs for any cameras not yet captured
    for cam in camera_list:
        if not any(c["camera"] == cam for c in captures):
            image_ref = f"img-{mission_id}-{uuid.uuid4().hex[:6]}"
            meta = {
                "image_ref": image_ref, "label": f"{label}-{cam}" if label else cam,
                "camera_name": cam, "image_type": "scene", "position": position_data,
                "timestamp": time.time(), "source": "synthetic", "mission_id": mission_id,
                "width": 0, "height": 0, "png_bytes": b"",
            }
            _image_store_put(image_ref, meta)
            captures.append({"image_ref": image_ref, "camera": cam,
                             "width": 0, "height": 0, "source": "synthetic"})

    result = {"status": "success", "captures": captures, "position": position_data}
    log_tool_output(result)
    return result


@mcp.tool()
async def get_camera_info(ctx: Context) -> dict:
    """List available cameras and their configurations.

    Returns:
        dict with camera names, types, and resolution info
    """
    log_tool_call("get_camera_info")
    connector = ctx.request_context.lifespan_context

    cameras = {
        "front_center": {
            "resolution": "1920x1080",
            "image_types": ["scene", "depth"],
            "gimbal": True,
            "position": "front, slightly below center",
        },
        "bottom_center": {
            "resolution": "1280x720",
            "image_types": ["scene", "segmentation"],
            "gimbal": False,
            "position": "bottom, fixed nadir (-90°)",
        },
    }

    airsim_connected = connector.airsim_client is not None
    result = {
        "status": "success",
        "airsim_connected": airsim_connected,
        "cameras": cameras,
        "supported_image_types": {
            "scene": "RGB color image (ImageType 0)",
            "depth": "Depth map (ImageType 2)",
            "segmentation": "Segmentation mask (ImageType 5)",
        },
    }
    if not airsim_connected:
        result["note"] = "AirSim not connected — capture returns synthetic refs"

    log_tool_output(result)
    return result


@mcp.tool()
async def analyze_image(ctx: Context, image_ref: str, prompt: str = "",
                        auto_add_finding: bool = False, yolo_confidence: float = 0.3,
                        use_claude_vision: bool = True) -> dict:
    """Analyze a captured image for objects, damage, or anomalies.

    Three-tier pipeline:
    1. YOLO v11 detection (always, ~50ms, free) — bounding boxes + class + confidence
    2. Claude Vision API (on YOLO hit or explicit prompt) — reasoning + severity
    3. Auto-log findings to mission state

    Falls back to synthetic stub if no real image data available.

    Args:
        ctx: MCP context
        image_ref: Reference from capture_image (e.g. "img-mission-abc123-def456")
        prompt: What to look for (e.g. "Check for damaged solar panels")
        auto_add_finding: If True and analysis detects something, auto-log as finding
        yolo_confidence: Minimum YOLO confidence threshold (default 0.3)
        use_claude_vision: Whether to use Claude Vision for reasoning (default True)

    Returns:
        dict with analysis results, YOLO detections, and optional Claude Vision reasoning
    """
    log_tool_call("analyze_image", image_ref=image_ref, prompt=prompt,
                  auto_add_finding=auto_add_finding)
    connector = ctx.request_context.lifespan_context

    # Look up image metadata
    image_meta = _image_store.get(image_ref)
    if image_meta is None:
        result = {"status": "failed", "error": f"Image ref '{image_ref}' not found. Use capture_image first."}
        log_tool_output(result)
        return result

    source = image_meta.get("source", "synthetic")
    png_bytes = image_meta.get("png_bytes", b"")
    position = image_meta.get("position", {})

    # If no real image data, return stub
    if source == "synthetic" or not png_bytes:
        result = {
            "status": "success",
            "image_ref": image_ref,
            "source": source,
            "position": position,
            "analysis": "No real image data — synthetic stub.",
            "yolo_detections": [],
            "claude_vision": None,
            "prompt": prompt,
            "note": "Set AIRSIM_HOST for real vision analysis.",
        }
        log_tool_output(result)
        return result

    yolo_detections = []
    claude_analysis = None

    # ---- Tier 1: YOLO Detection ----
    if YOLO_AVAILABLE and CV2_AVAILABLE:
        try:
            loop = asyncio.get_event_loop()
            yolo_detections = await loop.run_in_executor(None, lambda: _run_yolo(png_bytes, yolo_confidence))
            logger.info(f"{LogColors.STATUS}YOLO: {len(yolo_detections)} detections above {yolo_confidence}{LogColors.RESET}")
        except Exception as e:
            logger.warning(f"{LogColors.YELLOW}YOLO failed: {e}{LogColors.RESET}")

    # ---- Tier 2: Claude Vision API ----
    has_detections = len(yolo_detections) > 0
    should_use_claude = use_claude_vision and ANTHROPIC_AVAILABLE and os.environ.get("ANTHROPIC_API_KEY")
    if should_use_claude and (has_detections or prompt):
        try:
            claude_analysis = await _claude_vision_analyze(
                png_bytes=png_bytes,
                prompt=prompt,
                detections=yolo_detections,
                position=position,
            )
            logger.info(f"{LogColors.STATUS}Claude Vision: analysis complete{LogColors.RESET}")
        except Exception as e:
            logger.warning(f"{LogColors.YELLOW}Claude Vision failed: {e}{LogColors.RESET}")

    # ---- Tier 3: Auto-add findings ----
    findings_added = []
    if auto_add_finding and connector.current_mission:
        # Prefer Claude analysis findings, fall back to YOLO
        if claude_analysis and claude_analysis.get("findings"):
            for cf in claude_analysis["findings"]:
                finding = Finding(
                    id=f"f-{uuid.uuid4().hex[:6]}",
                    type=cf.get("type", "detection"),
                    lat=position.get("latitude_deg", 0.0),
                    lon=position.get("longitude_deg", 0.0),
                    confidence=cf.get("confidence", 0.5),
                    metadata={
                        "description": cf.get("description", ""),
                        "severity": cf.get("severity", "unknown"),
                        "source": "claude_vision",
                    },
                    image_ref=image_ref,
                )
                connector.current_mission.findings.append(finding)
                findings_added.append(finding.id)
        elif yolo_detections:
            for det in yolo_detections:
                finding = Finding(
                    id=f"f-{uuid.uuid4().hex[:6]}",
                    type=det["class"],
                    lat=position.get("latitude_deg", 0.0),
                    lon=position.get("longitude_deg", 0.0),
                    confidence=det["confidence"],
                    metadata={"bbox": det["bbox"], "source": "yolo"},
                    image_ref=image_ref,
                )
                connector.current_mission.findings.append(finding)
                findings_added.append(finding.id)

    result = {
        "status": "success",
        "image_ref": image_ref,
        "source": source,
        "position": position,
        "yolo_detections": yolo_detections,
        "yolo_count": len(yolo_detections),
        "claude_vision": claude_analysis,
        "prompt": prompt,
        "findings_added": findings_added,
    }
    log_tool_output(result)
    return result


@mcp.tool()
async def get_findings_near(ctx: Context, lat: float, lon: float, radius_m: float = 100.0) -> dict:
    """Query mission findings within a radius of a GPS point.

    Uses haversine distance for accurate filtering. Useful for checking if
    an area has already been flagged before committing to a closer inspection.

    Args:
        ctx: MCP context
        lat: Center latitude in degrees
        lon: Center longitude in degrees
        radius_m: Search radius in meters (default 100)

    Returns:
        dict with matching findings sorted by distance
    """
    log_tool_call("get_findings_near", lat=lat, lon=lon, radius_m=radius_m)
    connector = ctx.request_context.lifespan_context

    if connector.current_mission is None:
        result = {"status": "failed", "error": "No active mission."}
        log_tool_output(result)
        return result

    nearby = []
    for f in connector.current_mission.findings:
        dist = haversine_distance(lat, lon, f.lat, f.lon)
        if dist <= radius_m:
            nearby.append({
                "id": f.id,
                "type": f.type,
                "lat": f.lat,
                "lon": f.lon,
                "confidence": f.confidence,
                "distance_m": round(dist, 1),
                "metadata": f.metadata,
                "image_ref": f.image_ref,
            })

    nearby.sort(key=lambda x: x["distance_m"])

    result = {
        "status": "success",
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "count": len(nearby),
        "findings": nearby,
    }
    log_tool_output(result)
    return result


@mcp.tool()
async def capture_and_analyze(ctx: Context, label: str = "", camera_name: str = "front_center",
                               prompt: str = "", auto_add_finding: bool = True,
                               yolo_confidence: float = 0.3,
                               use_claude_vision: bool = True) -> dict:
    """Capture an image and immediately analyze it — one-call vision pipeline.

    Combines capture_image + analyze_image for efficient waypoint-based surveying.
    Default auto_add_finding=True so detections are logged to mission state automatically.

    Args:
        ctx: MCP context
        label: Label for the capture (e.g. "sector-3-wp-2")
        camera_name: Camera to use ("front_center" or "bottom_center")
        prompt: What to look for (e.g. "Check for damaged solar panels")
        auto_add_finding: Auto-log detections to mission findings (default True)
        yolo_confidence: Minimum YOLO confidence threshold (default 0.3)
        use_claude_vision: Use Claude Vision API for reasoning (default True)

    Returns:
        dict with capture info + analysis results
    """
    log_tool_call("capture_and_analyze", label=label, camera_name=camera_name, prompt=prompt)

    # Step 1: Capture
    capture_result = await capture_image(ctx, label=label, camera_name=camera_name)
    if capture_result.get("status") != "success":
        return capture_result

    image_ref = capture_result["image_ref"]

    # Step 2: Analyze
    analysis_result = await analyze_image(
        ctx, image_ref=image_ref, prompt=prompt,
        auto_add_finding=auto_add_finding,
        yolo_confidence=yolo_confidence,
        use_claude_vision=use_claude_vision,
    )

    # Merge results
    result = {
        "status": "success",
        "capture": {
            "image_ref": image_ref,
            "source": capture_result.get("source"),
            "camera_name": camera_name,
            "width": capture_result.get("width"),
            "height": capture_result.get("height"),
            "png_size_bytes": capture_result.get("png_size_bytes"),
        },
        "position": capture_result.get("position", {}),
        "analysis": {
            "yolo_detections": analysis_result.get("yolo_detections", []),
            "yolo_count": analysis_result.get("yolo_count", 0),
            "claude_vision": analysis_result.get("claude_vision"),
            "findings_added": analysis_result.get("findings_added", []),
        },
        "prompt": prompt,
    }
    log_tool_output(result)
    return result


# ============================================================
# Phase 2D: Extended Navigation & Safety Tools
# ============================================================

@mcp.tool()
async def fly_waypoint_route(
    ctx: Context, waypoints: list[dict], altitude: float, speed: float = 5.0
) -> dict:
    """Execute a custom multi-waypoint route.

    Uploads waypoints as a PX4 mission, sets cruise speed, and starts.
    The drone must be armed and airborne.

    Args:
        ctx: MCP context
        waypoints: List of waypoints, each with "lat" and "lon" keys (degrees).
                   Example: [{"lat": 47.397, "lon": 8.546}, {"lat": 47.398, "lon": 8.547}]
        altitude: Flight altitude in meters (relative to home), applied to all waypoints
        speed: Cruise speed in m/s (default 5.0)

    Returns:
        dict with waypoint count and estimated total distance
    """
    log_tool_call("fly_waypoint_route", waypoint_count=len(waypoints), altitude=altitude, speed=speed)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    if len(waypoints) < 2:
        return {"status": "failed", "error": "Need at least 2 waypoints."}
    if len(waypoints) > 200:
        return {"status": "failed", "error": f"Too many waypoints ({len(waypoints)}). Max 200."}

    for i, wp in enumerate(waypoints):
        if "lat" not in wp or "lon" not in wp:
            return {"status": "failed", "error": f"Waypoint {i} missing 'lat' or 'lon' key."}

    drone = connector.drone
    try:
        mission_items = []

        # Speed change item (MAV_CMD_DO_CHANGE_SPEED)
        mission_items.append(MissionItem(
            seq=0,
            frame=2,  # MAV_FRAME_MISSION
            command=178,  # MAV_CMD_DO_CHANGE_SPEED
            current=1,
            autocontinue=1,
            param1=1,  # speed type: ground speed
            param2=float(speed),
            param3=-1,  # no throttle change
            param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        # Navigation waypoints
        for i, wp in enumerate(waypoints):
            mission_items.append(MissionItem(
                seq=len(mission_items),
                frame=3,
                command=16,  # MAV_CMD_NAV_WAYPOINT
                current=0,
                autocontinue=1,
                param1=0,
                param2=2.0,  # acceptance radius
                param3=0,
                param4=float('nan'),
                x=int(wp["lat"] * 1e7),
                y=int(wp["lon"] * 1e7),
                z=float(altitude),
                mission_type=0,
            ))

        # RTL waypoint
        mission_items.append(MissionItem(
            seq=len(mission_items),
            frame=2,  # MAV_FRAME_MISSION
            command=20,  # MAV_CMD_NAV_RETURN_TO_LAUNCH
            current=0,
            autocontinue=1,
            param1=0, param2=0, param3=0, param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        # Calculate total distance
        total_dist = 0.0
        for i in range(1, len(waypoints)):
            total_dist += haversine_distance(
                waypoints[i - 1]["lat"], waypoints[i - 1]["lon"],
                waypoints[i]["lat"], waypoints[i]["lon"]
            )

        log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(mission_items))
        await drone.mission_raw.upload_mission(mission_items)

        await asyncio.sleep(0.5)

        log_mavlink_cmd("drone.mission.start_mission")
        await drone.mission.start_mission()

        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="waypoint_route",
            status="active",
            started_at=time.time(),
            command_tool="fly_waypoint_route",
            description=f"Waypoint route: {len(waypoints)} waypoints at {altitude}m",
            waypoint_count=len(waypoints),
            total_distance_m=total_dist,
            estimated_time_s=total_dist / speed if speed > 0 else 0,
            speed_m_s=speed,
            altitude_m=altitude,
        )

        result = {
            "status": "success",
            "message": f"Route started: {len(waypoints)} waypoints at {altitude}m, speed {speed} m/s",
            "waypoint_count": len(waypoints),
            "total_distance_m": round(total_dist, 1),
            "estimated_time_s": round(total_dist / speed, 1) if speed > 0 else None,
            "altitude_m": altitude,
            "speed_m_s": speed,
            "note": "Use get_drone_activity() to track progress",
        }
        log_tool_output(result)
        return result

    except Exception as e:
        logger.error(f"{LogColors.ERROR}fly_waypoint_route failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Route failed: {str(e)}"}


@mcp.tool()
async def orbit_point(
    ctx: Context, lat: float, lon: float, radius: float,
    altitude: float, laps: int = 1, speed: float = 5.0
) -> dict:
    """Orbit (circle) around a GPS point.

    Generates a circular waypoint mission with 12 points per lap.
    More reliable than MAV_CMD_DO_ORBIT in PX4 SITL.

    Args:
        ctx: MCP context
        lat: Center latitude in degrees
        lon: Center longitude in degrees
        radius: Orbit radius in meters
        altitude: Flight altitude in meters (relative)
        laps: Number of laps (default 1)
        speed: Cruise speed in m/s (default 5.0)

    Returns:
        dict with orbit parameters and waypoint count
    """
    log_tool_call("orbit_point", lat=lat, lon=lon, radius=radius, altitude=altitude, laps=laps, speed=speed)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    if radius < 5:
        return {"status": "failed", "error": "Radius must be at least 5 meters."}
    if laps < 1 or laps > 20:
        return {"status": "failed", "error": "Laps must be between 1 and 20."}

    drone = connector.drone
    try:
        points_per_lap = 12
        mission_items = []

        # Speed change item
        mission_items.append(MissionItem(
            seq=0,
            frame=2,  # MAV_FRAME_MISSION
            command=178,  # MAV_CMD_DO_CHANGE_SPEED
            current=1,
            autocontinue=1,
            param1=1,
            param2=float(speed),
            param3=-1,
            param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        # Generate circular waypoints
        for lap in range(laps):
            for j in range(points_per_lap):
                angle_deg = (360.0 / points_per_lap) * j
                angle_rad = math.radians(angle_deg)
                north_m = radius * math.cos(angle_rad)
                east_m = radius * math.sin(angle_rad)
                wp_lat, wp_lon = offset_gps(lat, lon, north_m, east_m)

                mission_items.append(MissionItem(
                    seq=len(mission_items),
                    frame=3,
                    command=16,
                    current=0,
                    autocontinue=1,
                    param1=0,
                    param2=2.0,
                    param3=0,
                    param4=float('nan'),
                    x=int(wp_lat * 1e7),
                    y=int(wp_lon * 1e7),
                    z=float(altitude),
                    mission_type=0,
                ))

        # RTL waypoint
        mission_items.append(MissionItem(
            seq=len(mission_items),
            frame=2,  # MAV_FRAME_MISSION
            command=20,  # MAV_CMD_NAV_RETURN_TO_LAUNCH
            current=0,
            autocontinue=1,
            param1=0, param2=0, param3=0, param4=0,
            x=0, y=0, z=0,
            mission_type=0,
        ))

        log_mavlink_cmd("drone.mission_raw.upload_mission", waypoint_count=len(mission_items))
        await drone.mission_raw.upload_mission(mission_items)

        await asyncio.sleep(0.5)

        log_mavlink_cmd("drone.mission.start_mission")
        await drone.mission.start_mission()

        circumference = 2 * math.pi * radius
        total_dist = circumference * laps

        connector.current_activity = FlightActivity(
            id=f"flight-{uuid.uuid4().hex[:8]}",
            type="orbit",
            status="active",
            started_at=time.time(),
            command_tool="orbit_point",
            description=f"Orbiting ({lat:.5f}, {lon:.5f}) r={radius}m, {laps} lap(s)",
            waypoint_count=len(mission_items) - 2,
            total_distance_m=total_dist,
            estimated_time_s=total_dist / speed if speed > 0 else 0,
            speed_m_s=speed,
            altitude_m=altitude,
        )

        result = {
            "status": "success",
            "message": f"Orbiting ({lat:.5f}, {lon:.5f}) at {radius}m radius, {laps} lap(s)",
            "center": {"lat": lat, "lon": lon},
            "radius_m": radius,
            "laps": laps,
            "altitude_m": altitude,
            "speed_m_s": speed,
            "waypoint_count": len(mission_items) - 2,
            "total_distance_m": round(total_dist, 1),
            "estimated_time_s": round(total_dist / speed, 1) if speed > 0 else None,
            "note": "Use get_drone_activity() to track progress",
        }
        log_tool_output(result)
        return result

    except Exception as e:
        logger.error(f"{LogColors.ERROR}orbit_point failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Orbit failed: {str(e)}"}


@mcp.tool()
async def set_geofence(ctx: Context, bounds: dict) -> dict:
    """Set an inclusion geofence boundary.

    Creates a rectangular geofence from bounds. The drone will be confined
    to this area. PX4 will trigger RTL/LAND if the drone exits the fence.

    Args:
        ctx: MCP context
        bounds: Area bounds as {north, south, east, west} in lat/lon degrees

    Returns:
        dict with geofence parameters
    """
    log_tool_call("set_geofence", bounds=bounds)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    required_keys = ["north", "south", "east", "west"]
    missing = [k for k in required_keys if k not in bounds]
    if missing:
        return {"status": "failed", "error": f"Bounds missing keys: {missing}"}

    drone = connector.drone
    try:
        # Create 4-corner inclusion polygon
        corners = [
            GeoPoint(bounds["north"], bounds["west"]),  # NW
            GeoPoint(bounds["north"], bounds["east"]),  # NE
            GeoPoint(bounds["south"], bounds["east"]),  # SE
            GeoPoint(bounds["south"], bounds["west"]),  # SW
        ]
        polygon = GeoPolygon(corners, FenceType.INCLUSION)
        geofence_data = GeofenceData([polygon], [])

        log_mavlink_cmd("drone.geofence.upload_geofence", corners=4)
        await drone.geofence.upload_geofence(geofence_data)

        result = {
            "status": "success",
            "message": "Geofence set — drone confined to bounds",
            "bounds": bounds,
            "type": "inclusion",
            "note": "PX4 will RTL/LAND if drone exits the fence. Use clear_geofence() to remove.",
        }
        log_tool_output(result)
        return result

    except Exception as e:
        logger.error(f"{LogColors.ERROR}set_geofence failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Geofence failed: {str(e)}"}


@mcp.tool()
async def clear_geofence(ctx: Context) -> dict:
    """Clear all geofence boundaries.

    Removes any previously uploaded geofence, allowing the drone to fly freely.

    Args:
        ctx: MCP context

    Returns:
        dict confirming geofence cleared
    """
    log_tool_call("clear_geofence")
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    drone = connector.drone
    try:
        log_mavlink_cmd("drone.geofence.clear_geofence")
        await drone.geofence.clear_geofence()

        result = {"status": "success", "message": "Geofence cleared."}
        log_tool_output(result)
        return result

    except Exception as e:
        logger.error(f"{LogColors.ERROR}clear_geofence failed: {e}{LogColors.RESET}")
        return {"status": "failed", "error": f"Clear geofence failed: {str(e)}"}


@mcp.tool()
async def return_to_launch_if_low_battery(ctx: Context, threshold: float = 20.0) -> dict:
    """Start a background battery monitor that triggers RTL if battery drops below threshold.

    Checks battery every 5 seconds. If remaining percent falls below the threshold,
    commands RTL and aborts any active mission. Only one monitor can be active at a time.

    Args:
        ctx: MCP context
        threshold: Battery percentage below which to trigger RTL (default 20.0)

    Returns:
        dict confirming monitor started
    """
    log_tool_call("return_to_launch_if_low_battery", threshold=threshold)
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    if threshold < 5 or threshold > 80:
        return {"status": "failed", "error": "Threshold must be between 5 and 80 percent."}

    drone = connector.drone

    # Cancel any existing monitor
    if connector._battery_monitor_task and not connector._battery_monitor_task.done():
        connector._battery_monitor_task.cancel()
        try:
            await connector._battery_monitor_task
        except asyncio.CancelledError:
            pass

    async def _battery_monitor(threshold_pct: float):
        """Background task: check battery from cache, RTL if low."""
        logger.info(f"{LogColors.STATUS}Battery monitor started — RTL below {threshold_pct}%{LogColors.RESET}")
        try:
            while True:
                await asyncio.sleep(5.0)
                try:
                    if connector.telemetry:
                        bat = connector.telemetry.get("battery")
                        pct = bat.remaining_percent * 100 if bat else None
                    else:
                        async def _read_battery():
                            async for bat in drone.telemetry.battery():
                                return bat
                            return None
                        bat_obj = await asyncio.wait_for(_read_battery(), timeout=5.0)
                        pct = bat_obj.remaining_percent * 100 if bat_obj else None

                    if pct is not None and pct < threshold_pct:
                        logger.warning(
                            f"{LogColors.ERROR}Battery {pct:.1f}% below threshold "
                            f"{threshold_pct}% — commanding RTL!{LogColors.RESET}"
                        )
                        # Abort mission if active
                        if connector.current_mission and connector.current_mission.status == MissionStatus.ACTIVE:
                            connector.current_mission.status = MissionStatus.ABORTED
                        await drone.action.return_to_launch()
                        logger.info(f"{LogColors.SUCCESS}RTL commanded due to low battery{LogColors.RESET}")
                        return  # Stop monitoring after triggering
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"{LogColors.YELLOW}Battery monitor read error: {e}{LogColors.RESET}")
        except asyncio.CancelledError:
            logger.info(f"{LogColors.STATUS}Battery monitor cancelled{LogColors.RESET}")

    connector._battery_monitor_task = asyncio.create_task(_battery_monitor(threshold))

    result = {
        "status": "success",
        "message": f"Battery monitor active — RTL if below {threshold}%",
        "threshold_pct": threshold,
        "check_interval_s": 5,
    }
    log_tool_output(result)
    return result


@mcp.tool()
async def get_drone_activity(ctx: Context) -> dict:
    """Get unified snapshot of current drone activity, telemetry, and mission state.

    Works for ALL flight types: goto, waypoint route, orbit, search patterns,
    RTL, landing, and idle. Single tool replaces the need to call different
    monitors for different flight modes.

    Returns activity status, telemetry (position, battery, speed, flight mode),
    navigation progress, and mission state (if a search is active).

    Activity status lifecycle: active → returning (RTL/LAND) → completed (on ground).
    When activity_returning is true, the drone is landing autonomously — do NOT call land().
    Only activity_complete=true means the flight is fully finished.

    Call this repeatedly (every 1-2s) to monitor any flight in progress.
    Uses cached telemetry streams — response time is <100ms.
    """
    log_tool_call("get_drone_activity")
    connector = ctx.request_context.lifespan_context

    if not await ensure_connection(connector):
        return {"status": "failed", "error": "Drone connection timeout."}

    drone = connector.drone

    # --- Telemetry from cache (instant) or fallback to direct reads ---
    if connector.telemetry:
        snapshot = connector.telemetry.get_snapshot()
        telemetry = {
            "position": snapshot["position"],
            "flight_mode": snapshot["flight_mode"],
            "battery_pct": snapshot["battery_pct"],
            "speed_m_s": snapshot["speed_m_s"],
            "landed_state": snapshot["landed_state"],
            "is_on_ground": snapshot["is_on_ground"],
            "_cache_ages_s": snapshot["_cache_ages_s"],
        }
        flight_mode = snapshot["flight_mode"]
        landed_state = snapshot["landed_state"]
        is_on_ground = snapshot["is_on_ground"]
    else:
        # Fallback: direct MAVSDK reads (only if TelemetryService not available)
        async def _read_one(aiter, timeout=5.0):
            async for item in aiter:
                return item
            return None

        telemetry = {}
        try:
            pos = await asyncio.wait_for(_read_one(drone.telemetry.position()), timeout=5.0)
            if pos:
                telemetry["position"] = {
                    "lat": round(pos.latitude_deg, 7),
                    "lon": round(pos.longitude_deg, 7),
                    "alt_relative_m": round(pos.relative_altitude_m, 1),
                    "alt_msl_m": round(pos.absolute_altitude_m, 1),
                }
        except asyncio.TimeoutError:
            telemetry["position"] = None

        try:
            fm = await asyncio.wait_for(_read_one(drone.telemetry.flight_mode()), timeout=5.0)
            flight_mode = str(fm).split(".")[-1] if fm else "UNKNOWN"
            telemetry["flight_mode"] = flight_mode
        except asyncio.TimeoutError:
            flight_mode = "UNKNOWN"
            telemetry["flight_mode"] = flight_mode

        try:
            bat = await asyncio.wait_for(_read_one(drone.telemetry.battery()), timeout=5.0)
            telemetry["battery_pct"] = round(bat.remaining_percent, 1) if bat else -1
        except asyncio.TimeoutError:
            telemetry["battery_pct"] = -1

        try:
            vel = await asyncio.wait_for(_read_one(drone.telemetry.velocity_ned()), timeout=5.0)
            if vel:
                speed = math.sqrt(vel.north_m_s**2 + vel.east_m_s**2)
                telemetry["speed_m_s"] = round(speed, 1)
            else:
                telemetry["speed_m_s"] = None
        except asyncio.TimeoutError:
            telemetry["speed_m_s"] = None

        landed_state = "UNKNOWN"
        is_on_ground = False
        try:
            ls = await asyncio.wait_for(_read_one(drone.telemetry.landed_state()), timeout=5.0)
            landed_state = str(ls).split(".")[-1] if ls else "UNKNOWN"
            is_on_ground = landed_state == "ON_GROUND"
        except asyncio.TimeoutError:
            pass
        telemetry["landed_state"] = landed_state
        telemetry["is_on_ground"] = is_on_ground

    activity = connector.current_activity

    # --- Activity completion detection ---
    # Status lifecycle: active → returning (RTL/LAND in progress) → completed (on ground)
    #                   active → completed (HOLD — pattern done, hovering, Claude decides next)
    if activity and activity.type in ("waypoint_route", "orbit", "search"):
        if (activity.status == "active"
                and flight_mode in ("RETURN_TO_LAUNCH", "LAND")):
            # Drone is actively returning — don't mark completed yet
            activity.status = "returning"
            # Clear mission_raw to fix re-arm bug
            try:
                await drone.mission_raw.clear_mission()
            except Exception:
                pass
        elif (activity.status == "active"
                and flight_mode == "HOLD"):
            # Pattern done, drone hovering at last waypoint — Claude can decide what to do
            activity.status = "completed"
            activity.completed_at = time.time()
            try:
                await drone.mission_raw.clear_mission()
            except Exception:
                pass
        elif activity.status == "returning" and is_on_ground:
            # RTL/landing finished — now truly complete
            activity.status = "completed"
            activity.completed_at = time.time()

    # --- Build response ---
    response = {"status": "success", "telemetry": telemetry}

    if not activity:
        response["activity"] = None
        response["activity_complete"] = True
        display = f"IDLE | Alt: {telemetry.get('position', {}).get('alt_relative_m', '?')}m | Batt: {telemetry.get('battery_pct', '?')}%"
        response["DISPLAY_TO_USER"] = display
        log_tool_output(response)
        return response

    elapsed = round(time.time() - activity.started_at, 1)

    display = f"{activity.type.upper()}: {activity.description} | {elapsed:.0f}s elapsed"

    response["activity"] = {
        "id": activity.id,
        "type": activity.type,
        "status": activity.status,
        "description": activity.description,
        "command_tool": activity.command_tool,
        "started_at": activity.started_at,
        "elapsed_s": elapsed,
        "waypoint_count": activity.waypoint_count,
        "total_distance_m": round(activity.total_distance_m, 1),
    }
    response["activity_complete"] = activity.status == "completed"
    response["activity_returning"] = activity.status == "returning"

    # --- Navigation (goto only) ---
    if activity.type == "goto" and activity.destination and telemetry.get("position"):
        dest = activity.destination
        pos = telemetry["position"]
        distance = haversine_distance(pos["lat"], pos["lon"], dest["latitude"], dest["longitude"])
        initial = dest.get("initial_distance", 1)
        progress = max(0, min(100, ((initial - distance) / initial) * 100)) if initial > 0 else 100
        speed_val = telemetry.get("speed_m_s") or 0
        eta = distance / speed_val if speed_val > 0.5 else None

        response["navigation"] = {
            "destination": {"lat": dest["latitude"], "lon": dest["longitude"]},
            "distance_remaining_m": round(distance, 1),
            "progress_pct": round(progress, 1),
            "eta_s": round(eta, 0) if eta else None,
        }
        display = f"GOTO: {progress:.0f}% | {distance:.0f}m remain | Alt: {pos.get('alt_relative_m', '?')}m | Batt: {telemetry.get('battery_pct', '?')}%"
    else:
        response["navigation"] = None

    # --- PX4 mission progress (waypoint/orbit/search) ---
    if activity.type in ("waypoint_route", "orbit", "search"):
        current_wp = 0
        total_wp = 0
        if connector.telemetry:
            mp = connector.telemetry.get_snapshot()["mission_progress"]
            current_wp = mp["current"]
            total_wp = mp["total"]
        else:
            try:
                async def _read_mp(aiter, timeout=5.0):
                    async for item in aiter:
                        return item
                    return None
                progress = await asyncio.wait_for(_read_mp(drone.mission.mission_progress()), timeout=5.0)
                if progress:
                    current_wp = progress.current
                    total_wp = progress.total
            except asyncio.TimeoutError:
                pass

        wp_progress = round((current_wp / total_wp) * 100, 1) if total_wp > 0 else 0
        # Only include px4_mission when PX4 actually reports progress (0/0 = mission_raw API mismatch)
        if total_wp > 0:
            response["px4_mission"] = {
                "current_waypoint": current_wp,
                "total_waypoints": total_wp,
                "progress_pct": wp_progress,
            }
        else:
            response["px4_mission"] = None

        if not response.get("navigation"):
            pos = telemetry.get("position", {})
            # Use elapsed time / estimated time for progress when PX4 waypoint index unavailable
            if activity.estimated_time_s > 0:
                time_progress = min(100, round((elapsed / activity.estimated_time_s) * 100, 1))
                display = f"{activity.type.upper()}: ~{time_progress:.0f}% (est) | {elapsed:.0f}s / {activity.estimated_time_s:.0f}s | Alt: {pos.get('alt_relative_m', '?')}m | Batt: {telemetry.get('battery_pct', '?')}%"
            else:
                display = f"{activity.type.upper()}: {elapsed:.0f}s elapsed | Alt: {pos.get('alt_relative_m', '?')}m | Batt: {telemetry.get('battery_pct', '?')}%"
    else:
        response["px4_mission"] = None

    # --- Mission state (search only) ---
    if connector.current_mission and activity.mission_id:
        mission = connector.current_mission
        sectors_completed = sum(1 for s in mission.sectors if s.status == SectorStatus.COMPLETED)
        sectors_total = len(mission.sectors)
        coverage = round((sectors_completed / sectors_total) * 100, 1) if sectors_total > 0 else 0
        active_sectors = [s for s in mission.sectors if s.status == SectorStatus.ACTIVE]

        response["mission"] = {
            "id": mission.id,
            "type": mission.type,
            "objective": mission.objective,
            "sectors_completed": sectors_completed,
            "sectors_total": sectors_total,
            "coverage_pct": coverage,
            "current_sector": active_sectors[0].id if active_sectors else None,
            "findings_count": len(mission.findings),
        }
        display = f"SEARCH: {sectors_completed}/{sectors_total} sectors ({coverage:.0f}%) | Alt: {telemetry.get('position', {}).get('alt_relative_m', '?')}m | Batt: {telemetry.get('battery_pct', '?')}%"
    else:
        response["mission"] = None

    # --- RTL / Landing display ---
    if activity.type == "rtl" and not is_on_ground:
        display = f"RTL: Returning to launch | Alt: {telemetry.get('position', {}).get('alt_relative_m', '?')}m | Batt: {telemetry.get('battery_pct', '?')}%"
    elif activity.type == "land" or activity.landing_initiated:
        if is_on_ground:
            display = f"LANDED | Mission complete"
        else:
            display = f"LANDING | Alt: {telemetry.get('position', {}).get('alt_relative_m', '?')}m"

    if activity.status == "completed" and is_on_ground:
        display = f"LANDED: {activity.description} | {elapsed:.0f}s elapsed"
        response["mission_complete"] = True
    elif activity.status == "returning":
        display = f"RETURNING: {activity.description} | Auto-landing in progress — do not intervene"
        response["mission_complete"] = False
    elif activity.status == "completed":
        display = f"COMPLETED: {activity.description} | {elapsed:.0f}s elapsed"
        response["mission_complete"] = False
    else:
        response["mission_complete"] = False

    response["DISPLAY_TO_USER"] = display

    log_tool_output(response)
    return response


if __name__ == "__main__":
    # Run the server
    mcp.run(transport='stdio')
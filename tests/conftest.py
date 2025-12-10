"""
Pytest fixtures for MAVLink MCP testing.

Provides mock MAVSDK drone, telemetry data, and MCP context fixtures.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ============================================================================
# Mock Data Classes (mimic MAVSDK telemetry data)
# ============================================================================

@dataclass
class MockPosition:
    """Mock position telemetry"""
    latitude_deg: float = 33.6461
    longitude_deg: float = -117.8427
    absolute_altitude_m: float = 120.0
    relative_altitude_m: float = 10.0


@dataclass
class MockBattery:
    """Mock battery telemetry"""
    voltage_v: float = 16.2
    remaining_percent: float = 0.85


@dataclass
class MockHealth:
    """Mock health telemetry"""
    is_gyrometer_calibration_ok: bool = True
    is_accelerometer_calibration_ok: bool = True
    is_magnetometer_calibration_ok: bool = True
    is_local_position_ok: bool = True
    is_global_position_ok: bool = True
    is_home_position_ok: bool = True
    is_armable: bool = True


@dataclass
class MockGpsInfo:
    """Mock GPS info"""
    num_satellites: int = 12
    fix_type: str = "FIX_3D"


@dataclass
class MockVelocityNed:
    """Mock velocity NED"""
    north_m_s: float = 2.5
    east_m_s: float = 1.0
    down_m_s: float = -0.5


@dataclass
class MockAttitudeEuler:
    """Mock attitude euler angles"""
    roll_deg: float = 1.5
    pitch_deg: float = -2.0
    yaw_deg: float = 45.0


@dataclass
class MockStatusText:
    """Mock status text"""
    type: str = "INFO"
    text: str = "Test status message"


@dataclass
class MockImu:
    """Mock IMU data"""
    timestamp_us: int = 1234567890
    acceleration_frd: MagicMock = field(default_factory=lambda: MagicMock(
        forward_m_s2=0.1, right_m_s2=0.05, down_m_s2=9.81
    ))
    angular_velocity_frd: MagicMock = field(default_factory=lambda: MagicMock(
        forward_rad_s=0.01, right_rad_s=0.02, down_rad_s=0.0
    ))
    magnetic_field_frd: MagicMock = field(default_factory=lambda: MagicMock(
        forward_gauss=0.2, right_gauss=0.1, down_gauss=0.4
    ))
    temperature_degc: float = 25.5


@dataclass
class MockHome:
    """Mock home position"""
    latitude_deg: float = 33.6460
    longitude_deg: float = -117.8426
    absolute_altitude_m: float = 110.0


@dataclass
class MockMissionProgress:
    """Mock mission progress"""
    current: int = 2
    total: int = 5


@dataclass
class MockFlightMode:
    """Mock flight mode"""
    mode: str = "GUIDED"
    
    def __str__(self):
        return self.mode


@dataclass
class MockIntParam:
    """Mock integer parameter"""
    name: str
    value: int


@dataclass
class MockFloatParam:
    """Mock float parameter"""
    name: str
    value: float


@dataclass
class MockAllParams:
    """Mock all parameters response"""
    int_params: list = field(default_factory=lambda: [
        MockIntParam(name="BATT_CAPACITY", value=5200),
        MockIntParam(name="RC1_MIN", value=1000),
    ])
    float_params: list = field(default_factory=lambda: [
        MockFloatParam(name="RTL_ALT", value=1500.0),
        MockFloatParam(name="WPNAV_SPEED", value=500.0),
    ])


# ============================================================================
# Async Iterator Helper
# ============================================================================

class AsyncIteratorMock:
    """Helper to create async iterators for telemetry streams"""
    def __init__(self, items):
        self.items = items
        self.index = 0
    
    def __aiter__(self):
        return self
    
    async def __anext__(self):
        if self.index < len(self.items):
            item = self.items[self.index]
            self.index += 1
            return item
        raise StopAsyncIteration


def make_async_iter(item):
    """Create an async iterator that yields a single item"""
    return AsyncIteratorMock([item])


# ============================================================================
# Mock MAVLinkConnector
# ============================================================================

@dataclass
class MockMAVLinkConnector:
    """Mock MAVLinkConnector for testing"""
    drone: MagicMock
    connection_ready: asyncio.Event = field(default_factory=asyncio.Event)
    
    def __post_init__(self):
        # Set connection as ready by default
        self.connection_ready.set()


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_drone():
    """Create a fully mocked MAVSDK drone instance"""
    drone = MagicMock()
    
    # Mock action methods
    drone.action.arm = AsyncMock()
    drone.action.disarm = AsyncMock()
    drone.action.takeoff = AsyncMock()
    drone.action.land = AsyncMock()
    drone.action.return_to_launch = AsyncMock()
    drone.action.kill = AsyncMock()
    drone.action.hold = AsyncMock()
    drone.action.goto_location = AsyncMock()
    drone.action.set_takeoff_altitude = AsyncMock()
    drone.action.set_maximum_speed = AsyncMock()
    
    # Mock telemetry methods - return async iterators
    drone.telemetry.position = MagicMock(return_value=make_async_iter(MockPosition()))
    drone.telemetry.battery = MagicMock(return_value=make_async_iter(MockBattery()))
    drone.telemetry.health = MagicMock(return_value=make_async_iter(MockHealth()))
    drone.telemetry.gps_info = MagicMock(return_value=make_async_iter(MockGpsInfo()))
    drone.telemetry.velocity_ned = MagicMock(return_value=make_async_iter(MockVelocityNed()))
    drone.telemetry.attitude_euler = MagicMock(return_value=make_async_iter(MockAttitudeEuler()))
    drone.telemetry.status_text = MagicMock(return_value=make_async_iter(MockStatusText()))
    drone.telemetry.imu = MagicMock(return_value=make_async_iter(MockImu()))
    drone.telemetry.home = MagicMock(return_value=make_async_iter(MockHome()))
    drone.telemetry.flight_mode = MagicMock(return_value=make_async_iter(MockFlightMode()))
    drone.telemetry.in_air = MagicMock(return_value=make_async_iter(True))
    drone.telemetry.armed = MagicMock(return_value=make_async_iter(True))
    drone.telemetry.set_rate_imu = AsyncMock()
    
    # Mock mission methods
    drone.mission.start_mission = AsyncMock()
    drone.mission.pause_mission = AsyncMock()
    drone.mission.clear_mission = AsyncMock()
    drone.mission.set_return_to_launch_after_mission = AsyncMock()
    drone.mission.mission_progress = MagicMock(return_value=make_async_iter(MockMissionProgress()))
    drone.mission.set_current_mission_item = AsyncMock()
    drone.mission.is_mission_finished = AsyncMock(return_value=False)
    
    # Mock mission_raw methods
    drone.mission_raw.upload_mission = AsyncMock()
    drone.mission_raw.download_mission = AsyncMock(return_value=[])
    
    # Mock param methods
    drone.param.get_param_int = AsyncMock(return_value=5200)
    drone.param.get_param_float = AsyncMock(return_value=1500.0)
    drone.param.set_param_int = AsyncMock()
    drone.param.set_param_float = AsyncMock()
    drone.param.get_all_params = AsyncMock(return_value=MockAllParams())
    
    return drone


@pytest.fixture
def mock_connector(mock_drone):
    """Create a mock MAVLinkConnector with the mocked drone"""
    return MockMAVLinkConnector(drone=mock_drone)


@pytest.fixture
def mock_context(mock_connector):
    """Create a mock MCP context with the connector"""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = mock_connector
    return ctx


@pytest.fixture
def disconnected_connector(mock_drone):
    """Create a connector that simulates disconnection (timeout)"""
    connector = MockMAVLinkConnector(drone=mock_drone)
    connector.connection_ready = asyncio.Event()  # Not set = not ready
    return connector


@pytest.fixture
def disconnected_context(disconnected_connector):
    """Create a context with a disconnected connector"""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = disconnected_connector
    return ctx


# ============================================================================
# Specialized Mock Fixtures
# ============================================================================

@pytest.fixture
def low_battery_drone(mock_drone):
    """Drone with low battery"""
    mock_drone.telemetry.battery = MagicMock(
        return_value=make_async_iter(MockBattery(voltage_v=14.2, remaining_percent=0.15))
    )
    return mock_drone


@pytest.fixture
def poor_gps_drone(mock_drone):
    """Drone with poor GPS"""
    mock_drone.telemetry.gps_info = MagicMock(
        return_value=make_async_iter(MockGpsInfo(num_satellites=3, fix_type="FIX_2D"))
    )
    return mock_drone


@pytest.fixture
def unhealthy_drone(mock_drone):
    """Drone with health issues"""
    mock_drone.telemetry.health = MagicMock(
        return_value=make_async_iter(MockHealth(
            is_global_position_ok=False,
            is_armable=False,
            is_magnetometer_calibration_ok=False
        ))
    )
    return mock_drone


@pytest.fixture
def grounded_drone(mock_drone):
    """Drone on the ground"""
    mock_drone.telemetry.in_air = MagicMock(return_value=make_async_iter(False))
    mock_drone.telemetry.armed = MagicMock(return_value=make_async_iter(False))
    mock_drone.telemetry.position = MagicMock(
        return_value=make_async_iter(MockPosition(relative_altitude_m=0.0))
    )
    return mock_drone


@pytest.fixture
def mission_complete_drone(mock_drone):
    """Drone with completed mission"""
    mock_drone.mission.is_mission_finished = AsyncMock(return_value=True)
    mock_drone.mission.mission_progress = MagicMock(
        return_value=make_async_iter(MockMissionProgress(current=5, total=5))
    )
    return mock_drone


# ============================================================================
# Environment Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Set required environment variables for testing"""
    monkeypatch.setenv("MAVLINK_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("MAVLINK_PORT", "14540")
    monkeypatch.setenv("MAVLINK_PROTOCOL", "udp")


@pytest.fixture(autouse=True)
def suppress_logging():
    """Suppress logging during tests"""
    import logging
    logging.getLogger("MAVLinkMCP").setLevel(logging.CRITICAL)


"""
Pytest fixtures for MAVLink MCP integration testing.

Connects to a REAL SITL or drone - no mocks!

Requirements:
  - SITL running: sim_vehicle.py -v ArduCopter --console --map
  - Or drone connected via MAVLink

Environment variables:
  - MAVLINK_ADDRESS: default 127.0.0.1
  - MAVLINK_PORT: default 14540
  - MAVLINK_PROTOCOL: default udp
"""
import pytest
import asyncio
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from mavsdk import System


# ============================================================================
# Configuration
# ============================================================================

def get_connection_string():
    """Build MAVLink connection string from environment"""
    protocol = os.getenv("MAVLINK_PROTOCOL", "udp")
    address = os.getenv("MAVLINK_ADDRESS", "127.0.0.1")
    port = os.getenv("MAVLINK_PORT", "14540")
    return f"{protocol}://{address}:{port}"


# ============================================================================
# Core Fixtures
# ============================================================================

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def drone():
    """
    Connect to a real SITL or drone.
    
    This fixture is session-scoped - connects once and reuses.
    
    Requirements:
        Start SITL before running tests:
        sim_vehicle.py -v ArduCopter --console --map
    """
    drone = System()
    connection_string = get_connection_string()
    
    print(f"\n🔌 Connecting to drone at {connection_string}...")
    
    await drone.connect(system_address=connection_string)
    
    # Wait for connection with timeout
    timeout = 30
    connected = False
    
    async for state in drone.core.connection_state():
        if state.is_connected:
            print("✅ Connected to drone!")
            connected = True
            break
        await asyncio.sleep(0.5)
        timeout -= 0.5
        if timeout <= 0:
            break
    
    if not connected:
        pytest.skip("Could not connect to drone/SITL. Is it running?")
    
    # Wait for GPS lock
    print("📡 Waiting for GPS lock...")
    async for health in drone.telemetry.health():
        if health.is_global_position_ok:
            print("✅ GPS lock acquired!")
            break
        await asyncio.sleep(0.5)
    
    yield drone
    
    # Cleanup: ensure disarmed
    try:
        async for armed in drone.telemetry.armed():
            if armed:
                print("⚠️ Disarming drone after tests...")
                await drone.action.disarm()
            break
    except Exception:
        pass


@pytest.fixture
async def armed_drone(drone):
    """
    Fixture that arms the drone before test and disarms after.
    
    Use for tests that need the drone armed.
    """
    # Arm
    await drone.action.arm()
    
    # Wait for armed
    async for armed in drone.telemetry.armed():
        if armed:
            break
    
    yield drone
    
    # Disarm after test
    try:
        # First land if in air
        async for in_air in drone.telemetry.in_air():
            if in_air:
                await drone.action.land()
                await asyncio.sleep(5)  # Wait for landing
            break
        
        await drone.action.disarm()
    except Exception:
        pass


@pytest.fixture
async def flying_drone(armed_drone):
    """
    Fixture that takes off before test and lands after.
    
    Use for tests that need the drone in the air.
    """
    # Takeoff to 5 meters
    await armed_drone.action.set_takeoff_altitude(5.0)
    await armed_drone.action.takeoff()
    
    # Wait until in air
    await asyncio.sleep(5)
    
    yield armed_drone
    
    # Land after test
    await armed_drone.action.land()
    await asyncio.sleep(5)


# ============================================================================
# Helper Functions (available to tests)
# ============================================================================

async def wait_for_altitude(drone, target_altitude, tolerance=1.0, timeout=30):
    """Wait for drone to reach target altitude (relative)"""
    start = asyncio.get_event_loop().time()
    
    async for position in drone.telemetry.position():
        if abs(position.relative_altitude_m - target_altitude) <= tolerance:
            return True
        if asyncio.get_event_loop().time() - start > timeout:
            return False
        await asyncio.sleep(0.5)
    
    return False


async def wait_for_armed(drone, expected=True, timeout=10):
    """Wait for drone to be armed/disarmed"""
    start = asyncio.get_event_loop().time()
    
    async for armed in drone.telemetry.armed():
        if armed == expected:
            return True
        if asyncio.get_event_loop().time() - start > timeout:
            return False
        await asyncio.sleep(0.1)
    
    return False


async def get_current_position(drone):
    """Get current position as dict"""
    async for pos in drone.telemetry.position():
        return {
            "latitude_deg": pos.latitude_deg,
            "longitude_deg": pos.longitude_deg,
            "absolute_altitude_m": pos.absolute_altitude_m,
            "relative_altitude_m": pos.relative_altitude_m,
        }

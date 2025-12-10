# MAVLink MCP Integration Tests

**Real drone/SITL tests - no mocks!**

These tests connect to an actual SITL simulator or real drone via MAVLink. They verify the entire communication stack works correctly.

---

## Quick Start

### 1. Start SITL

```bash
# In a separate terminal, start ArduPilot SITL:
sim_vehicle.py -v ArduCopter --console --map
```

### 2. Run Tests

```bash
cd /home/peter/Documents/CursorCode/MavlinkMCP

# Run all tests
uv run pytest tests/ -v

# Run just telemetry tests (safe - no flying)
uv run pytest tests/test_telemetry.py -v

# Run just parameter tests (safe - no flying)
uv run pytest tests/test_parameters.py -v
```

---

## Test Files

| File | What it Tests | Flies? |
|------|---------------|--------|
| `test_telemetry.py` | Position, battery, GPS, attitude | ❌ No |
| `test_parameters.py` | Read/write drone parameters | ❌ No |
| `test_basic_flight.py` | Arm, takeoff, land, hold | ✈️ Yes |
| `test_navigation.py` | GPS navigation, yaw control | ✈️ Yes |
| `test_mission.py` | Mission upload, execution | ✈️ Yes |
| `test_emergency_safety.py` | RTL, battery checks | ✈️ Yes |

---

## Safe Tests (No Flying)

These tests are safe to run anytime - they only read data:

```bash
# Telemetry only
uv run pytest tests/test_telemetry.py -v

# Parameters only
uv run pytest tests/test_parameters.py -v

# Both safe tests
uv run pytest tests/test_telemetry.py tests/test_parameters.py -v
```

---

## Flight Tests (⚠️ Will Fly!)

These tests will **arm and fly** the drone:

```bash
# Basic flight (arm, takeoff, land)
uv run pytest tests/test_basic_flight.py -v

# Navigation (GPS waypoints)
uv run pytest tests/test_navigation.py -v

# Mission execution
uv run pytest tests/test_mission.py -v

# Emergency features (RTL)
uv run pytest tests/test_emergency_safety.py -v
```

---

## Configuration

### Environment Variables

```bash
# Connection settings (defaults shown)
export MAVLINK_ADDRESS=127.0.0.1
export MAVLINK_PORT=14540
export MAVLINK_PROTOCOL=udp
```

### Cloud SITL

To connect to a cloud-hosted SITL:

```bash
export MAVLINK_ADDRESS=your-sitl-server.com
export MAVLINK_PORT=14540
uv run pytest tests/ -v
```

---

## Fixtures

The `conftest.py` provides these fixtures:

| Fixture | Description |
|---------|-------------|
| `drone` | Connected MAVSDK drone (session-scoped) |
| `armed_drone` | Drone that's armed (disarms after test) |
| `flying_drone` | Drone at 5m altitude (lands after test) |

### Usage Examples

```python
async def test_read_position(drone):
    """Uses connected drone - no arming"""
    async for pos in drone.telemetry.position():
        print(pos.latitude_deg)
        break

async def test_fly_somewhere(flying_drone):
    """Uses drone already in the air at 5m"""
    await flying_drone.action.goto_location(...)
```

---

## What's Tested

### Telemetry
- ✅ GPS position (lat, lon, alt)
- ✅ Battery (voltage, percent)
- ✅ GPS info (satellites, fix type)
- ✅ Health status (all sensors)
- ✅ Flight mode
- ✅ Armed status
- ✅ In-air status
- ✅ Attitude (roll, pitch, yaw)
- ✅ Velocity (NED)

### Parameters
- ✅ Read float parameters
- ✅ Read int parameters
- ✅ Write parameters
- ✅ Get all parameters

### Flight Control
- ✅ Arm / Disarm
- ✅ Takeoff
- ✅ Land
- ✅ Hold position

### Navigation
- ✅ Go to GPS location
- ✅ Yaw/heading control

### Missions
- ✅ Upload mission
- ✅ Clear mission
- ✅ Mission progress
- ✅ Mission execution

### Safety
- ✅ Return to Launch (RTL)
- ✅ Battery monitoring
- ✅ Pre-arm health checks

---

## Troubleshooting

### "Could not connect to drone/SITL"

Make sure SITL is running:
```bash
sim_vehicle.py -v ArduCopter --console --map
```

### "Drone should be armable"

Check SITL console for pre-arm failures. Common issues:
- GPS not locked (wait longer)
- Safety switch not disabled

### Tests timing out

Increase timeouts in conftest.py or individual tests.

---

## Why No Mocks?

**Mocks don't test what matters:**
- They don't verify MAVLink protocol works
- They don't catch SITL/firmware bugs
- They give false confidence

**Real tests verify:**
- Actual MAVLink communication
- Real sensor data formats
- Actual drone behavior
- End-to-end functionality

---

## Running on CI/CD

For GitHub Actions, use a cloud-hosted SITL:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      sitl:
        image: your-sitl-image
        ports:
          - 14540:14540
    steps:
      - uses: actions/checkout@v4
      - run: uv sync
      - run: uv run pytest tests/test_telemetry.py tests/test_parameters.py -v
```

Or use a hosted SITL service and set `MAVLINK_ADDRESS` accordingly.

---

## Adding New Tests

```python
"""tests/test_my_feature.py"""
import pytest

class TestMyFeature:
    async def test_something(self, drone):
        """Test using connected drone"""
        # Your test code here
        async for pos in drone.telemetry.position():
            assert pos.latitude_deg != 0
            break
    
    async def test_in_flight(self, flying_drone):
        """Test that needs drone in the air"""
        # Drone is already at 5m altitude
        await flying_drone.action.goto_location(...)
```

---

**Happy Testing! 🚁**

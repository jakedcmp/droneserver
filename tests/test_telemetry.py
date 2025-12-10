"""
Integration tests for telemetry tools.

Connects to real SITL/drone - no mocks!

Run SITL first:
    sim_vehicle.py -v ArduCopter --console --map

Then run tests:
    uv run pytest tests/test_telemetry.py -v
"""
import pytest


class TestTelemetry:
    """Tests for telemetry reading from real drone"""
    
    async def test_get_position(self, drone):
        """Test we can read GPS position"""
        async for position in drone.telemetry.position():
            assert -90 <= position.latitude_deg <= 90
            assert -180 <= position.longitude_deg <= 180
            assert position.absolute_altitude_m is not None
            print(f"📍 Position: {position.latitude_deg:.6f}, {position.longitude_deg:.6f}")
            break
    
    async def test_get_battery(self, drone):
        """Test we can read battery status"""
        async for battery in drone.telemetry.battery():
            assert battery.voltage_v > 0
            print(f"🔋 Battery: {battery.voltage_v:.1f}V, {battery.remaining_percent*100:.0f}%")
            break
    
    async def test_get_gps_info(self, drone):
        """Test we can read GPS info"""
        async for gps in drone.telemetry.gps_info():
            assert gps.num_satellites >= 0
            print(f"📡 GPS: {gps.num_satellites} satellites, fix: {gps.fix_type}")
            break
    
    async def test_get_health(self, drone):
        """Test we can read health status"""
        async for health in drone.telemetry.health():
            # Just verify we can read it
            print(f"💚 Health: armable={health.is_armable}, GPS={health.is_global_position_ok}")
            break
    
    async def test_get_flight_mode(self, drone):
        """Test we can read flight mode"""
        async for mode in drone.telemetry.flight_mode():
            print(f"🎮 Mode: {mode}")
            break
    
    async def test_get_armed_status(self, drone):
        """Test we can read armed status"""
        async for armed in drone.telemetry.armed():
            print(f"🔒 Armed: {armed}")
            break
    
    async def test_get_in_air_status(self, drone):
        """Test we can read in-air status"""
        async for in_air in drone.telemetry.in_air():
            print(f"✈️ In air: {in_air}")
            break
    
    async def test_get_attitude(self, drone):
        """Test we can read attitude"""
        async for attitude in drone.telemetry.attitude_euler():
            assert -180 <= attitude.roll_deg <= 180
            assert -90 <= attitude.pitch_deg <= 90
            assert 0 <= attitude.yaw_deg <= 360 or -180 <= attitude.yaw_deg <= 180
            print(f"🧭 Attitude: roll={attitude.roll_deg:.1f}°, pitch={attitude.pitch_deg:.1f}°, yaw={attitude.yaw_deg:.1f}°")
            break
    
    async def test_get_velocity(self, drone):
        """Test we can read velocity"""
        async for velocity in drone.telemetry.velocity_ned():
            print(f"💨 Velocity: N={velocity.north_m_s:.1f}, E={velocity.east_m_s:.1f}, D={velocity.down_m_s:.1f} m/s")
            break

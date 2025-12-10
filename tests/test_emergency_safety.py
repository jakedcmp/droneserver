"""
Integration tests for emergency and safety features.

Connects to real SITL/drone - no mocks!

⚠️ WARNING: Some tests will FLY the drone!

Run SITL first:
    sim_vehicle.py -v ArduCopter --console --map

Then run tests:
    uv run pytest tests/test_emergency_safety.py -v
"""
import pytest
import asyncio


class TestRTL:
    """Tests for Return to Launch"""
    
    async def test_rtl_from_hover(self, flying_drone):
        """Test RTL while hovering"""
        # Get home position
        async for home in flying_drone.telemetry.home():
            home_lat = home.latitude_deg
            home_lon = home.longitude_deg
            print(f"🏠 Home: {home_lat:.6f}, {home_lon:.6f}")
            break
        
        # Fly away a bit
        async for pos in flying_drone.telemetry.position():
            target_lat = pos.latitude_deg + 0.0002  # ~22m north
            target_lon = pos.longitude_deg
            target_alt = pos.absolute_altitude_m
            break
        
        await flying_drone.action.goto_location(
            target_lat, target_lon, target_alt, float('nan')
        )
        await asyncio.sleep(8)
        print("📍 Moved away from home")
        
        # Trigger RTL
        await flying_drone.action.return_to_launch()
        print("🏠 RTL triggered")
        
        # Wait for RTL
        await asyncio.sleep(15)
        
        # Verify we're back near home
        async for pos in flying_drone.telemetry.position():
            current_lat = pos.latitude_deg
            current_lon = pos.longitude_deg
            
            lat_error = abs(current_lat - home_lat)
            lon_error = abs(current_lon - home_lon)
            
            # Should be within ~10m of home
            assert lat_error < 0.0001, f"Too far from home (lat): {lat_error}"
            assert lon_error < 0.0001, f"Too far from home (lon): {lon_error}"
            print(f"✅ Returned to home: {current_lat:.6f}, {current_lon:.6f}")
            break


class TestBatteryMonitoring:
    """Tests for battery safety"""
    
    async def test_battery_level(self, drone):
        """Test battery level is readable and reasonable"""
        async for battery in drone.telemetry.battery():
            voltage = battery.voltage_v
            percent = battery.remaining_percent * 100
            
            # Basic sanity checks
            assert voltage > 10.0, f"Battery voltage too low: {voltage}V"
            assert voltage < 20.0, f"Battery voltage too high: {voltage}V"
            
            print(f"🔋 Battery: {voltage:.1f}V, {percent:.0f}%")
            
            if percent < 20:
                print("⚠️ WARNING: Low battery!")
            break


class TestPreArmChecks:
    """Tests for pre-arm safety checks"""
    
    async def test_health_check(self, drone):
        """Test all health checks pass before arming"""
        async for health in drone.telemetry.health():
            print(f"💚 Gyro OK: {health.is_gyrometer_calibration_ok}")
            print(f"💚 Accel OK: {health.is_accelerometer_calibration_ok}")
            print(f"💚 Mag OK: {health.is_magnetometer_calibration_ok}")
            print(f"💚 GPS OK: {health.is_global_position_ok}")
            print(f"💚 Home OK: {health.is_home_position_ok}")
            print(f"💚 Armable: {health.is_armable}")
            
            assert health.is_armable, "Drone should be armable"
            print("✅ All pre-arm checks passed")
            break

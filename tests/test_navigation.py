"""
Integration tests for navigation.

Connects to real SITL/drone - no mocks!

⚠️ WARNING: These tests will FLY the drone!

Run SITL first:
    sim_vehicle.py -v ArduCopter --console --map

Then run tests:
    uv run pytest tests/test_navigation.py -v
"""
import pytest
import asyncio
import math


class TestNavigation:
    """Tests for navigation commands"""
    
    async def test_goto_location(self, flying_drone):
        """Test flying to a GPS location"""
        # Get current position
        async for pos in flying_drone.telemetry.position():
            start_lat = pos.latitude_deg
            start_lon = pos.longitude_deg
            start_alt = pos.absolute_altitude_m
            print(f"📍 Start: {start_lat:.6f}, {start_lon:.6f}")
            break
        
        # Calculate target ~30m north
        target_lat = start_lat + 0.00027  # ~30m
        target_lon = start_lon
        target_alt = start_alt
        
        # Go to location
        print(f"🎯 Flying to: {target_lat:.6f}, {target_lon:.6f}")
        await flying_drone.action.goto_location(
            target_lat, target_lon, target_alt, float('nan')
        )
        
        # Wait for movement
        await asyncio.sleep(10)
        
        # Check we moved
        async for pos in flying_drone.telemetry.position():
            end_lat = pos.latitude_deg
            print(f"📍 End: {end_lat:.6f}, {pos.longitude_deg:.6f}")
            
            # Should have moved north
            assert end_lat > start_lat, "Should have moved north"
            print("✅ Navigation successful")
            break


class TestYaw:
    """Tests for yaw/heading control"""
    
    async def test_set_yaw(self, flying_drone):
        """Test rotating to a heading"""
        # Get current heading
        async for attitude in flying_drone.telemetry.attitude_euler():
            start_yaw = attitude.yaw_deg
            print(f"🧭 Start heading: {start_yaw:.0f}°")
            break
        
        # Rotate to 90 degrees (east)
        target_yaw = 90.0
        
        # Get current position for goto_location with yaw
        async for pos in flying_drone.telemetry.position():
            await flying_drone.action.goto_location(
                pos.latitude_deg,
                pos.longitude_deg,
                pos.absolute_altitude_m,
                target_yaw
            )
            break
        
        # Wait for rotation
        await asyncio.sleep(5)
        
        # Check heading changed
        async for attitude in flying_drone.telemetry.attitude_euler():
            end_yaw = attitude.yaw_deg
            print(f"🧭 End heading: {end_yaw:.0f}°")
            
            # Should be close to 90 (±15 degrees)
            yaw_error = abs(end_yaw - target_yaw)
            if yaw_error > 180:
                yaw_error = 360 - yaw_error
            
            assert yaw_error < 20, f"Yaw error too large: {yaw_error}°"
            print("✅ Yaw control successful")
            break

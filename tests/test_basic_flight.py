"""
Integration tests for basic flight control.

Connects to real SITL/drone - no mocks!

⚠️ WARNING: These tests will ARM and FLY the drone!

Run SITL first:
    sim_vehicle.py -v ArduCopter --console --map

Then run tests:
    uv run pytest tests/test_basic_flight.py -v
"""
import pytest
import asyncio


class TestArming:
    """Tests for arm/disarm"""
    
    async def test_arm_and_disarm(self, drone):
        """Test arming and disarming the drone"""
        # Arm
        await drone.action.arm()
        await asyncio.sleep(1)
        
        # Verify armed
        async for armed in drone.telemetry.armed():
            assert armed, "Drone should be armed"
            print("✅ Drone armed")
            break
        
        # Disarm
        await drone.action.disarm()
        await asyncio.sleep(1)
        
        # Verify disarmed
        async for armed in drone.telemetry.armed():
            assert not armed, "Drone should be disarmed"
            print("✅ Drone disarmed")
            break


class TestTakeoffLand:
    """Tests for takeoff and landing"""
    
    async def test_takeoff_and_land(self, drone):
        """Test takeoff to 5m and land"""
        # Arm first
        await drone.action.arm()
        await asyncio.sleep(1)
        
        # Set takeoff altitude and takeoff
        await drone.action.set_takeoff_altitude(5.0)
        await drone.action.takeoff()
        print("🚀 Taking off...")
        
        # Wait for altitude
        await asyncio.sleep(8)
        
        # Check we're in the air
        async for position in drone.telemetry.position():
            altitude = position.relative_altitude_m
            print(f"📍 Altitude: {altitude:.1f}m")
            assert altitude > 3.0, f"Should be above 3m, got {altitude}m"
            break
        
        # Land
        await drone.action.land()
        print("🛬 Landing...")
        
        # Wait for landing
        await asyncio.sleep(10)
        
        # Check we're on ground
        async for in_air in drone.telemetry.in_air():
            assert not in_air, "Should be on ground"
            print("✅ Landed safely")
            break
        
        # Disarm
        await drone.action.disarm()


class TestHold:
    """Tests for hold/hover"""
    
    async def test_hold_position(self, flying_drone):
        """Test holding position"""
        # Get current position
        async for pos in flying_drone.telemetry.position():
            lat1, lon1 = pos.latitude_deg, pos.longitude_deg
            print(f"📍 Initial position: {lat1:.6f}, {lon1:.6f}")
            break
        
        # Hold for 3 seconds
        await flying_drone.action.hold()
        await asyncio.sleep(3)
        
        # Check we haven't moved much
        async for pos in flying_drone.telemetry.position():
            lat2, lon2 = pos.latitude_deg, pos.longitude_deg
            print(f"📍 Final position: {lat2:.6f}, {lon2:.6f}")
            
            # Should be within ~5m (0.00005 degrees)
            assert abs(lat2 - lat1) < 0.0001, "Drifted too far north/south"
            assert abs(lon2 - lon1) < 0.0001, "Drifted too far east/west"
            print("✅ Position held")
            break

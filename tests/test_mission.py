"""
Integration tests for mission management.

Connects to real SITL/drone - no mocks!

⚠️ WARNING: Some tests will FLY the drone!

Run SITL first:
    sim_vehicle.py -v ArduCopter --console --map

Then run tests:
    uv run pytest tests/test_mission.py -v
"""
import pytest
import asyncio
from mavsdk.mission_raw import MissionItem


class TestMissionUpload:
    """Tests for mission upload (no flight required)"""
    
    async def test_upload_mission(self, drone):
        """Test uploading a mission without starting it"""
        # Get current position for relative waypoints
        async for pos in drone.telemetry.position():
            home_lat = int(pos.latitude_deg * 1e7)
            home_lon = int(pos.longitude_deg * 1e7)
            home_alt = pos.absolute_altitude_m
            break
        
        # Create simple 3-waypoint mission
        mission_items = [
            MissionItem(
                seq=0,
                frame=3,  # MAV_FRAME_GLOBAL_RELATIVE_ALT
                command=16,  # MAV_CMD_NAV_WAYPOINT
                current=1,
                autocontinue=1,
                param1=0, param2=0, param3=0, param4=float('nan'),
                x=home_lat + 1000,  # ~11m north
                y=home_lon,
                z=10.0,  # 10m altitude
                mission_type=0
            ),
            MissionItem(
                seq=1,
                frame=3,
                command=16,
                current=0,
                autocontinue=1,
                param1=0, param2=0, param3=0, param4=float('nan'),
                x=home_lat + 1000,
                y=home_lon + 1000,  # ~11m east
                z=10.0,
                mission_type=0
            ),
            MissionItem(
                seq=2,
                frame=3,
                command=16,
                current=0,
                autocontinue=1,
                param1=0, param2=0, param3=0, param4=float('nan'),
                x=home_lat,
                y=home_lon,
                z=10.0,
                mission_type=0
            ),
        ]
        
        # Upload mission
        await drone.mission_raw.upload_mission(mission_items)
        print("✅ Mission uploaded (3 waypoints)")
    
    async def test_clear_mission(self, drone):
        """Test clearing mission"""
        await drone.mission.clear_mission()
        print("✅ Mission cleared")


class TestMissionProgress:
    """Tests for mission progress monitoring"""
    
    async def test_mission_progress(self, drone):
        """Test reading mission progress"""
        async for progress in drone.mission.mission_progress():
            print(f"📊 Mission progress: {progress.current}/{progress.total}")
            break
    
    async def test_is_mission_finished(self, drone):
        """Test checking if mission is finished"""
        finished = await drone.mission.is_mission_finished()
        print(f"📊 Mission finished: {finished}")


class TestMissionExecution:
    """Tests for mission execution (WILL FLY!)"""
    
    @pytest.mark.slow
    async def test_simple_mission(self, armed_drone):
        """Test executing a simple mission - WILL FLY!"""
        # Get current position
        async for pos in armed_drone.telemetry.position():
            home_lat = int(pos.latitude_deg * 1e7)
            home_lon = int(pos.longitude_deg * 1e7)
            break
        
        # Create simple triangle mission
        mission_items = [
            MissionItem(
                seq=0, frame=3, command=22,  # MAV_CMD_NAV_TAKEOFF
                current=1, autocontinue=1,
                param1=0, param2=0, param3=0, param4=float('nan'),
                x=home_lat, y=home_lon, z=5.0,
                mission_type=0
            ),
            MissionItem(
                seq=1, frame=3, command=16,  # MAV_CMD_NAV_WAYPOINT
                current=0, autocontinue=1,
                param1=0, param2=0, param3=0, param4=float('nan'),
                x=home_lat + 500,  # ~5m north
                y=home_lon, z=5.0,
                mission_type=0
            ),
            MissionItem(
                seq=2, frame=3, command=21,  # MAV_CMD_NAV_LAND
                current=0, autocontinue=1,
                param1=0, param2=0, param3=0, param4=float('nan'),
                x=home_lat, y=home_lon, z=0.0,
                mission_type=0
            ),
        ]
        
        # Upload and start
        await armed_drone.mission_raw.upload_mission(mission_items)
        print("📤 Mission uploaded")
        
        await armed_drone.mission.start_mission()
        print("🚀 Mission started")
        
        # Wait for completion
        for _ in range(60):  # 60 second timeout
            finished = await armed_drone.mission.is_mission_finished()
            if finished:
                print("✅ Mission completed!")
                break
            
            async for progress in armed_drone.mission.mission_progress():
                print(f"📊 Progress: {progress.current}/{progress.total}")
                break
            
            await asyncio.sleep(2)
        
        # Wait for landing
        await asyncio.sleep(5)

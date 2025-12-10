"""
Tests for mission management tools:
- initiate_mission
- print_mission_progress
- pause_mission (deprecated)
- hold_mission_position
- resume_mission
- clear_mission
- upload_mission
- download_mission
- set_current_waypoint
- is_mission_finished
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import sys
import os
from dataclasses import dataclass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'server'))

from mavlinkmcp import (
    initiate_mission,
    print_mission_progress,
    pause_mission,
    hold_mission_position,
    resume_mission,
    clear_mission,
    upload_mission,
    download_mission,
    set_current_waypoint,
    is_mission_finished,
)


@dataclass
class MockMissionProgress:
    """Mock mission progress"""
    current: int = 2
    total: int = 5


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


# Sample waypoints for testing
SAMPLE_WAYPOINTS = [
    {"latitude_deg": 33.645, "longitude_deg": -117.842, "relative_altitude_m": 10.0, "speed_m_s": 5.0},
    {"latitude_deg": 33.646, "longitude_deg": -117.843, "relative_altitude_m": 15.0, "speed_m_s": 5.0},
    {"latitude_deg": 33.647, "longitude_deg": -117.844, "relative_altitude_m": 20.0, "speed_m_s": 5.0},
]

SIMPLE_WAYPOINTS = [
    {"latitude_deg": 33.645, "longitude_deg": -117.842, "relative_altitude_m": 10.0},
    {"latitude_deg": 33.646, "longitude_deg": -117.843, "relative_altitude_m": 15.0},
]


class TestInitiateMission:
    """Tests for initiate_mission tool"""
    
    async def test_initiate_mission_success(self, mock_context, mock_drone):
        """Test successful mission initiation"""
        result = await initiate_mission(mock_context, mission_points=SAMPLE_WAYPOINTS)
        
        assert result["status"] == "success"
        assert "3" in result["message"]  # 3 waypoints
        mock_drone.mission_raw.upload_mission.assert_called_once()
        mock_drone.mission.start_mission.assert_called_once()
    
    async def test_initiate_mission_with_rtl(self, mock_context, mock_drone):
        """Test mission with return to launch enabled"""
        result = await initiate_mission(
            mock_context, 
            mission_points=SAMPLE_WAYPOINTS,
            return_to_launch=True
        )
        
        assert result["status"] == "success"
        mock_drone.mission.set_return_to_launch_after_mission.assert_called_once_with(True)
    
    async def test_initiate_mission_invalid_latitude(self, mock_context, mock_drone):
        """Test rejection of invalid latitude"""
        invalid_waypoints = [
            {"latitude_deg": 100.0, "longitude_deg": -117.842, "relative_altitude_m": 10.0, "speed_m_s": 5.0},
        ]
        
        result = await initiate_mission(mock_context, mission_points=invalid_waypoints)
        
        assert result["status"] == "failed"
        assert "latitude" in result["error"].lower()
    
    async def test_initiate_mission_invalid_longitude(self, mock_context, mock_drone):
        """Test rejection of invalid longitude"""
        invalid_waypoints = [
            {"latitude_deg": 33.645, "longitude_deg": -200.0, "relative_altitude_m": 10.0, "speed_m_s": 5.0},
        ]
        
        result = await initiate_mission(mock_context, mission_points=invalid_waypoints)
        
        assert result["status"] == "failed"
        assert "longitude" in result["error"].lower()
    
    async def test_initiate_mission_missing_field(self, mock_context, mock_drone):
        """Test rejection of waypoints missing required fields"""
        invalid_waypoints = [
            {"latitude_deg": 33.645},  # Missing longitude and altitude
        ]
        
        result = await initiate_mission(mock_context, mission_points=invalid_waypoints)
        
        assert result["status"] == "failed"
    
    async def test_initiate_mission_connection_timeout(self, disconnected_context):
        """Test mission fails when drone not connected"""
        result = await initiate_mission(disconnected_context, mission_points=SAMPLE_WAYPOINTS)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestPrintMissionProgress:
    """Tests for print_mission_progress tool"""
    
    async def test_print_mission_progress_success(self, mock_context, mock_drone):
        """Test successful mission progress read"""
        result = await print_mission_progress(mock_context)
        
        assert result["status"] == "success"
        assert "current" in result
        assert "total" in result
    
    async def test_print_mission_progress_values(self, mock_context, mock_drone):
        """Test progress returns correct values"""
        # Default: 2/5
        result = await print_mission_progress(mock_context)
        
        assert result["current"] == 2
        assert result["total"] == 5
    
    async def test_print_mission_progress_connection_timeout(self, disconnected_context):
        """Test progress fails when drone not connected"""
        result = await print_mission_progress(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestPauseMission:
    """Tests for pause_mission tool (DEPRECATED)"""
    
    async def test_pause_mission_returns_deprecated_error(self, mock_context, mock_drone):
        """Test that pause_mission returns deprecation error"""
        result = await pause_mission(mock_context)
        
        assert result["status"] == "failed"
        assert "deprecated" in result["error"].lower() or "deprecated" in str(result).lower()
    
    async def test_pause_mission_suggests_alternative(self, mock_context, mock_drone):
        """Test that pause_mission suggests hold_mission_position"""
        result = await pause_mission(mock_context)
        
        assert "hold_mission_position" in result.get("safe_alternative", "")


class TestHoldMissionPosition:
    """Tests for hold_mission_position tool"""
    
    async def test_hold_mission_position_success(self, mock_context, mock_drone):
        """Test successful mission position hold"""
        result = await hold_mission_position(mock_context)
        
        assert result["status"] == "success"
        assert "GUIDED" in result.get("flight_mode", "") or "GUIDED" in result.get("message", "")
        mock_drone.action.goto_location.assert_called_once()
    
    async def test_hold_mission_position_reports_waypoint(self, mock_context, mock_drone):
        """Test that hold reports current waypoint"""
        result = await hold_mission_position(mock_context)
        
        assert result["status"] == "success"
        assert "was_at_waypoint" in result or "waypoint" in result.get("message", "").lower()
    
    async def test_hold_mission_position_returns_position(self, mock_context, mock_drone):
        """Test that hold returns current position"""
        result = await hold_mission_position(mock_context)
        
        assert result["status"] == "success"
        assert "position" in result
    
    async def test_hold_mission_position_connection_timeout(self, disconnected_context):
        """Test hold fails when drone not connected"""
        result = await hold_mission_position(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestResumeMission:
    """Tests for resume_mission tool"""
    
    async def test_resume_mission_success(self, mock_context, mock_drone):
        """Test successful mission resume"""
        result = await resume_mission(mock_context)
        
        assert result["status"] == "success"
        mock_drone.mission.start_mission.assert_called_once()
    
    async def test_resume_mission_reports_waypoint(self, mock_context, mock_drone):
        """Test that resume reports current waypoint"""
        result = await resume_mission(mock_context)
        
        assert result["status"] == "success"
        assert "current_waypoint" in result
    
    async def test_resume_mission_connection_timeout(self, disconnected_context):
        """Test resume fails when drone not connected"""
        result = await resume_mission(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestClearMission:
    """Tests for clear_mission tool"""
    
    async def test_clear_mission_success(self, mock_context, mock_drone):
        """Test successful mission clear"""
        result = await clear_mission(mock_context)
        
        assert result["status"] == "success"
        assert "cleared" in result["message"].lower()
        mock_drone.mission.clear_mission.assert_called_once()
    
    async def test_clear_mission_connection_timeout(self, disconnected_context):
        """Test clear fails when drone not connected"""
        result = await clear_mission(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestUploadMission:
    """Tests for upload_mission tool"""
    
    async def test_upload_mission_success(self, mock_context, mock_drone):
        """Test successful mission upload (without starting)"""
        result = await upload_mission(mock_context, waypoints=SIMPLE_WAYPOINTS)
        
        assert result["status"] == "success"
        assert result["waypoint_count"] == 2
        mock_drone.mission_raw.upload_mission.assert_called_once()
        # Should NOT start mission automatically
        mock_drone.mission.start_mission.assert_not_called()
    
    async def test_upload_mission_empty_waypoints(self, mock_context, mock_drone):
        """Test rejection of empty waypoints"""
        result = await upload_mission(mock_context, waypoints=[])
        
        assert result["status"] == "failed"
        assert "no waypoints" in result["error"].lower()
    
    async def test_upload_mission_invalid_type(self, mock_context, mock_drone):
        """Test rejection of non-list waypoints"""
        result = await upload_mission(mock_context, waypoints="not a list")
        
        assert result["status"] == "failed"
        assert "list" in result["error"].lower()
    
    async def test_upload_mission_missing_fields(self, mock_context, mock_drone):
        """Test rejection of waypoints missing required fields"""
        incomplete_waypoints = [
            {"latitude_deg": 33.645},  # Missing longitude and altitude
        ]
        
        result = await upload_mission(mock_context, waypoints=incomplete_waypoints)
        
        assert result["status"] == "failed"
        assert "missing" in result["error"].lower()
    
    async def test_upload_mission_invalid_coordinates(self, mock_context, mock_drone):
        """Test rejection of invalid coordinates"""
        invalid_waypoints = [
            {"latitude_deg": 100.0, "longitude_deg": -117.842, "relative_altitude_m": 10.0},
        ]
        
        result = await upload_mission(mock_context, waypoints=invalid_waypoints)
        
        assert result["status"] == "failed"
        assert "latitude" in result["error"].lower()
    
    async def test_upload_mission_connection_timeout(self, disconnected_context):
        """Test upload fails when drone not connected"""
        result = await upload_mission(disconnected_context, waypoints=SIMPLE_WAYPOINTS)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestDownloadMission:
    """Tests for download_mission tool"""
    
    async def test_download_mission_success(self, mock_context, mock_drone):
        """Test successful mission download"""
        # Mock mission items
        from mavsdk.mission_raw import MissionItem
        mock_items = [
            MagicMock(seq=0, command=16, x=336450000, y=-1178420000, z=10.0, frame=3),
            MagicMock(seq=1, command=16, x=336460000, y=-1178430000, z=15.0, frame=3),
        ]
        mock_drone.mission_raw.download_mission = AsyncMock(return_value=mock_items)
        
        result = await download_mission(mock_context)
        
        assert result["status"] == "success"
        assert "waypoints" in result
    
    async def test_download_mission_empty(self, mock_context, mock_drone):
        """Test download with no mission on drone"""
        # Mock empty mission (0 waypoints)
        mock_drone.mission.mission_progress = MagicMock(
            return_value=make_async_iter(MockMissionProgress(current=0, total=0))
        )
        
        result = await download_mission(mock_context)
        
        assert result["status"] == "failed"
        assert "no mission" in result["error"].lower() or "0" in result["error"]
    
    async def test_download_mission_connection_timeout(self, disconnected_context):
        """Test download fails when drone not connected"""
        result = await download_mission(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestSetCurrentWaypoint:
    """Tests for set_current_waypoint tool"""
    
    async def test_set_current_waypoint_success(self, mock_context, mock_drone):
        """Test successful waypoint set"""
        result = await set_current_waypoint(mock_context, waypoint_index=3)
        
        assert result["status"] == "success"
        assert result["waypoint_index"] == 3
        mock_drone.mission.set_current_mission_item.assert_called_once_with(3)
    
    async def test_set_current_waypoint_first(self, mock_context, mock_drone):
        """Test setting to first waypoint (restart mission)"""
        result = await set_current_waypoint(mock_context, waypoint_index=0)
        
        assert result["status"] == "success"
        assert result["waypoint_index"] == 0
    
    async def test_set_current_waypoint_negative(self, mock_context, mock_drone):
        """Test rejection of negative index"""
        result = await set_current_waypoint(mock_context, waypoint_index=-1)
        
        assert result["status"] == "failed"
        assert "invalid" in result["error"].lower()
    
    async def test_set_current_waypoint_connection_timeout(self, disconnected_context):
        """Test waypoint set fails when drone not connected"""
        result = await set_current_waypoint(disconnected_context, waypoint_index=0)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()


class TestIsMissionFinished:
    """Tests for is_mission_finished tool"""
    
    async def test_is_mission_finished_not_finished(self, mock_context, mock_drone):
        """Test mission in progress"""
        result = await is_mission_finished(mock_context)
        
        assert result["status"] == "success"
        assert result["mission_finished"] == False
        assert result["status_text"] == "IN PROGRESS"
    
    async def test_is_mission_finished_completed(self, mock_context, mock_drone):
        """Test completed mission"""
        mock_drone.mission.is_mission_finished = AsyncMock(return_value=True)
        mock_drone.mission.mission_progress = MagicMock(
            return_value=make_async_iter(MockMissionProgress(current=5, total=5))
        )
        
        result = await is_mission_finished(mock_context)
        
        assert result["status"] == "success"
        assert result["mission_finished"] == True
        assert result["status_text"] == "FINISHED"
    
    async def test_is_mission_finished_reports_progress(self, mock_context, mock_drone):
        """Test that finish check reports waypoint progress"""
        result = await is_mission_finished(mock_context)
        
        assert result["status"] == "success"
        assert "current_waypoint" in result
        assert "total_waypoints" in result
        assert "progress_percentage" in result
    
    async def test_is_mission_finished_connection_timeout(self, disconnected_context):
        """Test finish check fails when drone not connected"""
        result = await is_mission_finished(disconnected_context)
        
        assert result["status"] == "failed"
        assert "timeout" in result["error"].lower()

